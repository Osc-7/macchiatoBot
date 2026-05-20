#!/usr/bin/env python3
"""
macchiatoBot MCP Server 入口（stdio）。
"""

import asyncio
import sys
from pathlib import Path
from agent_core.config import get_config
from frontend.mcp_server import run_stdio_server

# 确保直接运行 `python mcp_server.py` 时也能导入 src 下的包。
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    config = get_config()
    asyncio.run(run_stdio_server(config=config))


if __name__ == "__main__":
    main()
