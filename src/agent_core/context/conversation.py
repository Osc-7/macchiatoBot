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
    """

    messages: List[Dict[str, Any]] = field(default_factory=list)
    """消息列表"""

    max_messages: int = 100
    """最大消息数量（超出时会裁剪旧消息）"""

    def add_user_message(self, content: str) -> None:
        """
        添加用户消息。

        Args:
            content: 消息内容
        """
        self._add_message({"role": "user", "content": content})

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        添加助手消息。

        Args:
            content: 文本内容（可选）
            tool_calls: 工具调用列表（可选）
        """
        message: Dict[str, Any] = {"role": "assistant"}

        if content is not None:
            message["content"] = content

        if tool_calls is not None:
            message["tool_calls"] = tool_calls

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
        """
        添加消息并检查数量限制。

        Args:
            message: 消息对象
        """
        self.messages.append(message)

        # 如果超出限制，移除旧消息（保留第一条系统消息）
        # 必须按「块」裁剪，避免产生孤立的 tool 消息（API 要求 tool 必须紧接在 assistant+tool_calls 之后）
        if len(self.messages) > self.max_messages:
            system_messages = [m for m in self.messages if m.get("role") == "system"]
            other_messages = [m for m in self.messages if m.get("role") != "system"]
            keep_count = self.max_messages - len(system_messages)
            other_messages = self._trim_preserving_tool_blocks(other_messages, keep_count)
            self.messages = system_messages + other_messages

    @staticmethod
    def _trim_preserving_tool_blocks(
        messages: List[Dict[str, Any]], keep_count: int
    ) -> List[Dict[str, Any]]:
        """
        裁剪消息列表，保持 tool 调用块完整。
        tool 消息必须紧接在 assistant+tool_calls 之后，否则 API 报错。
        """
        if len(messages) <= keep_count:
            return messages
        # 按块分割：user | (assistant + tool_calls + 后续 tool 结果)
        blocks: List[List[Dict[str, Any]]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            role = m.get("role", "")
            if role == "user":
                blocks.append([m])
                i += 1
            elif role == "assistant":
                block = [m]
                i += 1
                if m.get("tool_calls"):
                    while i < len(messages) and messages[i].get("role") == "tool":
                        block.append(messages[i])
                        i += 1
                blocks.append(block)
            elif role == "tool":
                # 孤立的 tool（不应出现），单独成块便于丢弃
                blocks.append([m])
                i += 1
            else:
                blocks.append([m])
                i += 1
        def _is_incomplete_block(block: List[Dict[str, Any]]) -> bool:
            """assistant+tool_calls 后必须有 tool 消息，否则为不完整块"""
            first = block[0] if block else {}
            return (
                first.get("role") == "assistant"
                and bool(first.get("tool_calls"))
                and len(block) == 1
            )

        def _is_orphan_tool_block(block: List[Dict[str, Any]]) -> bool:
            """孤立的 tool 块：tool 消息必须紧接 assistant+tool_calls 之后，否则 API 报错"""
            return block and block[0].get("role") == "tool"

        # 保留最后若干块，使总条数 <= keep_count，且不以孤立 tool 开头
        # 跳过不完整块（assistant+tool_calls 无 tool 结果）、孤立 tool 块
        result: List[Dict[str, Any]] = []
        for block in reversed(blocks):
            if _is_incomplete_block(block) or _is_orphan_tool_block(block):
                continue
            if len(result) + len(block) > keep_count:
                break
            result = block + result
        # 若开头是孤立的 tool 块（理论上已跳过，双重保险）
        while result and result[0].get("role") == "tool":
            result = result[1:]
        return result

    def __len__(self) -> int:
        """返回消息数量"""
        return len(self.messages)
