"""
对话上下文管理

管理多轮对话的消息历史。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_SYNTH_INTERRUPTED_TOOL = "（该工具调用被中断或未在当轮执行完毕，无有效输出。）"


def _tool_call_ids_from_assistant_message(msg: Dict[str, Any]) -> List[str]:
    """OpenAI-format assistant.tool_calls -> stable list of string ids."""
    raw = msg.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for tc in raw:
        if isinstance(tc, dict):
            tid = tc.get("id")
            if tid is not None and str(tid).strip():
                out.append(str(tid).strip())
    return out


def repair_incomplete_assistant_tool_call_sequence(
    messages: List[Dict[str, Any]],
) -> int:
    """
    就地修复：紧跟在带 ``tool_calls`` 的 assistant 之后，为每个尚未回应的
    ``tool_call_id`` 插入一条合成 ``role=tool`` 消息。

    典型场景：内核任务被 ``terminal_cancel`` / IPC 断开 / 异常打断，assistant
    已写入上下文但工具结果未全部落库；下一轮 LLM 会 400（如 DeepSeek
    ``insufficient tool messages following tool_calls``）。

    Returns:
        插入的合成 tool 消息条数。
    """
    inserted = 0
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue
        required = _tool_call_ids_from_assistant_message(msg)
        if not required:
            i += 1
            continue
        j = i + 1
        found: set[str] = set()
        while j < n and messages[j].get("role") == "tool":
            tid = messages[j].get("tool_call_id")
            if tid is not None and str(tid).strip():
                found.add(str(tid).strip())
            j += 1
        missing = [tid for tid in required if tid not in found]
        if missing:
            insert_pos = j
            for tid in missing:
                messages.insert(
                    insert_pos,
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": _SYNTH_INTERRUPTED_TOOL,
                    },
                )
                insert_pos += 1
                inserted += 1
                n += 1
            i = insert_pos
        else:
            i = j
    return inserted


@dataclass
class ConversationContext:
    """
    对话上下文。

    管理多轮对话的消息历史，支持：
    - 添加用户消息
    - 添加助手消息
    - 添加工具调用结果
    - 导出为 LLM API 格式

    不限制消息条数；上下文体积由「token 上限 + Kernel 压缩」等机制控制。
    """

    messages: List[Dict[str, Any]] = field(default_factory=list)
    """消息列表"""

    def add_user_message(
        self,
        content: str,
        *,
        media_items: Optional[List[Dict[str, Any]]] = None,
        turn_id: Optional[int] = None,
    ) -> None:
        """
        添加用户消息。

        Args:
            content: 消息文本内容
            media_items: 可选，多模态内容（path/url 引用），与文本合并为一条消息
            turn_id: 可选，所属 turn；用于 API 请求时按 turn 临时注入二进制
        """
        message: Dict[str, Any] = {"role": "user"}
        if turn_id is not None:
            message["_turn_id"] = turn_id
        if media_items:
            from agent_core.agent.media_helpers import normalize_media_items_for_context

            parts: List[Dict[str, Any]] = [{"type": "text", "text": content}]
            parts.extend(normalize_media_items_for_context(media_items))
            message["content"] = parts
        else:
            message["content"] = content
        self._add_message(message)

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        reasoning_content: Optional[str] = None,
        anthropic_message_content: Optional[List[Dict[str, Any]]] = None,
        responses_reasoning_items: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        添加助手消息。

        Args:
            content: 文本内容（可选）
            tool_calls: 工具调用列表（可选）
            reasoning_content: 模型推理/思考文本（部分厂商多轮工具调用需原样回传）
            anthropic_message_content: Anthropic Messages 的 content 块（thinking/tool_use 等），
                扩展思考多轮工具时由 provider 填充并原样回传。
            responses_reasoning_items: Responses API reasoning item（含 encrypted_content）。
        """
        message: Dict[str, Any] = {"role": "assistant"}

        if content is not None:
            message["content"] = content

        if tool_calls is not None:
            message["tool_calls"] = tool_calls

        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content

        if anthropic_message_content is not None:
            message["anthropic_message_content"] = anthropic_message_content

        if responses_reasoning_items is not None:
            message["responses_reasoning_items"] = responses_reasoning_items

        self._add_message(message)

    def add_tool_result(
        self,
        tool_call_id: str,
        result: Any,
        is_error: bool = False,
    ) -> None:
        """
        添加工具调用结果。

        Args:
            tool_call_id: 工具调用 ID
            result: 工具返回结果
            is_error: 是否是错误结果
        """
        if isinstance(result, str):
            content = result
        elif hasattr(result, "to_json"):
            content = result.to_json()
        elif hasattr(result, "model_dump"):
            content = json.dumps(result.model_dump(), ensure_ascii=False)
        else:
            content = json.dumps(result, ensure_ascii=False)

        self._add_message(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }
        )

    def get_messages(self) -> List[Dict[str, Any]]:
        """
        获取消息列表。

        Returns:
            消息列表
        """
        return list(self.messages)

    def clear(self) -> None:
        """清空消息历史"""
        self.messages.clear()

    def repair_dangling_assistant_tool_calls(self) -> int:
        """见 `repair_incomplete_assistant_tool_call_sequence`；就地修改 ``messages``。"""
        return repair_incomplete_assistant_tool_call_sequence(self.messages)

    def _add_message(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)

    def __len__(self) -> int:
        """返回消息数量"""
        return len(self.messages)
