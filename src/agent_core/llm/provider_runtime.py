"""
运行时注册 LLM provider 的参数解析与 YAML 加载。

支持三种输入方式（可组合）：
1. ``--file path/to/provider.yaml`` — 与 ``config/llm/providers.d/*.yaml`` 相同格式
2. ``--base-url / --api-key / --model / ...`` — 命令行逐项指定
3. 末尾 JSON 对象 — 高级字段（``vendor_params``、``capabilities`` 等）一次性传入
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

_BOOL = re.compile(r"^(true|yes|on|1|false|no|off|0)$", re.I)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "yes", "on", "1")


def _parse_kv_pair(raw: str) -> Tuple[str, str]:
    if "=" not in raw:
        raise ValueError(f"期望 key=value，收到: {raw!r}")
    key, val = raw.split("=", 1)
    key = key.strip()
    val = val.strip()
    if not key:
        raise ValueError(f"空的 key: {raw!r}")
    return key, val


def load_providers_from_yaml(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    从 provider 片段 YAML 加载 ``name -> ProviderEntry dict``。

    支持两种顶层格式（与 ``load_config`` 的 provider 片段一致）：
    - ``providers: { name: {...} }``
    - ``name: { base_url, api_key, model, ... }``
    """
    if not path.is_file():
        raise FileNotFoundError(f"provider 文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        frag = yaml.safe_load(f)
    if frag is None:
        return {}
    if not isinstance(frag, dict):
        raise ValueError(f"provider YAML 顶层应为 dict: {path}")

    if "providers" in frag and isinstance(frag["providers"], dict):
        out = {str(k): dict(v) for k, v in frag["providers"].items() if isinstance(v, dict)}
    else:
        out = {str(k): dict(v) for k, v in frag.items() if isinstance(v, dict)}
    if not out:
        raise ValueError(f"provider YAML 中未找到有效条目: {path}")
    return out


def resolve_provider_yaml_path(raw: str, *, cwd: Optional[Path] = None) -> Path:
    """解析 ``@path``、相对路径或绝对路径。"""
    text = str(raw or "").strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if not text:
        raise ValueError("file 路径不能为空")

    p = Path(text)
    if p.is_absolute():
        return p

    base = cwd or Path.cwd()
    candidates = [
        base / p,
        base / "config" / p,
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return (base / p).resolve()


@dataclass
class ModelAddEntry:
    name: str
    provider: Dict[str, Any]


@dataclass
class ModelAddRequest:
    entries: List[ModelAddEntry] = field(default_factory=list)
    persist: bool = True
    overwrite: bool = False
    switch_to: Optional[str] = None


def _merge_provider_dict(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in extra.items():
        if key == "capabilities" and isinstance(val, dict):
            caps = dict(out.get("capabilities") or {})
            caps.update(val)
            out["capabilities"] = caps
        elif key == "vendor_params" and isinstance(val, dict):
            vp = dict(out.get("vendor_params") or {})
            vp.update(val)
            out["vendor_params"] = vp
        elif key == "headers" and isinstance(val, dict):
            hdr = dict(out.get("headers") or {})
            hdr.update(val)
            out["headers"] = hdr
        else:
            out[key] = val
    return out


def _apply_scalar_flag(provider: Dict[str, Any], flag: str, value: str) -> None:
    mapping = {
        "--base-url": "base_url",
        "-u": "base_url",
        "--api-key": "api_key",
        "-k": "api_key",
        "--model": "model",
        "-m": "model",
        "--label": "label",
        "-l": "label",
        "--protocol": "protocol",
        "--auth-file": "auth_file",
        "--reasoning-effort": "reasoning_effort",
        "--temperature": "temperature",
        "--context-window": ("capabilities", "context_window"),
    }
    cap_flags = {
        "--vision": ("vision", True),
        "--no-vision": ("vision", False),
        "--tools": ("function_calling", True),
        "--no-tools": ("function_calling", False),
        "--reasoning": ("reasoning_content", True),
        "--no-reasoning": ("reasoning_content", False),
        "--thinking-inline": ("thinking_tag_inline", True),
        "--parallel-tools": ("parallel_tool_calls", True),
    }

    if flag in mapping:
        target = mapping[flag]
        if isinstance(target, tuple):
            caps = dict(provider.get("capabilities") or {})
            key, parsed = target
            if key == "context_window":
                caps[key] = int(value)
            else:
                caps[key] = value
            provider["capabilities"] = caps
        elif flag == "--temperature":
            provider["temperature"] = float(value)
        else:
            provider[target] = value
        return

    if flag in cap_flags:
        cap_key, cap_val = cap_flags[flag]
        caps = dict(provider.get("capabilities") or {})
        caps[cap_key] = cap_val
        provider["capabilities"] = caps
        return

    if flag == "--vendor-param":
        k, v = _parse_kv_pair(value)
        vp = dict(provider.get("vendor_params") or {})
        vp[k] = _coerce_json_value(v)
        provider["vendor_params"] = vp
        return

    if flag == "--header":
        k, v = _parse_kv_pair(value)
        hdr = dict(provider.get("headers") or {})
        hdr[k] = v
        provider["headers"] = hdr
        return

    raise ValueError(f"未知参数: {flag}")


def _coerce_json_value(raw: str) -> Any:
    text = raw.strip()
    if _BOOL.match(text):
        return _parse_bool(text)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    ):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return raw


def _try_parse_json_blob(text: str) -> Optional[Dict[str, Any]]:
    s = text.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        raise ValueError("JSON 追加段必须是 object")
    return obj


def parse_model_add_cli(
    arg_text: str,
    *,
    cwd: Optional[Path] = None,
) -> ModelAddRequest:
    """
    解析 ``/model add ...`` 命令行参数。

    示例::

        /model add glm52 -u https://glm/v4 -k sk-xxx -m glm-5.2 --label "GLM 5.2" --reasoning
        /model add --file config/llm/providers.d/glm.yaml
        /model add my_glm --file glm.yaml --switch
        /model add glm52 -u ... -m ... '{"vendor_params":{"enable_thinking":true}}'
    """
    tokens = shlex.split(arg_text.strip()) if arg_text.strip() else []
    if not tokens or tokens[0].lower() != "add":
        raise ValueError("用法以 add 开头")

    req = ModelAddRequest()
    i = 1
    name: Optional[str] = None
    inline: Dict[str, Any] = {}
    file_path: Optional[str] = None
    json_tail: Optional[Dict[str, Any]] = None

    scalar_needs_value = {
        "--base-url",
        "-u",
        "--api-key",
        "-k",
        "--model",
        "-m",
        "--label",
        "-l",
        "--protocol",
        "--auth-file",
        "--reasoning-effort",
        "--temperature",
        "--context-window",
        "--vendor-param",
        "--header",
        "--file",
        "-f",
    }

    positional: List[str] = []

    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()

        if low in ("--no-persist",):
            req.persist = False
            i += 1
            continue
        if low in ("--overwrite",):
            req.overwrite = True
            i += 1
            continue
        if low in ("--switch", "--switch-to"):
            req.switch_to = "__pending__"
            i += 1
            continue
        if low in ("--file", "-f"):
            if i + 1 >= len(tokens):
                raise ValueError("--file 需要路径参数")
            file_path = tokens[i + 1]
            i += 2
            continue

        if tok.startswith("-") and low in scalar_needs_value:
            if i + 1 >= len(tokens):
                raise ValueError(f"{tok} 需要参数值")
            if low in ("--file", "-f"):
                file_path = tokens[i + 1]
            else:
                _apply_scalar_flag(inline, tok, tokens[i + 1])
            i += 2
            continue

        if tok.startswith("-"):
            _apply_scalar_flag(inline, tok, "")
            i += 1
            continue

        blob = _try_parse_json_blob(tok)
        if blob is not None:
            json_tail = blob
            i += 1
            continue

        positional.append(tok)
        i += 1

    if positional:
        name = positional[0]
        if len(positional) > 1:
            extra_json = _try_parse_json_blob(" ".join(positional[1:]))
            if extra_json is None:
                raise ValueError(f"无法解析多余参数: {' '.join(positional[1:])}")
            json_tail = _merge_provider_dict(json_tail or {}, extra_json)

    file_entries: Dict[str, Dict[str, Any]] = {}
    if file_path:
        path = resolve_provider_yaml_path(file_path, cwd=cwd)
        file_entries = load_providers_from_yaml(path)

    if file_entries and name:
        if len(file_entries) == 1:
            only_cfg = next(iter(file_entries.values()))
            merged = _merge_provider_dict(only_cfg, inline)
            if json_tail:
                merged = _merge_provider_dict(merged, json_tail)
            req.entries.append(ModelAddEntry(name=name, provider=merged))
        elif name in file_entries:
            merged = _merge_provider_dict(file_entries[name], inline)
            if json_tail:
                merged = _merge_provider_dict(merged, json_tail)
            req.entries.append(ModelAddEntry(name=name, provider=merged))
        else:
            raise ValueError(
                f"文件中含多个 provider（{list(file_entries)}），"
                f"请指定与 YAML key 一致的 name，或省略 name 一次导入全部"
            )
    elif file_entries:
        for entry_name, cfg in file_entries.items():
            merged = _merge_provider_dict(cfg, inline)
            if json_tail:
                merged = _merge_provider_dict(merged, json_tail)
            req.entries.append(ModelAddEntry(name=entry_name, provider=merged))
    elif name:
        merged = dict(inline)
        if json_tail:
            merged = _merge_provider_dict(merged, json_tail)
        if not merged.get("base_url") or not merged.get("model"):
            raise ValueError(
                "内联注册至少需要 --base-url 与 --model；"
                "或使用 --file 加载完整 YAML 片段"
            )
        req.entries.append(ModelAddEntry(name=name, provider=merged))
    else:
        raise ValueError(
            "请指定 provider 名，或使用 --file 从 YAML 导入。"
            "运行 /model add help 查看示例。"
        )

    if req.switch_to == "__pending__":
        req.switch_to = req.entries[0].name if len(req.entries) == 1 else None
        if req.switch_to is None:
            raise ValueError("--switch 仅适用于单条注册")

    return req


MODEL_ADD_HELP = """
/model add — 运行注册新 LLM provider（无需重启 daemon）

方式 1：命令行指定连接参数（api_key 可用 ${ENV_VAR}）
  /model add <name> --base-url <url> --api-key <key> --model <api-model-id> [选项]

  常用选项:
    --label "展示名"          /model 切换时显示的备注名
    --protocol openai|anthropic|codex_oauth
    --vision / --no-vision
    --reasoning / --no-reasoning
    --context-window 1000000
    --temperature 0.7
    --vendor-param key=value   可重复，写入 vendor_params
    --header key=value         可重复，自定义 HTTP header
    --switch                   注册后立即切换为当前 session 主模型
    --overwrite                覆盖同名 provider
    --no-persist               仅内存生效，不写 _runtime.yaml

  示例:
    /model add glm52 -u https://open.bigmodel.cn/api/paas/v4 \\
        -k ${GLM_API_KEY} -m glm-5.2 --label "GLM-5.2" --reasoning --switch

方式 2：从 YAML 文件导入（与 config/llm/providers.d/*.yaml 相同格式）
  /model add --file config/llm/providers.d/glm.yaml
  /model add my_alias --file glm.yaml --switch

方式 3：JSON 追加高级字段（可与方式 1/2 组合，放在命令末尾）
  /model add glm52 -u ... -m ... '{"vendor_params":{"enable_thinking":true}}'

注册后可用 /model list 查看，/model <label或name> 切换。
持久化默认写入 providers_dir/_runtime.yaml，daemon 重启后仍有效。
""".strip()
