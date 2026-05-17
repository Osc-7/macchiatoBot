#!/usr/bin/env python3
"""
OpenAI Codex OAuth 登录工具。

用法：
    uv run python -m system.oauth.oauth_login [--provider chatgpt-plus]

流程：
    1. 生成授权 URL，打印到终端
    2. 用户在浏览器打开 URL，登录 ChatGPT 并授权
    3. 浏览器跳到 localhost（报错无视），用户复制完整 URL 粘贴回来
    4. 交换 token，写入 data/oauth/<provider>.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from pathlib import Path

from system.oauth.codex_oauth import (
    build_auth_url,
    exchange_code_for_token,
    generate_pkce_pair,
    parse_callback_url,
    save_token_state,
    TokenState,
)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("oauth_login")


async def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI Codex OAuth 登录")
    parser.add_argument(
        "--provider",
        default="chatgpt-plus",
        help="provider 名称（用于 token 文件名，默认 chatgpt-plus）",
    )
    args = parser.parse_args()

    # 1. 生成 PKCE 参数
    code_verifier, code_challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    # 2. 构建授权 URL
    auth_url = build_auth_url(code_challenge, state)

    print()
    print("=" * 60)
    print("  OpenAI Codex OAuth 登录")
    print("=" * 60)
    print()
    print("  1. 在浏览器中打开以下链接：")
    print()
    print(f"     {auth_url}")
    print()
    print("  2. 登录 ChatGPT 并完成授权")
    print("  3. 授权后浏览器会跳到 localhost 页面（报错没关系）")
    print("  4. 复制浏览器地址栏的完整 URL 粘贴到这里")
    print()
    print("-" * 60)

    callback_url = input("  > ").strip()

    # 3. 从回调 URL 提取 code
    code = parse_callback_url(callback_url)
    if not code:
        print()
        print("  ❌ 无法从 URL 中提取 authorization code，请检查粘贴的 URL 是否完整")
        sys.exit(1)

    print()
    print("  正在交换 token...")

    try:
        data = await exchange_code_for_token(code, code_verifier)
    except Exception as e:
        print(f"  ❌ Token 交换失败: {e}")
        sys.exit(1)

    # 4. 保存 token
    import time

    state_obj = TokenState(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=time.time() + data.get("expires_in", 86400),
    )

    project_root = Path.cwd()
    # 确认我们在 macchiatoBot 项目根（有 config/config.yaml）
    if not (project_root / "config" / "config.yaml").exists():
        print("  ⚠ 未找到 config/config.yaml，将 token 保存在当前目录")
        auth_dir = project_root / "data" / "oauth"
    else:
        auth_dir = project_root / "data" / "oauth"

    file_path = auth_dir / f"{args.provider}.json"
    save_token_state(file_path, state_obj)

    print(f"  ✅ 授权成功！token 已保存至 {file_path}")
    print()
    print(f"  使用前请确保 config.yaml 中有对应的 provider 配置：")
    print()
    print(f"    llm:")
    print(f"      active: \"{args.provider}\"")
    print(f"      providers:")
    print(f"        {args.provider}:")
    print(f"          protocol: \"codex_oauth\"")
    print(f"          model: \"gpt-5.5\"")
    print(f"          base_url: \"https://api.openai.com/codex/v1\"")
    print(f"          auth_file: \"./data/oauth/{args.provider}.json\"")
    if "gpt-5.5" not in str(data):
        print(f"          capabilities:")
        print(f"            context_window: 400000")
    print()


if __name__ == "__main__":
    asyncio.run(main())
