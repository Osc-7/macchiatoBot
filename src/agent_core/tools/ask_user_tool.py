"""ask_user：向用户展示多道选择题（含「其他」自由填写），阻塞直到作答或超时。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import agent_core.config as _config_mod
from agent_core.permissions.ask_user_registry import (
    AskUserBatchDecision,
    notify_ask_user_pending,
    register_ask_user_wait,
)
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class AskUserTool(BaseTool):
    """挂起当前 turn，直到 resolve_ask_user 或超时。"""

    @property
    def name(self) -> str:
        return "ask_user"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""当需要与用户确认偏好、消歧或收集选择时使用（类似 Cursor 的 Ask User）。

一次调用可包含**多道**题目；每道题须给出 `prompt` 与若干 `options`（字符串列表）。前端会额外展示一项「其他 / 自由填写」：若用户意图不在选项中，可填写自定义说明。

调用后进程会阻塞，直到用户在前端提交全部题目的答案或超时。返回的 data.answers 中每题包含 `question_id`、`selected_option`（选中 agent 选项时）与/或 `custom_text`（自由填写时，可与选项互斥）。""",
            parameters=[
                ToolParameter(
                    name="questions",
                    type="array",
                    description=(
                        "题目列表。每项为对象：id（可选，缺省为 q1,q2,…）、"
                        "prompt（必填，题干）、options（必填，至少一个非空字符串）。"
                        "同一批次内 id 须唯一。"
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="custom_option_label",
                    type="string",
                    description='前端用于「自由填写」入口的文案，默认「其他（请填写具体说明）」',
                    required=False,
                ),
                ToolParameter(
                    name="timeout_seconds",
                    type="number",
                    description="等待秒数，默认 600",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "两道题：部署区域与是否回滚",
                    "params": {
                        "questions": [
                            {
                                "id": "region",
                                "prompt": "部署到哪个区域？",
                                "options": ["cn-east", "cn-west", "海外"],
                            },
                            {
                                "id": "rollback",
                                "prompt": "失败时是否自动回滚？",
                                "options": ["是", "否"],
                            },
                        ],
                    },
                },
            ],
            usage_notes=[
                "仅在确实需要用户决策时调用；避免与对话中已明确的信息重复提问",
                "选项应互斥且覆盖常见情况；无法用选项表达时用户可选用自由填写",
                "须一次性给出本批所有题目，返回后会得到全部题目的答案",
            ],
            tags=["交互", "澄清"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        questions_raw = kwargs.get("questions")
        custom_label = str(kwargs.get("custom_option_label") or "").strip()
        timeout_s = kwargs.get("timeout_seconds")
        try:
            timeout = float(timeout_s) if timeout_s is not None else 600.0
        except (TypeError, ValueError):
            timeout = 600.0

        try:
            batch_id, fut, payload_items = register_ask_user_wait(questions_raw)
        except ValueError as exc:
            return ToolResult(
                success=False,
                error="INVALID_ARGUMENTS",
                message=str(exc),
            )

        cfg = _config_mod.get_config()
        _ = cfg  # 保留与 request_permission 一致的可扩展位

        payload: Dict[str, Any] = {
            "questions": payload_items,
            "custom_option_label": custom_label or "其他（请填写具体说明）",
            "timeout_seconds": timeout,
            "memory_owner": exec_ctx.get("memory_owner"),
            "session_id": exec_ctx.get("session_id"),
            "source": exec_ctx.get("source"),
            "user_id": exec_ctx.get("user_id"),
        }
        feishu_cid = str(exec_ctx.get("feishu_chat_id") or "").strip()
        if feishu_cid:
            payload["feishu_chat_id"] = feishu_cid

        notify_ask_user_pending(batch_id, payload)

        try:
            decision: AskUserBatchDecision = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="ASK_USER_TIMEOUT",
                message="等待用户作答超时",
                data={"ask_user_id": batch_id},
            )
        except asyncio.CancelledError as exc:
            return ToolResult(
                success=False,
                error="ASK_USER_CANCELLED",
                message=str(exc),
                data={"ask_user_id": batch_id},
            )

        answers_out: List[Dict[str, Any]] = []
        for a in decision.answers:
            d: Dict[str, Any] = {"question_id": a.question_id}
            if a.selected_option:
                d["selected_option"] = a.selected_option
            if a.custom_text:
                d["custom_text"] = a.custom_text
            answers_out.append(d)

        return ToolResult(
            success=True,
            message="已收到用户作答。",
            data={
                "ask_user_id": batch_id,
                "answers": answers_out,
            },
        )
