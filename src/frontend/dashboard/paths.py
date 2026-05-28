"""Public URL paths for the web dashboard."""

from __future__ import annotations

import os

LOGIN_PATH = "/login"
CONSOLE_PREFIX = os.environ.get("MACCHIATO_DASHBOARD_CONSOLE_PREFIX", "/console").rstrip("/") or "/console"


def console_path(suffix: str = "") -> str:
    if not suffix:
        return CONSOLE_PREFIX or "/"
    if not suffix.startswith("/"):
        suffix = f"/{suffix}"
    return f"{CONSOLE_PREFIX}{suffix}"


def public_paths() -> set[str]:
    return {
        LOGIN_PATH,
        console_path("/api/auth/login"),
        console_path("/api/auth/logout"),
        console_path("/api/auth/status"),
    }


def public_prefixes() -> tuple[str, ...]:
    return (console_path("/assets/"),)


def is_console_api(path: str) -> bool:
    return path.startswith(console_path("/api/"))
