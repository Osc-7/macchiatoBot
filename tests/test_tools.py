"""
工具系统测试
"""

import pytest

from agent_core.tools import (
    BaseTool,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolResult,
)


class MockTool(BaseTool):
    """模拟工具用于测试"""

    @property
    def name(self) -> str:
        return "mock_tool"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="mock_tool",
            description="一个模拟工具",
            parameters=[
                ToolParameter(
                    name="input",
                    type="string",
                    description="输入参数",
                    required=True,
                )
            ],
            examples=[{"description": "示例用法", "params": {"input": "测试"}}],
            usage_notes=["这是一个模拟工具"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        input_value = kwargs.get("input", "")
        return ToolResult(
            success=True,
            data={"result": input_value},
            message=f"处理完成: {input_value}",
        )


class ErrorTool(BaseTool):
    """模拟错误工具"""

    @property
    def name(self) -> str:
        return "error_tool"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="error_tool",
            description="一个总是失败的错误工具",
            parameters=[],
        )

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            success=False,
            error="MOCK_ERROR",
            message="模拟错误",
        )


class TestToolParameter:
    """测试 ToolParameter"""

    def test_parameter_creation(self):
        """测试参数创建"""
        param = ToolParameter(
            name="title",
            type="string",
            description="日程标题",
            required=True,
        )

        assert param.name == "title"
        assert param.type == "string"
        assert param.description == "日程标题"
        assert param.required is True

    def test_parameter_with_enum(self):
        """测试带枚举的参数"""
        param = ToolParameter(
            name="priority",
            type="string",
            description="优先级",
            required=False,
            enum=["low", "medium", "high"],
            default="medium",
        )

        assert param.enum == ["low", "medium", "high"]
        assert param.default == "medium"

    def test_to_json_schema(self):
        """测试转换为 JSON Schema"""
        param = ToolParameter(
            name="title",
            type="string",
            description="标题",
        )

        schema = param.to_json_schema()

        assert schema["type"] == "string"
        assert schema["description"] == "标题"

    def test_to_json_schema_with_enum(self):
        """测试带枚举的 JSON Schema"""
        param = ToolParameter(
            name="status",
            type="string",
            description="状态",
            enum=["active", "inactive"],
        )

        schema = param.to_json_schema()

        assert "enum" in schema
        assert schema["enum"] == ["active", "inactive"]


class TestToolDefinition:
    """测试 ToolDefinition"""

    def test_definition_creation(self):
        """测试定义创建"""
        definition = ToolDefinition(
            name="create_event",
            description="创建新日程",
            parameters=[
                ToolParameter(name="title", type="string", description="标题"),
            ],
        )

        assert definition.name == "create_event"
        assert len(definition.parameters) == 1

    def test_to_openai_tool(self):
        """测试转换为 OpenAI 格式"""
        definition = ToolDefinition(
            name="create_event",
            description="创建新日程",
            parameters=[
                ToolParameter(
                    name="title", type="string", description="标题", required=True
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="描述",
                    required=False,
                ),
            ],
        )

        openai_tool = definition.to_openai_tool()

        assert openai_tool["type"] == "function"
        assert openai_tool["function"]["name"] == "create_event"
        assert "parameters" in openai_tool["function"]
        assert "title" in openai_tool["function"]["parameters"]["properties"]
        assert "required" in openai_tool["function"]["parameters"]
        assert "title" in openai_tool["function"]["parameters"]["required"]

    def test_build_description_with_examples(self):
        """测试包含示例的描述"""
        definition = ToolDefinition(
            name="create_event",
            description="创建新日程",
            examples=[{"description": "创建会议", "params": {"title": "团队周会"}}],
            usage_notes=["请确保填写标题"],
        )

        full_desc = definition._build_description()

        assert "创建新日程" in full_desc
        assert "示例用法" in full_desc
        assert "创建会议" in full_desc
        assert "注意事项" in full_desc


class TestToolResult:
    """测试 ToolResult"""

    def test_success_result(self):
        """测试成功结果"""
        result = ToolResult(
            success=True,
            data={"id": "123"},
            message="创建成功",
        )

        assert result.success is True
        assert result.data == {"id": "123"}
        assert result.error is None

    def test_error_result(self):
        """测试错误结果"""
        result = ToolResult(
            success=False,
            error="MISSING_TITLE",
            message="缺少标题",
        )

        assert result.success is False
        assert result.error == "MISSING_TITLE"

    def test_to_json(self):
        """测试转换为 JSON"""
        result = ToolResult(
            success=True,
            data={"id": "123"},
            message="创建成功",
            metadata={"timestamp": "2026-02-17"},
        )

        json_str = result.to_json()

        assert '"success": true' in json_str
        assert '"message": "创建成功"' in json_str

    def test_to_json_with_pydantic_data(self):
        """测试转换 Pydantic 数据为 JSON"""

        class MockModel:
            def model_dump(self):
                return {"key": "value"}

        result = ToolResult(
            success=True,
            data=MockModel(),
            message="成功",
        )

        json_str = result.to_json()

        assert '"key": "value"' in json_str

    def test_data_preview_str_includes_structured_data(self):
        result = ToolResult(
            success=False,
            message="命令执行结束，返回码为 127",
            error="NON_ZERO_EXIT",
            data={
                "stderr": "/bin/bash: pip: command not found\n",
                "stdout": "",
                "return_code": 127,
            },
        )
        preview = result.data_preview_str(500)
        assert "pip" in preview
        assert "stderr" in preview
        # 飞书/trace 预览只展示流，不包含整段 data 里的 return_code 重复
        assert "return_code" not in preview


class TestBaseTool:
    """测试 BaseTool"""

    def test_tool_name(self):
        """测试工具名称"""
        tool = MockTool()
        assert tool.name == "mock_tool"

    def test_get_definition(self):
        """测试获取定义"""
        tool = MockTool()
        definition = tool.get_definition()

        assert definition.name == "mock_tool"
        assert len(definition.parameters) == 1

    def test_to_openai_tool(self):
        """测试转换为 OpenAI 格式"""
        tool = MockTool()
        openai_tool = tool.to_openai_tool()

        assert openai_tool["type"] == "function"
        assert openai_tool["function"]["name"] == "mock_tool"

    @pytest.mark.asyncio
    async def test_execute(self):
        """测试执行工具"""
        tool = MockTool()
        result = await tool.execute(input="测试输入")

        assert result.success is True
        assert result.data == {"result": "测试输入"}
        assert result.message == "处理完成: 测试输入"


class TestToolRegistry:
    """测试 ToolRegistry"""

    def test_empty_registry(self):
        """测试空注册表"""
        registry = ToolRegistry()

        assert len(registry) == 0
        assert registry.list_names() == []

    def test_register_tool(self):
        """测试注册工具"""
        registry = ToolRegistry()
        tool = MockTool()

        registry.register(tool)

        assert len(registry) == 1
        assert registry.has("mock_tool")
        assert "mock_tool" in registry

    def test_register_duplicate_tool(self):
        """测试注册重复工具"""
        registry = ToolRegistry()
        tool1 = MockTool()
        tool2 = MockTool()

        registry.register(tool1)

        with pytest.raises(ValueError, match="已注册"):
            registry.register(tool2)

    def test_unregister_tool(self):
        """测试注销工具"""
        registry = ToolRegistry()
        tool = MockTool()

        registry.register(tool)
        assert registry.has("mock_tool")

        result = registry.unregister("mock_tool")

        assert result is True
        assert not registry.has("mock_tool")

    def test_unregister_nonexistent_tool(self):
        """测试注销不存在的工具"""
        registry = ToolRegistry()

        result = registry.unregister("nonexistent")

        assert result is False

    def test_get_tool(self):
        """测试获取工具"""
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)

        retrieved = registry.get("mock_tool")

        assert retrieved is tool

    def test_get_nonexistent_tool(self):
        """测试获取不存在的工具"""
        registry = ToolRegistry()

        retrieved = registry.get("nonexistent")

        assert retrieved is None

    def test_get_definition(self):
        """测试获取工具定义"""
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)

        definition = registry.get_definition("mock_tool")

        assert definition is not None
        assert definition.name == "mock_tool"

    def test_get_all_definitions(self):
        """测试获取所有定义"""
        registry = ToolRegistry()
        registry.register(MockTool())
        registry.register(ErrorTool())

        definitions = registry.get_all_definitions()

        assert len(definitions) == 2
        names = [d["function"]["name"] for d in definitions]
        assert "mock_tool" in names
        assert "error_tool" in names

    def test_get_all_tools(self):
        """测试获取所有工具"""
        registry = ToolRegistry()
        registry.register(MockTool())
        registry.register(ErrorTool())

        tools = registry.get_all_tools()

        assert len(tools) == 2

    @pytest.mark.asyncio
    async def test_execute_tool(self):
        """测试执行工具"""
        registry = ToolRegistry()
        registry.register(MockTool())

        result = await registry.execute("mock_tool", input="测试")

        assert result.success is True
        assert result.data == {"result": "测试"}

    @pytest.mark.asyncio
    async def test_execute_nonexistent_tool(self):
        """测试执行不存在的工具"""
        registry = ToolRegistry()

        result = await registry.execute("nonexistent", input="测试")

        assert result.success is False
        assert result.error == "TOOL_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_execute_error_tool(self):
        """测试执行错误工具"""
        registry = ToolRegistry()
        registry.register(ErrorTool())

        result = await registry.execute("error_tool")

        assert result.success is False
        assert result.error == "MOCK_ERROR"
