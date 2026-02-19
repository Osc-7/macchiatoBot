"""
CLI 输出样式

在 TTY 下使用 ANSI 颜色与粗体提升可读性；非 TTY 或管道时回退为纯文本。
"""

import shutil
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
WHITE = "\033[37m"
BRIGHT_BLACK = "\033[90m"


def _use_color() -> bool:
    """是否使用颜色（仅交互式终端）"""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _term_width() -> int:
    return shutil.get_terminal_size((80, 20)).columns


def t(text: str, color: str = "", bold: bool = False, dim: bool = False) -> str:
    """
    按当前环境返回带样式或纯文本。

    Args:
        text: 原始文本
        color: ANSI 颜色码（如 CYAN）
        bold: 是否加粗
        dim: 是否暗色
    """
    if not _use_color():
        return text
    parts = []
    if bold:
        parts.append(BOLD)
    if dim:
        parts.append(DIM)
    if color:
        parts.append(color)
    parts.append(text)
    parts.append(RESET)
    return "".join(parts)


def title(text: str) -> str:
    """区块标题（粗体 + 青色）"""
    return t(text, CYAN, bold=True)


def hint(text: str) -> str:
    """提示/说明（暗色）"""
    return t(text, dim=True)


def label(text: str) -> str:
    """标签（绿色）"""
    return t(text, GREEN)


def accent(text: str) -> str:
    """强调（黄色）"""
    return t(text, YELLOW)


def prompt_prefix() -> str:
    """输入提示符「❯ 」的样式"""
    return t("❯ ", CYAN, bold=True)


def assistant_prefix() -> str:
    """助手回复前的标签样式"""
    return t("助手: ", GREEN, bold=True)


def sep_line(char: str = "-", length: int = 50) -> str:
    """分隔线"""
    return char * length


def thin_separator() -> str:
    """轮次之间的淡色分隔线，占满终端宽度"""
    w = _term_width()
    line = "─" * w
    return t(line, dim=True)


def _format_token_count(n: int) -> str:
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"

import datetime



def status_bar(total_tokens: int, call_count: int, delta_tokens: int = 0) -> str:
    """
    生成带右边空隙和本轮调用时刻的 token 状态栏（淡色，右对齐，右侧留空，含本轮时间）。

    右对齐但最右边保留4格空白，并把本轮时间显示在最右侧（短格式: HH:MM:SS）

    示例:
    ───── tokens: 1,234 (+56) · calls: 3 │ 17:45:09 ──
    """
    total_str = _format_token_count(total_tokens)
    delta_part = f" (+{_format_token_count(delta_tokens)})" if delta_tokens > 0 else ""
    info = f" tokens: {total_str}{delta_part} · calls: {call_count} │ "  # tokens状态后加竖线


    # 当前时刻，短格式（24小时制，Asia/Shanghai）
    import zoneinfo
    time_str = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")

    w = _term_width()
    min_right_space = 3
    bar_len = w - min_right_space - len(time_str) if w > 0 else 46

    if len(info) + 2 > bar_len:
        bar_line = info[-bar_len:]
    else:
        pad = bar_len - len(info)
        bar_line = "─" * pad + info

    # 右侧空白+时刻
    return t(f"{bar_line}{time_str}{' ' + '─' * (min_right_space - 1)}", dim=True)
