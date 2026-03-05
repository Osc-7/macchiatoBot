#!/usr/bin/env python3
"""
Feishu integration HTTP server.

使用 FastAPI 提供飞书事件回调入口，将消息转发给 automation daemon。

运行方式示例：

    source init.sh
    uvicorn feishu_server:app --host 0.0.0.0 --port 8001

或直接：

    python feishu_server.py
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from schedule_agent.config import get_config
from schedule_agent.frontend.feishu.router import router as feishu_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feishu_server")

app = FastAPI(title="Schedule Agent Feishu Gateway")


@app.on_event("startup")
async def _on_startup() -> None:  # pragma: no cover - 轻量启动逻辑
    cfg = get_config()
    if not cfg.feishu.enabled:
        logger.warning("Feishu integration is disabled (feishu.enabled=false). Server will still start but return 503 for callbacks.")
    else:
        logger.info("Feishu integration enabled. Base URL=%s domain=%s", cfg.feishu.base_url, cfg.feishu.domain)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """基础健康检查端点。"""
    return JSONResponse({"status": "ok"})


app.include_router(feishu_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("feishu_server:app", host="0.0.0.0", port=8001, reload=False)

