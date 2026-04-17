"""
对话上下文管理

管理多轮对话的消息历史。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
    ) -> None:
        """
        添加用户消息。

        Args:
            content: 消息文本内容
            media_items: 可选，多模态内容（如图片 image_url），与文本合并为一条消息
        """
        if media_items:
            parts: List[Dict[str, Any]] = [{"type": "text", "text": content}]
            parts.extend(media_items)
            self._add_message({"role": "user", "content": parts})
        else:
            self._add_message({"role": "user", "content": content})

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        reasoning_content: Optional[str] = None,
        anthropic_message_content: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        添加助手消息。

        Args:
            content: 文本内容（可选）
            tool_calls: 工具调用列表（可选）
            reasoning_content: 模型推理/思考文本（部分厂商多轮工具调用需原样回传）
            anthropic_message_content: Anthropic Messages 的 content 块（thinking/tool_use 等），
                扩展思考多轮工具时由 provider 填充并原样回传。
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

    def _add_message(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)

    def __len__(self) -> int:
        """返回消息数量"""
        return len(self.messages)
