"""
BashSecurity -- 命令安全校验模块。

三层校验架构：
  Layer 1 -- 规则快速路径：受限模式白名单 + shell 运算符禁止
  Layer 2 -- 危险模式检测：正则匹配破坏性命令（rm -rf、sudo 等）
  Layer 3 -- 交互审批：危险命令返回 ASK_USER，由 BashTool 提示模型请求用户确认

参考：
- Claude Code bashSecurity.ts (23 条校验)
- Codex approval/sandbox 分层
- 早期命令工具的白名单与危险模式检测思路
- 更强隔离可考虑 Linux bubblewrap（见 Claude/Codex 文档），本模块仍为启发式校验
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from agent_core.kernel_interface.profile import CoreProfile


class SecurityAction(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK_USER = "ask_user"


@dataclass(frozen=True)
class SecurityVerdict:
    action: SecurityAction
    reason: str = ""
    error_code: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == SecurityAction.ALLOW

    @property
    def denied(self) -> bool:
        return self.action == SecurityAction.DENY

    @property
    def needs_confirmation(self) -> bool:
        return self.action == SecurityAction.ASK_USER


_VERDICT_ALLOW = SecurityVerdict(SecurityAction.ALLOW)

# ── Layer 2: 危险命令模式（从 command_tools.py 迁移并扩展）──────

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[^ ]*r[^ ]*|-rf|-fr|-r\s+-f)\b", re.I), "rm -rf 递归删除"),
    (re.compile(r"\brm\s+-[^ ]*f[^ ]*\s+-r\b", re.I), "rm -fr 递归删除"),
    (re.compile(r"\bchmod\s+(-R|--recursive)\b", re.I), "chmod -R 递归权限变更"),
    (re.compile(r"\bchown\s+(-R|--recursive)\b", re.I), "chown -R 递归属主变更"),
    (re.compile(r"\bdd\s+", re.I), "dd 磁盘写入"),
    (re.compile(r"\bsudo\b", re.I), "sudo 提权"),
    (re.compile(r"\bmkfs\.", re.I), "mkfs 格式化"),
    (re.compile(r">\s*/dev/(sd|hd|nvme|vd)[a-z]", re.I), "写入块设备"),
    (re.compile(r"\bformat\s+", re.I), "format 命令"),
    # 扩展：参考 Claude Code bashSecurity 的部分模式
    (re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b", re.I), "curl pipe to shell"),
    (re.compile(r"\bwget\b.*\|\s*(ba)?sh\b", re.I), "wget pipe to shell"),
    (re.compile(r"\beval\b", re.I), "eval 动态执行"),
    (re.compile(r">\s*/etc/", re.I), "写入 /etc/ 系统目录"),
    (re.compile(r"\bkill\s+-9\s", re.I), "kill -9 强制终止"),
    (re.compile(r"\bshutdown\b", re.I), "shutdown 关机"),
    (re.compile(r"\breboot\b", re.I), "reboot 重启"),
    (re.compile(r"\binit\s+[0-6]\b", re.I), "init 运行级别变更"),
]

# shell 运算符：受限模式下禁止，全量模式下不阻止
_SHELL_OPERATORS = ("|", "&", ";", "`", "$(", ">", ">>", "<", "&&", "||")

# 默认受限模式白名单（从 CommandToolsConfig.subagent_command_whitelist 迁移）
_DEFAULT_RESTRICTED_WHITELIST = frozenset([
    "ls", "pwd", "cat", "head", "tail", "grep", "find", "echo",
    "which", "file", "stat", "wc", "date", "whoami", "id", "env", "printenv",
])

# 工作区隔离：阻止通过特殊 builtin 绕过覆盖后的 cd
_WORKSPACE_JAIL_BUILTIN_CD = re.compile(r"\bbuiltin\s+cd\b", re.I)
_WORKSPACE_JAIL_COMMAND_CD = re.compile(r"\bcommand\s+cd\b", re.I)
_WORKSPACE_WRITE_MUTATING_ALL_PATHS = frozenset(
    {"touch", "mkdir", "rmdir", "truncate", "rm", "unlink"}
)
_WORKSPACE_WRITE_MUTATING_LAST_PATH = frozenset(
    {"mv", "cp", "install", "ln", "rsync"}
)
_WORKSPACE_SEGMENT_OPERATORS = frozenset({"&&", "||", ";", "|"})

# 重定向等「写」目标视为无害设备，不触发工作区外写入拒绝（对齐常见 shell 用法如 >/dev/null）
_WORKSPACE_WRITE_EXEMPT_POSIX_DEVS = frozenset(
    {
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
    }
)


def is_workspace_write_exempt_target(path_str: str) -> bool:
    """若为绝对路径且指向无害 POSIX 设备，则跳过工作区写范围检测。"""
    s = (path_str or "").strip()
    if not s.startswith("/"):
        return False
    try:
        resolved = Path(s).resolve()
    except OSError:
        return False
    key = str(resolved)
    if key in _WORKSPACE_WRITE_EXEMPT_POSIX_DEVS:
        return True
    # 兼容 /dev/null 等符号链接解析结果
    for dev in _WORKSPACE_WRITE_EXEMPT_POSIX_DEVS:
        try:
            if resolved == Path(dev).resolve():
                return True
        except OSError:
            continue
    return False


class BashSecurity:
    """
    命令安全校验器。

    根据 CoreProfile.mode 和配置决定校验严格程度：
    - sub 模式：Layer 1 白名单 + 禁止 shell 运算符（最严格）
    - full/background 模式：Layer 2 危险模式 → Layer 3 审批
    """

    def __init__(
        self,
        *,
        restricted_whitelist: Optional[List[str]] = None,
        allow_run_for_restricted: bool = False,
        workspace_jail_root: Optional[str] = None,
        workspace_tmp_root: Optional[str] = None,
        workspace_extra_write_roots: Optional[List[Path]] = None,
    ) -> None:
        self._restricted_whitelist = frozenset(
            restricted_whitelist
        ) if restricted_whitelist is not None else _DEFAULT_RESTRICTED_WHITELIST
        self._allow_run_for_restricted = allow_run_for_restricted
        self._workspace_jail_root = workspace_jail_root
        self._workspace_tmp_root = workspace_tmp_root
        self._workspace_extra_write_roots: List[Path] = list(workspace_extra_write_roots or [])

    def refresh_write_roots_from_config(
        self,
        source: str,
        user_id: str,
    ) -> None:
        """从全局 Config 重新合并可写根（含 ACL 文件），供 request_permission 写入后下一轮 bash 生效。"""
        if not self._workspace_jail_root:
            return
        from agent_core.config import get_config
        from agent_core.agent.workspace_paths import merged_bash_write_root_paths

        cfg = get_config()
        merged = merged_bash_write_root_paths(
            cfg.command_tools,
            source,
            user_id,
            app_config=cfg,
        )
        jail = Path(self._workspace_jail_root).resolve()
        tmp_r = (
            Path(self._workspace_tmp_root).resolve() if self._workspace_tmp_root else None
        )
        self._workspace_extra_write_roots = []
        for p in merged:
            pr = p.resolve()
            if pr == jail:
                continue
            if tmp_r is not None and pr == tmp_r:
                continue
            self._workspace_extra_write_roots.append(pr)

    def check(
        self,
        command: str,
        *,
        profile: Optional["CoreProfile"] = None,
        confirmed: bool = False,
    ) -> SecurityVerdict:
        """
        校验命令是否允许执行。

        Args:
            command: shell 命令字符串
            profile: CoreProfile（None 则视为 full 模式）
            confirmed: 用户是否已确认（跳过 Layer 3）
        """
        command = command.strip()
        if not command:
            return SecurityVerdict(
                SecurityAction.DENY,
                reason="命令为空",
                error_code="EMPTY_COMMAND",
            )

        if self._workspace_jail_root:
            jail = self._check_workspace_jail_bypass(command)
            if jail is not None:
                return jail
            jail = self._check_workspace_write_scope(command)
            if jail is not None:
                return jail

        mode = (profile.mode if profile else "full").lower()
        is_restricted = mode == "sub"

        # ── Layer 1: 受限模式白名单 ──────────────────────
        if is_restricted:
            if not self._allow_run_for_restricted:
                return SecurityVerdict(
                    SecurityAction.DENY,
                    reason="受限模式（sub）下 bash 未启用",
                    error_code="PERMISSION_DENIED",
                )
            return self._check_restricted(command)

        # ── Layer 2: 危险模式检测 ────────────────────────
        danger = self._check_dangerous(command)
        if danger is not None:
            if confirmed:
                return _VERDICT_ALLOW
            # ── Layer 3: 交互审批 ────────────────────
            return SecurityVerdict(
                SecurityAction.ASK_USER,
                reason=f"该命令可能造成不可逆损害（{danger}）。请先向用户展示命令内容，"
                       f"待用户确认后再调用并传 confirm=true 执行",
                error_code="CONFIRMATION_REQUIRED",
            )

        return _VERDICT_ALLOW

    def _check_workspace_jail_bypass(self, command: str) -> Optional[SecurityVerdict]:
        """工作区隔离已启用时，禁止显式调用 builtin/command cd 绕过函数式 cd。"""
        if _WORKSPACE_JAIL_BUILTIN_CD.search(command):
            return SecurityVerdict(
                SecurityAction.DENY,
                reason="工作区隔离模式下禁止使用 builtin cd 绕过目录防护",
                error_code="WORKSPACE_JAIL_DENIED",
            )
        if _WORKSPACE_JAIL_COMMAND_CD.search(command):
            return SecurityVerdict(
                SecurityAction.DENY,
                reason="工作区隔离模式下禁止使用 command cd 绕过目录防护",
                error_code="WORKSPACE_JAIL_DENIED",
            )
        return None

    def _check_workspace_write_scope(self, command: str) -> Optional[SecurityVerdict]:
        """
        工作区隔离已启用时，拒绝对工作区外显式写入/修改。

        目标：
        - 工作区内：允许读写
        - 工作区外：允许只读访问，但拒绝显式写路径（touch、cp 目标、重定向等）

        这是启发式规则，主要覆盖 shell / 常见文件工具的直接写路径。
        """
        roots = [Path(self._workspace_jail_root or "").resolve()]
        if self._workspace_tmp_root:
            roots.append(Path(self._workspace_tmp_root).resolve())
        roots.extend(self._workspace_extra_write_roots)
        for segment in self._split_command_segments(command):
            tokens = self._tokenize_segment(segment)
            if not tokens:
                continue
            for path_str in self._extract_write_targets(tokens):
                if is_workspace_write_exempt_target(path_str):
                    continue
                candidate = self._resolve_candidate_path(path_str, roots[0])
                if candidate is None:
                    continue
                if is_workspace_write_exempt_target(str(candidate)):
                    continue
                if not any(self._path_within_root(candidate, root) for root in roots):
                    return SecurityVerdict(
                        SecurityAction.DENY,
                        reason=f"工作区隔离模式下禁止写入工作区外路径: {candidate}",
                        error_code="WORKSPACE_WRITE_DENIED",
                    )
        return None

    @staticmethod
    def _split_command_segments(command: str) -> List[str]:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        segments: list[list[str]] = [[]]
        for token in lexer:
            if token in _WORKSPACE_SEGMENT_OPERATORS:
                if segments[-1]:
                    segments.append([])
                continue
            segments[-1].append(token)
        return [" ".join(seg).strip() for seg in segments if seg]

    @staticmethod
    def _tokenize_segment(segment: str) -> List[str]:
        lexer = shlex.shlex(segment, posix=True, punctuation_chars="<>")
        lexer.whitespace_split = True
        return list(lexer)

    @staticmethod
    def _is_option(token: str) -> bool:
        return token.startswith("-") and token != "-"

    @staticmethod
    def _resolve_candidate_path(path_str: str, root: Path) -> Optional[Path]:
        s = (path_str or "").strip()
        if not s:
            return None
        if s.startswith("~/"):
            return (root / s[2:]).resolve()
        if s.startswith("/"):
            return Path(s).resolve()
        return None

    @staticmethod
    def _path_within_root(candidate: Path, root: Path) -> bool:
        try:
            candidate.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _extract_write_targets(self, tokens: List[str]) -> List[str]:
        targets: list[str] = []
        targets.extend(self._extract_redirect_targets(tokens))
        if not tokens:
            return targets
        base_cmd = Path(tokens[0]).name.lower()
        args = tokens[1:]

        if base_cmd in _WORKSPACE_WRITE_MUTATING_ALL_PATHS:
            targets.extend(
                token for token in args if not self._is_option(token) and token != "--"
            )
            return targets

        if base_cmd in _WORKSPACE_WRITE_MUTATING_LAST_PATH:
            non_option = [
                token for token in args if not self._is_option(token) and token != "--"
            ]
            if non_option:
                targets.append(non_option[-1])
            return targets

        if base_cmd == "tee":
            targets.extend(
                token
                for token in args
                if not self._is_option(token) and token not in {"-", "/dev/stdout"}
            )
            return targets

        if base_cmd in {"chmod", "chown", "chgrp"}:
            non_option = [
                token for token in args if not self._is_option(token) and token != "--"
            ]
            if len(non_option) >= 2:
                targets.extend(non_option[1:])
            return targets

        if base_cmd == "sed" and any(
            token == "-i" or token.startswith("-i") for token in args
        ):
            non_option = [
                token for token in args if not self._is_option(token) and token != "--"
            ]
            if len(non_option) >= 2:
                targets.extend(non_option[1:])
            return targets

        if base_cmd == "dd":
            for token in args:
                if token.startswith("of="):
                    targets.append(token[3:])
            return targets

        return targets

    @staticmethod
    def _extract_redirect_targets(tokens: List[str]) -> List[str]:
        targets: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in {">", ">>", "1>", "1>>", "2>", "2>>"}:
                if i + 1 < len(tokens):
                    targets.append(tokens[i + 1])
                    i += 2
                    continue
            m = re.match(r"^(?:\d+)?(>>?)(.+)$", token)
            if m:
                targets.append(m.group(2))
            i += 1
        return targets

    # ── Layer 1 实现 ──────────────────────────────────────────

    def _check_restricted(self, command: str) -> SecurityVerdict:
        """受限模式：白名单 + 禁止 shell 运算符。"""
        for op in _SHELL_OPERATORS:
            if op in command:
                return SecurityVerdict(
                    SecurityAction.DENY,
                    reason=f"受限模式下禁止使用 shell 运算符（如 {op}），仅允许单条非破坏性命令",
                    error_code="SHELL_OPERATOR_DENIED",
                )

        parts = command.split()
        if not parts:
            return SecurityVerdict(
                SecurityAction.DENY,
                reason="命令为空",
                error_code="EMPTY_COMMAND",
            )

        base_cmd = Path(parts[0]).name.lower()
        if base_cmd not in self._restricted_whitelist:
            allowed_preview = ", ".join(sorted(self._restricted_whitelist)[:10])
            return SecurityVerdict(
                SecurityAction.DENY,
                reason=f"受限模式下命令 '{base_cmd}' 不在白名单内。"
                       f"允许的命令示例: {allowed_preview}",
                error_code="COMMAND_NOT_WHITELISTED",
            )

        danger = self._check_dangerous(command)
        if danger is not None:
            return SecurityVerdict(
                SecurityAction.DENY,
                reason=f"受限模式禁止执行危险命令（{danger}），仅允许只读白名单命令",
                error_code="COMMAND_DENIED",
            )

        return _VERDICT_ALLOW

    # ── Layer 2 实现 ──────────────────────────────────────────

    @staticmethod
    def _check_dangerous(command: str) -> Optional[str]:
        """检测危险命令模式，返回匹配的描述或 None。"""
        for pattern, description in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                return description
        return None
