#!/usr/bin/env python3
"""mbot-efforts.py — 给 daemon 造「推理档位」变体，让 DSH 的 effort 选择有东西可选。

背景：daemon 的 `reasoning_effort` 是 **per-provider 配置字段**（不是每请求参数），
而且目前只有 `protocol: codex_oauth` 真正把它发出去。DSH 侧要做到「effort 可调」，
只能把一个模型摊成几份配置：

    gpt-5.6-sol            reasoning_effort: medium   （原来的）
    gpt-5.6-sol__low       reasoning_effort: low      （本脚本生成）
    gpt-5.6-sol__high      ...
    gpt-5.6-sol__xhigh     ...

DSH 侧（lib/provider.js）会把这些变体折叠回「一个模型 + 多档 effort」，
选中档位后 `model_switch` 到对应变体名。

用法（在 daemon 那台机器上跑）：

    python3 mbot-efforts.py --list          # 看看哪些模型支持档位
    python3 mbot-efforts.py                 # 生成 / 更新变体并热重载
    python3 mbot-efforts.py --remove        # 删掉变体文件并热重载（回滚）

产物：`config/llm/providers.d/zz_lab_effort.yaml`（单独一个文件，方便一键删掉）
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh")
SUFFIX = "__"
OUT_NAME = "zz_lab_effort.yaml"
MB_ROOT = Path("/home/macchiato/macchiatoBot")
SOCK = MB_ROOT / "data/automation/automation.sock"


def ipc(method: str, params: dict | None = None, timeout: float = 30.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(SOCK))
    try:
        s.sendall((json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode() or "{}")
    finally:
        s.close()


def load_yaml(path: Path):
    import yaml  # daemon 的 venv 里有

    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def supports_effort(entry: dict) -> bool:
    """只有 codex_oauth 协议真的会发 reasoning_effort；其它协议改了也不会生效。"""
    return str(entry.get("protocol") or "") == "codex_oauth"


def collect(providers_dir: Path) -> dict:
    found: dict[str, dict] = {}
    for path in sorted(providers_dir.glob("*.yaml")):
        if path.name == OUT_NAME:
            continue
        for name, entry in load_yaml(path).items():
            if isinstance(entry, dict) and supports_effort(entry):
                found[str(name)] = entry
    return found


def build_variants(found: dict) -> dict:
    out: dict[str, dict] = {}
    for name, entry in found.items():
        label = str(entry.get("label") or name)
        for effort in EFFORTS:
            variant = dict(entry)
            variant["reasoning_effort"] = effort
            variant["label"] = f"{label} · {effort}"
            out[f"{name}{SUFFIX}{effort}"] = variant
    return out


def write_variants(path: Path, variants: dict) -> None:
    import yaml

    header = (
        "# 由 @lab/dsh-agent-mbot 的 scripts/mbot-efforts.py 生成，别手改。\n"
        "# 每个 <模型>__<档位> 都是原模型的一份拷贝，只改了 reasoning_effort。\n"
        "# 回滚：python3 mbot-efforts.py --remove\n"
    )
    path.write_text(header + yaml.safe_dump(variants, allow_unicode=True, sort_keys=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(MB_ROOT))
    ap.add_argument("--list", action="store_true", help="只列出支持档位的模型")
    ap.add_argument("--remove", action="store_true", help="删除变体文件并热重载")
    args = ap.parse_args()

    providers_dir = Path(args.root) / "config/llm/providers.d"
    if not providers_dir.is_dir():
        print(f"找不到 {providers_dir}", file=sys.stderr)
        return 1
    out_path = providers_dir / OUT_NAME

    if args.remove:
        if out_path.exists():
            out_path.unlink()
            print(f"已删除 {out_path}")
        else:
            print("没有变体文件，无需删除")
        print(ipc("model_reload"))
        return 0

    found = collect(providers_dir)
    if args.list:
        print(json.dumps({name: entry.get("label") for name, entry in found.items()}, ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("没有支持 reasoning_effort 的模型（当前只有 codex_oauth 协议支持）")
        return 0

    variants = build_variants(found)
    write_variants(out_path, variants)
    print(f"写入 {out_path} · {len(variants)} 个变体（{', '.join(sorted(found))}）")
    reload_result = ipc("model_reload")
    print(json.dumps(reload_result, ensure_ascii=False)[:400])

    models = (ipc("model_list", {"client_id": "effort-probe"}).get("result") or {}).get("models") or []
    names = [str(m.get("name")) for m in models if SUFFIX in str(m.get("name"))]
    print(f"model_list 里现在有 {len(names)} 个档位变体：{', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
