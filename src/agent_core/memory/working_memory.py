"""
工作记忆 - 会话内滑动窗口（ConversationContext.messages + token 估算）

折叠摘要以 ``[会话进行中摘要]`` user 消息存在于窗口内；``running_summary`` 仅作
checkpoint / 会话结束总结等元数据，不再单独注入主 system。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agent_core.context.conversation import ConversationContext

_SESSION_SUMMARIZE_SYSTEM_PROMPT = """\
你是一个会话总结引擎。给定一整个会话的对话历史（包含用户消息、助手消息、工具调用及结果），
请输出一个结构化的 JSON 对象：
{
  "summary": "会话内容的详细摘要，尽可能完整，不遗漏每一句话的信息",
  "decisions": ["本次会话做出的关键决策列表"],
  "open_questions": ["会话结束时仍未解决的问题"],
  "referenced_files": ["对话中涉及/提到的文件路径列表"],
  "tags": ["关键词标签列表"]
}

只输出合法 JSON，不要包含 markdown 代码块标记或其他文本。使用中文。"""


def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数（中文约 1.5 字/token，英文约 4 字符/token）。"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4) + 1


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """估算消息列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            total += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False))
        total += 4  # role/name overhead
    return total


class WorkingMemory:
    """
    滑动窗口与 token 估算。

    会话正文在 ``context.messages``；``running_summary`` 与 Kernel 压缩同步，供持久化等使用。
    """

    def __init__(
        self,
        context: ConversationContext,
        max_tokens: int = 8000,
    ):
        self._context = context
        self._max_tokens = max_tokens
        self._running_summary: Optional[str] = None
        # 已完成上下文自总结（折叠）的次数；供压缩提示与续写 bundle 元信息使用
        self._compression_round: int = 0

    @property
    def context(self) -> ConversationContext:
        return self._context

    @property
    def running_summary(self) -> Optional[str]:
        """与最近折叠摘要同步的文本（元数据）；主对话以 messages 为准。"""
        return self._running_summary

    @running_summary.setter
    def running_summary(self, value: Optional[str]) -> None:
        self._running_summary = value

    @property
    def compression_round(self) -> int:
        """已完成的自总结（上下文折叠）次数。"""
        return self._compression_round

    @compression_round.setter
    def compression_round(self, value: int) -> None:
        self._compression_round = max(0, int(value))

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def get_current_tokens(self, actual_tokens: Optional[int] = None) -> int:
        """获取当前 token 数，优先使用 actual_tokens。"""
        if actual_tokens is not None and actual_tokens > 0:
            return actual_tokens
        return estimate_messages_tokens(self._context.get_messages())

    def should_compress(self, actual_tokens: Optional[int] = None) -> bool:
        """判断当前上下文是否需要 Kernel 侧压缩。"""
        return self.get_current_tokens(actual_tokens) >= self._max_tokens

    async def summarize_session(self, llm_client) -> Dict[str, Any]:
        """
        会话结束时总结整个对话，返回结构化摘要数据。

        Returns:
            包含 summary, decisions, open_questions, referenced_files, tags 的字典
        """
        messages = self._context.get_messages()
        if not messages:
            return {
                "summary": "空会话",
                "decisions": [],
                "open_questions": [],
                "referenced_files": [],
                "tags": [],
            }

        conversation_text = self._format_messages_for_summary(messages)
        if self._running_summary:
            conversation_text = (
                f"之前折叠的摘要：\n{self._running_summary}\n\n"
                f"最近对话：\n{conversation_text}"
            )

        response = await llm_client.chat(
            messages=[{"role": "user", "content": conversation_text}],
            system_message=_SESSION_SUMMARIZE_SYSTEM_PROMPT,
        )

        raw = (response.content or "").strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {
                "summary": raw,
                "decisions": [],
                "open_questions": [],
                "referenced_files": [],
                "tags": [],
            }

        for key in (
            "summary",
            "decisions",
            "open_questions",
            "referenced_files",
            "tags",
        ):
            result.setdefault(key, [] if key != "summary" else "")

        return result

    @staticmethod
    def _format_messages_for_summary(messages: List[Dict[str, Any]]) -> str:
        """将消息列表格式化为可读文本，供 LLM 总结。"""
        parts: List[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if role == "system" and "[会话进行中摘要]" in (content or ""):
                parts.append(f"[摘要] {content}")
                continue

            if role == "user":
                parts.append(f"用户: {content}")
            elif role == "assistant":
                if content:
                    parts.append(f"助手: {content}")
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        parts.append(
                            f"助手调用工具: {fn.get('name', '?')}({fn.get('arguments', '')})"
                        )
            elif role == "tool":
                tc_id = msg.get("tool_call_id", "?")
                parts.append(f"工具结果[{tc_id}]: {content[:500]}")
            else:
                parts.append(f"[{role}] {content}")

        return "\n".join(parts)
