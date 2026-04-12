"""
Macchiato 合成「类 Linux 用户」环境（隔离 bash 内）。

``BashRuntime`` 使用 ``--norc`` / ``--noprofile``，且隔离模式下 ``HOME`` 映射到工作区单元格根目录，
因此不会自动获得交互式终端里由 ``.bashrc`` / nvm / asdf 等注入的 PATH 与 XDG 基线。

本模块生成在 ``export HOME=...`` 之后执行的 bash 片段，用于：

- 按 **项目 node_modules → 工作区 node_modules → 合成用户 ``$HOME`` 下 ``.local/bin`` 等 → 宿主 ``MACCHIATO_REAL_HOME`` 常见工具链 → 继承的系统 PATH** 的顺序拼接 ``PATH``（后 prepend 的更靠前）；
- 为当前 ``HOME``（工作区）设置 **XDG Base Directory** 默认值并 ``mkdir -p``，使 CLI 的 ``~/.config`` 等语义与桌面 Linux 一致。

宿主侧路径一律基于 ``MACCHIATO_REAL_HOME``（服务进程用户真实 ``Path.home()``），
勿执行任意用户 shell 启动脚本（安全与可审计）。
"""

from __future__ import annotations

import re
from typing import List

# 仅允许相对 MACCHIATO_REAL_HOME 的安全子路径（配置项 bash_real_home_path_suffixes）
_SAFE_RH_SUFFIX = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$")


def validate_real_home_path_suffix(s: str) -> str:
    t = (s or "").strip()
    if not t or t.startswith("/") or ".." in t.split("/"):
        raise ValueError(
            f"bash_real_home_path_suffixes 项无效（须为相对真实家目录的子路径、无 ..）: {s!r}"
        )
    if not _SAFE_RH_SUFFIX.match(t):
        raise ValueError(f"bash_real_home_path_suffixes 项含非法字符: {s!r}")
    return t


def render_terminal_like_bootstrap_bash(
    *,
    extra_real_home_suffixes: List[str] | None = None,
) -> str:
    """
    返回在 ``MACCHIATO_*`` 已导出且 ``HOME`` 已指向工作区之后执行的 bash 片段。

    ``extra_real_home_suffixes``：相对 ``MACCHIATO_REAL_HOME`` 的额外目录（存在则加入 PATH），
    用于 fnm/pnpm 等非标准安装前缀。
    """
    extras: List[str] = []
    for raw in extra_real_home_suffixes or []:
        extras.append(validate_real_home_path_suffix(raw))
    extra_lines = "\n".join(
        f'  _macchiato_path_if_dir "$_rh/{s}"' for s in extras
    )

    # PATH：多次 prepend，**后**调用的目录在 PATH **更前**（优先命中）。
    # 调用顺序：先宿主 bin，再合成用户 HOME 下 bin，再工作区/项目 node_modules（项目最后 prepend，最优先）。
    path_block = f"""
_macchiato_path_if_dir() {{
  [ -z "${{1:-}}" ] && return 0
  [ -d "$1" ] || return 0
  case ":${{PATH:-}}:" in *":$1:"*) ;; *) PATH="$1:${{PATH:-}}" ;; esac
}}
if [ -n "${{MACCHIATO_REAL_HOME:-}}" ]; then
  _rh="$MACCHIATO_REAL_HOME"
  _macchiato_path_if_dir "$_rh/.cargo/bin"
  _macchiato_path_if_dir "$_rh/.deno/bin"
  _macchiato_path_if_dir "$_rh/.volta/bin"
  _macchiato_path_if_dir "$_rh/.local/bin"
  _macchiato_path_if_dir "$_rh/.npm-global/bin"
  _macchiato_path_if_dir "$_rh/bin"
  _macchiato_path_if_dir "$_rh/.asdf/bin"
  _macchiato_path_if_dir "$_rh/.asdf/shims"
  if [ -d "$_rh/.local/share/fnm/node-versions" ]; then
    for _fnm in "$_rh"/.local/share/fnm/node-versions/*/installation/bin; do
      [ -d "$_fnm" ] && _macchiato_path_if_dir "$_fnm"
    done
  fi
  if [ -d "$_rh/.nvm/versions/node" ]; then
    for _nv in "$_rh"/.nvm/versions/node/*/bin; do
      [ -d "$_nv" ] && _macchiato_path_if_dir "$_nv"
    done
  fi
  _macchiato_path_if_dir "$_rh/miniconda3/bin"
  _macchiato_path_if_dir "$_rh/anaconda3/bin"
  _macchiato_path_if_dir "$_rh/micromamba/bin"{("\n" + extra_lines) if extra_lines else ""}
fi
# 合成用户 HOME 下的用户级安装（pip --user / npm prefix 指向 ~ 等），与真终端「~/.local/bin 在 PATH」一致
_macchiato_path_if_dir "$HOME/.local/bin"
_macchiato_path_if_dir "$HOME/bin"
_macchiato_path_if_dir "$HOME/.npm-global/bin"
_macchiato_path_if_dir "${{MACCHIATO_WORKSPACE_ROOT:-}}/node_modules/.bin"
_macchiato_path_if_dir "${{MACCHIATO_PROJECT_ROOT:-}}/node_modules/.bin"
unset -f _macchiato_path_if_dir
export PATH
""".strip()

    xdg_block = """
# XDG：HOME 已指向工作区单元格，配置/缓存落在该「合成用户」目录下，与常见 Linux 桌面一致
: "${XDG_DATA_HOME:=$HOME/.local/share}"
: "${XDG_CONFIG_HOME:=$HOME/.config}"
: "${XDG_STATE_HOME:=$HOME/.local/state}"
: "${XDG_CACHE_HOME:=$HOME/.cache}"
export XDG_DATA_HOME XDG_CONFIG_HOME XDG_STATE_HOME XDG_CACHE_HOME
mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" 2>/dev/null || true
""".strip()

    return f"{xdg_block}\n{path_block}".strip()
