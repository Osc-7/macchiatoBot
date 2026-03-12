"""
解析水源 Discourse 正文中的图片引用，转换为 ContentReference。

支持的几种常见格式：
- Markdown： ![alt|WxH](upload://shortcode.ext)
- 富文本 lightbox：
    <a class="lightbox" data-download-href="/uploads/short-url/xxx.png?dl=1">...</a>
- 直接 secure-uploads URL：
    <img src="https://shuiyuan.sjtu.edu.cn/secure-uploads/optimized/..." ...>
"""

from __future__ import annotations

import re
from typing import List, Tuple

from agent_core.content import ContentReference

SHUIYUAN_SITE_URL = "https://shuiyuan.sjtu.edu.cn"

# 1）Markdown upload:// 短链格式
_UPLOAD_RE = re.compile(r"!\[([^\]]*)\]\((upload://[^\s)]+)\)")

# 2）lightbox 的 data-download-href="/uploads/short-url/xxx.png?dl=1"
_DOWNLOAD_HREF_RE = re.compile(
    r'data-download-href="(/uploads/short-url/[^"]+)"', re.IGNORECASE
)

# 3）secure-uploads / S3 优化图直链：
#    - src="https://shuiyuan.sjtu.edu.cn/secure-uploads/..."
#    - src="https://shuiyuan.s3.jcloud.sjtu.edu.cn/optimized/4X/..."
_SECURE_UPLOAD_RE = re.compile(
    r'src="(https?://[^"]*secure-uploads[^"]*)"|src="(https?://shuiyuan\\.s3\\.jcloud\\.sjtu\\.edu\\.cn/optimized/[^"]*)"',
    re.IGNORECASE,
)


def _normalize_url(path_or_url: str, site_url: str) -> str:
    """将 /uploads/... 形式补全为绝对 URL，已有 http(s) 则直接返回。"""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return f"{site_url.rstrip('/')}{path_or_url}"


def parse_shuiyuan_raw_images(
    raw: str,
    *,
    site_url: str = SHUIYUAN_SITE_URL,
) -> Tuple[List[ContentReference], str]:
    """
    从 Discourse raw/cooked 正文中提取嵌入图片。

    Args:
        raw: 帖子 raw（或 cooked）正文
        site_url: 水源站点地址

    Returns:
        (content_refs, cleaned_raw)
        - content_refs: 每张图对应一个 ContentReference(source="shuiyuan")
        - cleaned_raw: 将图片位置替换为 “[图片]” 占位符后的文本
    """
    text = raw or ""
    refs: List[ContentReference] = []

    # 1）先处理 Markdown upload:// 短链，直接替换为 [图片]
    def _replace_upload(m: re.Match) -> str:
        short_url = m.group(2)  # "upload://shortcode.ext"
        short_path = short_url[len("upload://") :]
        full_url = f"{site_url.rstrip('/')}/uploads/short-url/{short_path}"
        refs.append(
            ContentReference(
                source="shuiyuan",
                ref_type="image",
                key=full_url,
            )
        )
        return "[图片]"

    cleaned = _UPLOAD_RE.sub(_replace_upload, text)

    # 2）再识别 data-download-href="/uploads/short-url/xxx.png?dl=1"
    def _replace_download_href(m: re.Match) -> str:
        href = m.group(1)
        full_url = _normalize_url(href.split("?", 1)[0], site_url)
        refs.append(
            ContentReference(
                source="shuiyuan",
                ref_type="image",
                key=full_url,
            )
        )
        return 'data-download-href="[图片]"'

    cleaned = _DOWNLOAD_HREF_RE.sub(_replace_download_href, cleaned)

    # 3）最后识别 secure-uploads 的 img src，避免重复添加（去重按 key）
    seen_keys = {r.key for r in refs}

    def _replace_secure(m: re.Match) -> str:
        url = m.group(1) or m.group(2)
        if url not in seen_keys:
            refs.append(
                ContentReference(
                    source="shuiyuan",
                    ref_type="image",
                    key=url,
                )
            )
            seen_keys.add(url)
        return 'src="[图片]"'

    cleaned = _SECURE_UPLOAD_RE.sub(_replace_secure, cleaned)

    return refs, cleaned
