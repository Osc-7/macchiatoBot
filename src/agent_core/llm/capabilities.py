"""
Provider 能力声明。

每个 LLM provider 声明它对哪些 Chat Completions 扩展的支持，
由 LLMClient / AgentCore 在运行时据此决定：
- 是否允许塞 image_url 到消息里（vision）
- 是否发 tools/tool_choice 参数（function_calling）
- 是否把 reasoning_content 当独立字段回传（reasoning_content）
- 是否需要从 content 里剥离 <think>...</think>（thinking_tag_inline）
- context_window 的真实上限（覆盖基于模型名的启发式）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Capabilities:
    """Provider 能力矩阵。"""

    vision: bool = False
    """模型可以直接理解 image_url / video_url 内容项。"""

    function_calling: bool = True
    """支持 OpenAI function calling 格式（tools + tool_choice）。"""

    parallel_tool_calls: bool = True
    """支持在一次响应里返回多个 tool_calls。"""

    reasoning_content: bool = False
    """在 choices[0].message 上有独立的 reasoning_content 字段（DeepSeek 思考模式、GLM、Kimi 等）。"""

    thinking_tag_inline: bool = False
    """模型可能在 content 中输出 <think>...</think>（Qwen 深度思考等）。"""

    context_window: Optional[int] = None
    """模型上下文窗口 token 数；None 表示按模型名启发式推断。"""

    file_input_mime_types: Tuple[str, ...] = ()
    """模型原生支持的输入文件 MIME 列表；空表示不支持直接文件输入。"""
