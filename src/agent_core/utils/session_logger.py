"""
Session 日志记录器

记录从客户端启动到退出的完整会话日志，包含：
- 用户与智能体的所有对话
- 每轮 LLM 调用的请求/响应
- 工具调用的入参、出参、耗时
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.llm import LLMResponse, ToolCall
from agent_core.tools import ToolResult


@dataclass
class SessionLogger:
    """
    会话日志记录器。

    以 JSONL 格式记录完整会话，每行一条 JSON 记录。
    """

    log_dir: str = "./logs/sessions"
    """日志目录"""

    enable_detailed_log: bool = False
    """是否启用详细模式（记录完整 prompt）"""

    max_system_prompt_log_len: int = 2000
    """详细模式下 system prompt 截断长度"""

    _file_path: Optional[Path] = field(default=None, repr=False)
    _session_id: Optional[str] = field(default=None, repr=False)
    _turn_count: int = field(default=0, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        # 使用本地时间生成文件名；时区由全局 TZ 配置控制（在 config.load_config 中统一设置）。
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_id = f"sess-{int(time.time())}"
        self._file_path = Path(self.log_dir) / f"session-{ts}.jsonl"

    def _write_record(self, record: Dict[str, Any]) -> None:
        """追加一条记录到日志文件"""
        if self._closed or self._file_path is None:
            return
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(line)

    def _timestamp(self) -> str:
        """ISO 8601 时间戳（本地时区，精确到毫秒）。"""
        dt = datetime.now().astimezone()
        return dt.isoformat(timespec="milliseconds")

    def on_session_start(self) -> None:
        """会话开始"""
        self._write_record(
            {
                "event": "session_start",
                "timestamp": self._timestamp(),
                "session_id": self._session_id,
            }
        )

    def on_user_message(self, turn_id: int, content: str) -> None:
        """用户消息"""
        self._write_record(
            {
                "event": "user_message",
                "timestamp": self._timestamp(),
                "turn_id": turn_id,
                "content": content,
            }
        )

    def on_llm_request(
        self,
        turn_id: int,
        iteration: int,
        message_count: int,
        tool_count: int,
        system_prompt_len: int = 0,
        system_prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """LLM 请求"""
        record: Dict[str, Any] = {
            "event": "llm_request",
            "timestamp": self._timestamp(),
            "turn_id": turn_id,
            "iteration": iteration,
            "message_count": message_count,
            "tool_count": tool_count,
            "system_prompt_len": system_prompt_len,
        }
        if self.enable_detailed_log:
            if system_prompt is not None:
                if len(system_prompt) > self.max_system_prompt_log_len:
                    record["system_prompt"] = (
                        system_prompt[: self.max_system_prompt_log_len] + "..."
                    )
                else:
                    record["system_prompt"] = system_prompt
            if messages is not None:
                record["messages"] = messages
        self._write_record(record)

    def on_llm_response(
        self,
        turn_id: int,
        iteration: int,
        response: LLMResponse,
    ) -> None:
        """LLM 响应"""
        tool_calls = []
        for tc in response.tool_calls:
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {"_raw_preview": args[:500] + ("...(截断)" if len(args) > 500 else "")}
            row: Dict[str, Any] = {
                "id": tc.id,
                "name": tc.name,
                "arguments": args,
            }
            if getattr(tc, "extra_content", None):
                row["extra_content"] = tc.extra_content
            tool_calls.append(row)
        record: Dict[str, Any] = {
            "event": "llm_response",
            "timestamp": self._timestamp(),
            "turn_id": turn_id,
            "iteration": iteration,
            "content": response.content,
            "tool_calls": tool_calls,
            "finish_reason": response.finish_reason,
        }
        if response.usage:
            record["usage"] = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        self._write_record(record)

    def on_tool_call(
        self,
        turn_id: int,
        iteration: int,
        tool_call: ToolCall,
    ) -> None:
        """工具调用"""
        args = tool_call.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                # 解析失败时仅记录预览，避免超长原始 JSON 刷屏
                args = {"_raw_preview": args[:500] + ("...(截断)" if len(args) > 500 else "")}
        self._write_record(
            {
                "event": "tool_call",
                "timestamp": self._timestamp(),
                "turn_id": turn_id,
                "iteration": iteration,
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "arguments": args,
            }
        )

    def _serialize_for_json(self, data: Any) -> Any:
        """递归序列化为可 JSON 序列化的数据结构（与 ToolResult._serialize_data 一致）"""
        if data is None:
            return None
        if isinstance(data, datetime):
            return data.isoformat()
        if hasattr(data, "model_dump"):
            try:
                # Pydantic v2: mode="json" 可将 datetime/enum 等转换为 JSON 兼容值
                return data.model_dump(mode="json")
            except TypeError:
                return self._serialize_for_json(data.model_dump())
        if hasattr(data, "to_dict"):
            return self._serialize_for_json(data.to_dict())
        if isinstance(data, list):
            return [self._serialize_for_json(item) for item in data]
        if isinstance(data, dict):
            return {k: self._serialize_for_json(v) for k, v in data.items()}
        if isinstance(data, (str, int, float, bool)):
            return data
        return str(data)

    def on_tool_result(
        self,
        turn_id: int,
        iteration: int,
        tool_call_id: str,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        """工具结果"""
        data_serialized = (
            self._serialize_for_json(result.data) if result.data is not None else None
        )
        self._write_record(
            {
                "event": "tool_result",
                "timestamp": self._timestamp(),
                "turn_id": turn_id,
                "iteration": iteration,
                "tool_call_id": tool_call_id,
                "success": result.success,
                "message": result.message,
                "data": data_serialized,
                "error": result.error,
                "duration_ms": duration_ms,
            }
        )

    def on_assistant_message(self, turn_id: int, content: str) -> None:
        """最终助手回复"""
        self._write_record(
            {
                "event": "assistant_message",
                "timestamp": self._timestamp(),
                "turn_id": turn_id,
                "content": content,
            }
        )

    def on_session_end(
        self,
        turn_count: int,
        total_usage: Optional[Dict[str, int]] = None,
    ) -> None:
        """会话结束"""
        record: Dict[str, Any] = {
            "event": "session_end",
            "timestamp": self._timestamp(),
            "turn_count": turn_count,
        }
        if total_usage:
            record["total_usage"] = total_usage
        self._write_record(record)

    def close(self) -> None:
        """关闭日志，停止写入"""
        self._closed = True

    @property
    def file_path(self) -> Optional[Path]:
        """当前日志文件路径"""
        return self._file_path
