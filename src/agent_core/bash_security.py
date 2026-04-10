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
"""

from __future__ import annotations

import re
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
    ) -> None:
        self._restricted_whitelist = frozenset(
            restricted_whitelist
        ) if restricted_whitelist is not None else _DEFAULT_RESTRICTED_WHITELIST
        self._allow_run_for_restricted = allow_run_for_restricted

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
