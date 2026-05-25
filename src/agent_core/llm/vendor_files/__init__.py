"""厂商 Files API 适配（按 provider 能力声明启用）。"""

from __future__ import annotations

from .kimi import (
    KimiVendorFilesClient,
    build_kimi_vendor_files_client,
    is_kimi_ms_url,
    ms_url,
)

__all__ = [
    "KimiVendorFilesClient",
    "build_kimi_vendor_files_client",
    "is_kimi_ms_url",
    "ms_url",
]
