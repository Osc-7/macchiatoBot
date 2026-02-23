"""
工具基类和定义

定义工具系统的核心接口和数据结构。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolParameter:
    """
    工具参数定义。

    遵循 OpenAI Function Calling 参数格式。
    """

    name: str
    """参数名称"""

    type: str
    """参数类型（string, number, integer, boolean, array, object）"""

    description: str
    """参数描述"""

    required: bool = True
    """是否必需"""

    enum: Optional[List[str]] = None
    """枚举值列表（可选）"""

    default: Optional[Any] = None
    """默认值（可选）"""

    def to_json_schema(self) -> Dict[str, Any]:
        """
        转换为 JSON Schema 格式。

        Returns:
            JSON Schema 字典
        """
        schema: Dict[str, Any] = {
            "type": self.type,
            "description": self.description,
        }

        if self.enum:
            schema["enum"] = self.enum

        return schema


@dataclass
class ToolDefinition:
    """
    工具定义。

    遵循 OpenAI Function Calling 格式，包含：
    - 工具名称和描述
    - 参数定义
    - 使用示例
    - 注意事项
    - 标签（用于搜索和分类）
    """

    name: str
    """工具名称（动词 + 名词格式，如 create_event, get_tasks）"""

    description: str
    """详细描述，包括功能说明和使用场景"""

    parameters: List[ToolParameter] = field(default_factory=list)
    """参数列表"""

    examples: List[Dict[str, Any]] = field(default_factory=list)
    """使用示例"""

    usage_notes: List[str] = field(default_factory=list)
    """使用注意事项"""

    tags: List[str] = field(default_factory=list)
    """标签列表，用于工具搜索和分类"""

    def to_openai_tool(self) -> Dict[str, Any]:
        """
        转换为 OpenAI Function Calling 格式。

        Returns:
            OpenAI 工具定义字典
        """
        properties = {}
        required = []

        for param in self.parameters:
            properties[param.name] = param.to_json_schema()
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self._build_description(),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def _build_description(self) -> str:
        """
        构建完整的工具描述。

        包含基础描述、使用示例和注意事项。

        Returns:
            完整的描述字符串
        """
        parts = [self.description]

        if self.examples:
            parts.append("\n\n示例用法:")
            for i, example in enumerate(self.examples, 1):
                desc = example.get("description", f"示例 {i}")
                params = example.get("params", {})
                parts.append(f"\n{i}. {desc}")
                if params:
                    parts.append(f"   参数：{json.dumps(params, ensure_ascii=False)}")

        if self.usage_notes:
            parts.append("\n\n注意事项:")
            for note in self.usage_notes:
                parts.append(f"- {note}")

        return "\n".join(parts)


@dataclass
class ToolResult:
    """
    工具执行结果。

    统一的工具返回格式，包含：
    - 执行状态
    - 返回数据
    - 人类可读的消息
    - 错误信息（如果有）
    """

    success: bool
    """是否成功"""

    data: Any = None
    """返回数据"""

    message: str = ""
    """人类可读的消息"""

    error: Optional[str] = None
    """错误代码（如果有）"""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """额外元数据"""

    def to_json(self) -> str:
        """
        转换为 JSON 字符串。

        Returns:
            JSON 字符串
        """
        result = {
            "success": self.success,
            "message": self.message,
        }

        if self.data is not None:
            result["data"] = self._serialize_data(self.data)

        if self.error:
            result["error"] = self.error

        if self.metadata:
            result["metadata"] = self.metadata

        return json.dumps(result, ensure_ascii=False, default=str)

    def _serialize_data(self, data: Any) -> Any:
        """
        序列化数据。

        Args:
            data: 要序列化的数据

        Returns:
            可 JSON 序列化的数据
        """
        if hasattr(data, "model_dump"):
            return data.model_dump()
        elif isinstance(data, list):
            return [self._serialize_data(item) for item in data]
        elif isinstance(data, dict):
            return {k: self._serialize_data(v) for k, v in data.items()}
        return data


class BaseTool(ABC):
    """
    工具基类。

    所有工具都需要继承此类并实现 get_definition 和 execute 方法。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""
        pass

    @abstractmethod
    def get_definition(self) -> ToolDefinition:
        """
        获取工具定义。

        Returns:
            工具定义对象
        """
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """
        执行工具。

        Args:
            **kwargs: 工具参数

        Returns:
            工具执行结果
        """
        pass

    def to_openai_tool(self) -> Dict[str, Any]:
        """
        转换为 OpenAI Function Calling 格式。

        Returns:
            OpenAI 工具定义字典
        """
        return self.get_definition().to_openai_tool()
