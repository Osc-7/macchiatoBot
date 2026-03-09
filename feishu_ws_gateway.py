#!/usr/bin/env python3
"""
Feishu long-connection gateway.

使用飞书官方 Python SDK (lark-oapi) 通过 WebSocket 长连接接收事件，
并将文本消息转发给 Schedule Agent 的 automation daemon。

运行方式示例:

    source init.sh
    python feishu_ws_gateway.py

前置条件:
- config.yaml 中正确配置 llm 与 feishu 段:
  - feishu.enabled = true
  - feishu.app_id / feishu.app_secret 已填写或通过环境变量覆盖
- automation_daemon.py 已启动，IPC 监听正常

相比 HTTP Webhook 模式，长连接模式无需公网 IP / 域名 / ngrok，更适合本地开发。
"""

from __future__ import annotations

import logging
import sys

# 必须在其他导入前配置 logging，避免 lark-oapi 等库先初始化
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
# 确保 frontend.feishu 的 logger 输出到 stdout
logging.getLogger("frontend.feishu").setLevel(logging.INFO)

from frontend.feishu.ws_client import run_ws_client

logger = logging.getLogger("feishu_ws_gateway")


def main() -> None:
    try:
        run_ws_client()
    except KeyboardInterrupt:
        # 用户主动通过 Ctrl+C 终止进程时，优雅退出而不打印异常堆栈
        logger.info("Feishu ws gateway stopped by user (KeyboardInterrupt)")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Feishu ws gateway exited with error: %s", exc)
        raise


if __name__ == "__main__":
    main()

