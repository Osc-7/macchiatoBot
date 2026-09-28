"""
ModelCatalog — daemon 侧 LLM provider 目录的唯一权威。

设计：
- 目录数据仍然存放在进程级 ``Config.llm.providers``（原地更新），所有持有同一 ``Config`` 的对象
  （CorePool / AgentCore / gateway）自然读到最新目录；不替换 ``get_config()`` 单例对象。
- 每个 ``LLMClient`` 构造时 attach 到 catalog（weakref）；目录变更后 catalog 调用其
  ``sync_from_catalog()`` 做增量同步：新增 provider 立即可切换，正在使用的 provider 不会被强制移除。
- 运行时注册（``/model add``）默认持久化到 ``providers_dir/_runtime.yaml``；``reload_from_disk``
  会重新解析 config.yaml + providers.d + _runtime.yaml。

本模块只关心模型层（providers / active / vision_provider），不做整份 config 热替换。
"""

from __future__ import annotations

import logging
import threading
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core import config as config_mod
from agent_core.config import CapabilitiesModel, Config, LLMConfig, ProviderEntry

logger = logging.getLogger(__name__)


def _entry_fingerprint(entry: Any) -> Any:
    """用于 diff 的可比较表示。"""
    dump = getattr(entry, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001
            pass
    return repr(entry)


def build_provider_entry(name: str, raw: Dict[str, Any]) -> ProviderEntry:
    """从 dict 构造 ``ProviderEntry``：展开 ``${ENV}`` 后交给 Pydantic 校验。"""
    if not isinstance(raw, dict):
        raise ValueError(f"provider {name!r} 条目必须是 dict")
    holder: Dict[str, Any] = {name: dict(raw)}
    config_mod._expand_env_vars_in_providers(holder)
    return ProviderEntry(**holder[name])


def provider_row(
    name: str,
    entry: Any,
    *,
    active_name: Optional[str] = None,
    vision_name: Optional[str] = None,
) -> Dict[str, Any]:
    """与 ``AgentCore.list_models`` 字段一致的单行描述。"""
    caps_m = getattr(entry, "capabilities", None) or CapabilitiesModel()
    api_model = str(getattr(entry, "model", "") or "")
    return {
        "name": name,
        "model": api_model,
        "api_model": api_model,
        "label": getattr(entry, "label", None),
        "base_url": getattr(entry, "base_url", None),
        "vision": bool(getattr(caps_m, "vision", False)),
        "function_calling": bool(getattr(caps_m, "function_calling", True)),
        "reasoning_content": bool(getattr(caps_m, "reasoning_content", False)),
        "context_window": getattr(caps_m, "context_window", None),
        "is_active": bool(active_name) and name == active_name,
        "is_vision_provider": bool(vision_name) and name == vision_name,
    }


def resolve_vision_provider_name(llm: Any) -> Optional[str]:
    """配置的 vision_provider 若有效则用之；否则挑第一个 ``capabilities.vision=True``。"""
    prov_map = getattr(llm, "providers", None) or {}
    configured = getattr(llm, "vision_provider", None)
    if configured and str(configured) in prov_map:
        return str(configured)
    for n, ent in prov_map.items():
        caps_m = getattr(ent, "capabilities", None)
        if caps_m is not None and bool(getattr(caps_m, "vision", False)):
            return str(n)
    return None


class ModelCatalog:
    """LLM provider 目录：注册 / 磁盘重载 / 快照 / 通知已存活的 LLMClient。"""

    def __init__(self) -> None:
        self._generation = 0
        self._clients: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def generation(self) -> int:
        return self._generation

    @staticmethod
    def _resolve_config(config: Optional[Config]) -> Config:
        return config if config is not None else config_mod.get_config()

    def attach(self, client: Any) -> None:
        """登记一个 LLMClient（weakref）；目录变更时会调用其 ``sync_from_catalog()``。"""
        try:
            self._clients.add(client)
        except TypeError:
            logger.debug("ModelCatalog.attach: client 不支持 weakref，跳过")

    def detach(self, client: Any) -> None:
        self._clients.discard(client)

    @property
    def attached_client_count(self) -> int:
        return len(self._clients)

    def _bump_and_notify(self) -> int:
        self._generation += 1
        for client in list(self._clients):
            fn = getattr(client, "sync_from_catalog", None)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ModelCatalog: sync client failed: %s", exc)
        return self._generation

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(
        self,
        *,
        config: Optional[Config] = None,
        active_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按当前目录生成模型列表；``active_name`` 缺省取 ``llm.active`` / 首个 provider。"""
        cfg = self._resolve_config(config)
        llm = cfg.llm
        prov_map = getattr(llm, "providers", None) or {}
        if not prov_map:
            return []
        if not active_name:
            a = getattr(llm, "active", None)
            active_name = str(a) if a and str(a) in prov_map else next(iter(prov_map))
        vision_name = resolve_vision_provider_name(llm)
        return [
            provider_row(str(n), ent, active_name=active_name, vision_name=vision_name)
            for n, ent in prov_map.items()
        ]

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        entry: Dict[str, Any],
        *,
        persist: bool = True,
        overwrite: bool = False,
        config: Optional[Config] = None,
        config_path: Optional[Path] = None,
    ) -> ProviderEntry:
        """注册（或覆盖）一条 provider，并同步到所有已 attach 的 LLMClient。

        Raises:
            ValueError: 名称为空 / 同名且未 ``overwrite`` / 条目校验失败
        """
        key = str(name or "").strip()
        if not key:
            raise ValueError("provider 名不能为空")
        cfg = self._resolve_config(config)
        with self._lock:
            prov_map = dict(getattr(cfg.llm, "providers", None) or {})
            if key in prov_map and not overwrite:
                raise ValueError(f"provider {key!r} 已存在；如需覆盖请加 --overwrite")
            pe = build_provider_entry(key, entry)
            prov_map[key] = pe
            cfg.llm.providers = prov_map

            if persist:
                try:
                    path = config_mod.persist_runtime_provider(
                        key, dict(entry), config_path=config_path
                    )
                    logger.info("ModelCatalog: provider %s persisted to %s", key, path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ModelCatalog: persist provider %s failed: %s", key, exc
                    )

            gen = self._bump_and_notify()
        logger.info(
            "ModelCatalog: registered provider=%s api_model=%s generation=%d",
            key,
            pe.model,
            gen,
        )
        return pe

    # ------------------------------------------------------------------
    # 磁盘重载
    # ------------------------------------------------------------------
    def reload_from_disk(
        self,
        *,
        config: Optional[Config] = None,
        config_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """重新解析磁盘上的 llm 段，整体替换 ``config.llm.providers``，返回 diff。

        失败时保留上一代目录并抛出异常（调用方决定是否吞掉）。
        """
        cfg = self._resolve_config(config)
        new_llm: LLMConfig = config_mod.load_llm_catalog_snapshot(config_path)
        new_map: Dict[str, Any] = dict(getattr(new_llm, "providers", None) or {})
        if not new_map:
            raise ValueError("磁盘配置中 llm.providers 为空，拒绝重载")

        with self._lock:
            old_map: Dict[str, Any] = dict(getattr(cfg.llm, "providers", None) or {})
            added = sorted(k for k in new_map if k not in old_map)
            removed = sorted(k for k in old_map if k not in new_map)
            changed = sorted(
                k
                for k in new_map
                if k in old_map
                and _entry_fingerprint(new_map[k]) != _entry_fingerprint(old_map[k])
            )

            cfg.llm.providers = new_map
            cfg.llm.active = getattr(new_llm, "active", None)
            cfg.llm.vision_provider = getattr(new_llm, "vision_provider", None)
            cfg.llm.frontend_providers = dict(
                getattr(new_llm, "frontend_providers", None) or {}
            )
            gen = self._bump_and_notify()

        result = {
            "generation": gen,
            "added": added,
            "removed": removed,
            "changed": changed,
            "total": len(new_map),
            "active": cfg.llm.active,
            "vision_provider": resolve_vision_provider_name(cfg.llm),
        }
        logger.info(
            "ModelCatalog: reloaded from disk generation=%d added=%s removed=%s changed=%s",
            gen,
            added,
            removed,
            changed,
        )
        return result


def catalog_watch_paths(config_path: Optional[Path] = None) -> List[Path]:
    """模型目录涉及的磁盘文件：主配置 + provider_include 文件 + providers_dir 下全部 yaml。

    供 daemon watcher 计算 mtime 指纹；目录里新增/删除文件也会引起指纹变化。
    """
    cfg_path = (
        Path(config_path) if config_path is not None else config_mod.find_config_file()
    ).resolve()
    out: List[Path] = [cfg_path]
    try:
        import yaml

        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return out
    llm_raw = raw.get("llm") if isinstance(raw, dict) else None
    if not isinstance(llm_raw, dict):
        return out

    for rel in llm_raw.get("provider_include") or []:
        rel_s = str(rel or "").strip()
        if not rel_s:
            continue
        p = config_mod._resolve_llm_provider_file(rel_s, cfg_path)
        if p is not None:
            out.append(p)

    pd = str(llm_raw.get("providers_dir") or "").strip()
    if pd:
        d = config_mod._resolve_llm_providers_dir(pd, cfg_path)
        if d is not None:
            out.append(d)
            out.extend(sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")))
    return out


def catalog_files_fingerprint(config_path: Optional[Path] = None) -> Any:
    """(path, mtime_ns) 元组集合；任一文件变化 / 增删都会改变。"""
    items: List[Any] = []
    for p in catalog_watch_paths(config_path):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append((str(p), st.st_mtime_ns))
    return tuple(sorted(items))


_CATALOG = ModelCatalog()


def get_model_catalog() -> ModelCatalog:
    """进程级 ModelCatalog 单例。"""
    return _CATALOG


def reset_model_catalog() -> None:
    """测试用：清空已 attach 的 client 与代数。"""
    global _CATALOG
    _CATALOG = ModelCatalog()
