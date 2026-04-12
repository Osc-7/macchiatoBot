from __future__ import annotations
from typing import Optional

from markdown_it import MarkdownIt
from bs4 import BeautifulSoup

"""
飞书端 Markdown 过滤工具。

飞书文本消息（msg_type="text"）不支持 Markdown 渲染，这里统一在发送前
将 Markdown 转为适合飞书展示的纯文本。

说明：Agent 主回复若配置为 feishu.reply_format=markdown_card，则走交互卡片发送，
由卡片内 markdown 组件渲染，**不会**调用本模块；本模块仅用于仍使用
send_text_message 的路径（错误提示、斜杠指令回显、部分系统通知等）。

- 用 markdown-it-py 将 Markdown 解析为 HTML
- 用 BeautifulSoup 提取可读文本，并保留链接/图片 URL
- 再做一次简单的换行归一化，避免一整段糊成一坨
"""


def _normalize_whitespace(text: str) -> str:
    """标准化尾随空格和空行数量，提升在飞书中的可读性。"""
    lines = [line.rstrip() for line in text.splitlines()]

    normalized_lines: list[str] = []
    empty_count = 0
    for line in lines:
        if line.strip():
            empty_count = 0
            normalized_lines.append(line)
        else:
            empty_count += 1
            # 这里将连续空行上限设为 1，
            # 确保不会出现 "\n\n\n" 这种 3 个及以上空行的情况。
            if empty_count <= 1:
                normalized_lines.append("")

    return "\n".join(normalized_lines).strip()


_md_renderer: Optional[MarkdownIt] = None


def _get_md_renderer() -> MarkdownIt:
    global _md_renderer
    if _md_renderer is None:
        # 使用默认配置即可满足大多数 Markdown 语法
        _md_renderer = MarkdownIt()
    return _md_renderer


def _markdown_to_html(md: str) -> str:
    renderer = _get_md_renderer()
    return renderer.render(md)


def _html_to_plain_text(html: str) -> str:
    """
    使用 BeautifulSoup 将 HTML 转为纯文本，同时保留链接/图片的 URL 信息。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 先处理链接：<a href="...">文本</a> -> "文本 (url)"
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        label = a.get_text(strip=True)
        if href and label:
            replacement = f"{label} ({href})"
        else:
            replacement = label or href
        a.replace_with(replacement)

    # 处理图片：<img alt="alt" src="url" /> -> "[图片: alt] (url)"
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        src = (img.get("src") or "").strip()
        if alt and src:
            replacement = f"[图片: {alt}] ({src})"
        elif src:
            replacement = f"[图片] ({src})"
        else:
            replacement = "[图片]"
        img.replace_with(replacement)

    # 使用换行作为分隔符，避免所有块级元素黏在一行
    text = soup.get_text("\n")
    return text


def filter_markdown_for_feishu(text: str) -> str:
    """
    将 Markdown 文本转换为适合飞书文本消息展示的纯文本。

    设计目标：
    - 尽量完整保留语义内容
    - 去掉 Markdown 语法噪音
    - 保留合理的换行，避免整段粘在一起
    """
    if not text:
        return text

    html = _markdown_to_html(text)
    plain = _html_to_plain_text(html)
    result = _normalize_whitespace(plain)
    return result
