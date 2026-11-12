"""安全分析对「引号不配对」命令的回归。

bug 现场：命令里出现未配对引号（如 don't）时 shlex 抛
ValueError: No closing quotation，直通到工具层成了未处理异常，回合被打断。
"""
from __future__ import annotations

import pytest

from agent_core.bash_security import BashSecurity


def _checker() -> BashSecurity:
    return BashSecurity(workspace_jail_root="/tmp/ws-root", workspace_tmp_root="/tmp")


@pytest.mark.parametrize(
    "command",
    [
        "echo don't panic",
        "python3 -c \"print('a')\" && echo 'unclosed",
        "cat <<'EOF'\ndon't stop\nEOF",
        "echo \"未闭合的中文引号",
    ],
)
def test_unbalanced_quotes_do_not_raise(command: str) -> None:
    checker = _checker()
    # 不抛异常即可；判定结果不影响安全边界（沙箱/审批仍在）
    verdict = checker.check(command)
    assert verdict is not None


def test_normal_command_still_analyzed() -> None:
    checker = _checker()
    verdict = checker.check("echo hi > /root/outside.txt")
    assert verdict is not None
