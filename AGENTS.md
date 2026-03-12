---
description: 
alwaysApply: true
---

# 开发规范

本文档定义了 macchiato 项目的开发规范，基于 Anthropic 和 OpenAI 的官方指南以及业界最佳实践整理。

---

## 项目概述

macchiatoBot 是一个基于大语言模型的ai助手，采用**工具驱动（Tool-driven）**架构设计。

### 核心目标

- 通过自然语言交互管理日程
- 支持多轮对话和上下文理解
- 提供智能规划建议
- 可扩展的插件系统


## MANDATORY: Agent 工作流程

**每次会话必须遵循以下流程：**

### Step 1: 初始化环境

```bash
source init.sh
```

这会：
- 安装必要依赖
- 验证 Python 环境

**不要跳过这一步！** 确保环境正确后再继续。

### Step 2: 了解项目状态

1. **确认工作目录**: `pwd` 应该是项目根目录
2. **查看 git 历史**: `git log --oneline -10`
3. **读取进度文件**: `cat claude-progress.txt`
4. **读取功能列表**: `cat feature_list.json`

### Step 3: 选择下一个任务

从 `feature_list.json` 中选择一个 `passes: false` 的任务；或者在用户提出需求时，按照用户的需求开发。

选择优先级：
1. 高优先级任务 (`priority: "high"`)
2. 基础依赖任务（其他任务依赖它）
3. 最小未完成任务

### Step 4: 实现任务

- 仔细阅读任务的 `steps` 列表
- 按步骤逐一实现
- 遵循现有代码风格和架构
- 每完成一个步骤可以运行测试验证

### Step 5: 测试验证

**强制测试要求：**

1. **核心功能修改**：
   - 分批运行测试（主要为保证测试不超时）
   - 确保所有测试通过

2. **新增功能**：
   - 编写对应的测试用例
   - 运行测试确保通过

3. **所有修改必须**：
   - 代码可以正常 import
   - 没有语法错误
   - 测试全部通过

### Step 6: 更新进度

在 `claude-progress.txt` 中记录：

```markdown
## [日期] - 任务: [任务ID和描述]

### 完成内容:
- [具体的代码改动]
- [新增的文件或功能]

### 测试:
- [如何验证]
- [测试结果]

### 备注:
- [任何需要注意的事项]
- [后续可能需要的改进]
```

### Step 7: 通知用户更改


---

## 重要规则

1. **一个会话只完成一个任务** - 专注于把一件事做好
2. **测试通过才标记完成** - 只有所有步骤验证通过后才设置 `passes: true`
3. **永不删除任务** - 只能改变 `passes` 状态
4. **单次提交** - 代码、progress、feature_list 在同一个 commit
5. **阻塞时停止** - 遇到无法解决的问题，记录后停止，不要提交

---

## 阻塞处理 (Blocking Issues)

### 需要停止任务并请求人工帮助的情况：

1. **缺少环境配置**：
   - 需要填写真实的 API 密钥
   - 需要外部服务配置

2. **外部依赖不可用**：
   - 第三方 API 服务异常
   - 需要人工授权

3. **测试无法进行**：
   - 依赖未完成的功能
   - 需要特定硬件环境

### 阻塞时的正确操作：

**禁止：**
- ❌ 提交 git commit
- ❌ 将 task.json 的 passes 设为 true
- ❌ 假装任务已完成

**必须：**
- ✅ 在 progress.txt 中记录当前进度和阻塞原因
- ✅ 输出清晰的阻塞信息
- ✅ 停止任务，等待人工介入
---


## 功能列表格式

```json
{
  "features": [
    {
      "category": "context",
      "id": "CTX-001",
      "description": "时间上下文注入",
      "steps": [
        "实现 TimeContext 类",
        "在 Agent 中注入时间上下文",
        "编写测试用例"
      ],
      "passes": false,
      "priority": "high"
    }
  ]
}
```

**更新规则**：
- 只能修改 `passes` 字段 (`false` → `true`)
- 禁止删除或修改其他字段
- 禁止删除已完成的任务



## Agent 设计规范

### 1. Agent 循环模式

遵循 Anthropic 推荐的简单循环模式：

```python
class LLMAgentV2:
    async def process_input(self, user_input: str) -> str:
        # 1. 添加用户消息到上下文
        self.context.add_user_message(user_input)

        # 2. Agent 主循环
        iteration = 0
        while iteration < self.max_iterations:
            iteration += 1

            # 2.1 调用 LLM
            response = await self.llm_client.chat_with_tools(
                system_message=self._build_system_prompt(),
                messages=self.context.messages,
                tools=self.tool_registry.get_all_definitions(),
            )

            # 2.2 处理工具调用
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    result = await self.tool_registry.execute(
                        tool_call.function.name,
                        **tool_call.function.arguments
                    )
                    self.context.add_tool_result(tool_call.id, result.to_json())
                continue

            # 2.3 返回最终响应
            return response.content

        return "处理超时"
```

### 2. 上下文管理

#### 时间上下文

每次 LLM 调用必须注入准确的当前时间：

```python
def _build_system_prompt(self) -> str:
    time_ctx = get_time_context(self.timezone)
    return f"""
## 当前时间上下文
{time_ctx.to_prompt_string()}
"""
```

## 工具系统规范

### 1. 工具定义格式

遵循 OpenAI Function Calling 格式：

```python
@dataclass
class ToolDefinition:
    name: str                    # 工具名称（动词+名词）
    description: str             # 详细描述
    parameters: List[ToolParameter]  # 参数列表
    examples: List[Dict]         # 使用示例
    usage_notes: List[str]       # 使用注意事项
```

### 2. 工具描述最佳实践

**每个工具都必须包含：**

1. **清晰的功能描述** - 说明工具做什么
2. **使用场景说明** - 何时使用此工具
3. **参数详细说明** - 每个参数的类型、格式、默认值
4. **示例用法** - 至少 2-3 个真实场景示例
5. **注意事项** - 重要的使用提示
示例：
```python
ToolDefinition(
    name="create_schedule",
    description="""创建新的日程安排。

这是最常用的工具,当用户想要:
- 添加新日程/任务/会议
- 安排某个活动
- 设置提醒事项

工具会自动:
- 解析自然语言时间(如"明天下午3点")
- 检测时间冲突并提示
- 设置合理的默认值""",
    parameters=[
        ToolParameter(
            name="title",
            type="string",
            description="日程标题,简洁明了地描述这个日程",
            required=True
        ),
        # ... 更多参数
    ],
    examples=[
        {
            "description": "创建明天下午的团队会议",
            "params": {
                "title": "团队周会",
                "start_time": "明天下午3点",
            }
        }
    ],
    usage_notes=[
        "时间解析支持中文自然语言,不需要精确格式",
        "如果用户没有明确说结束时间,可以不填 end_time",
    ]
)
```

### 3. 工具返回格式

返回结构化的 `ToolResult`：

```python
@dataclass
class ToolResult:
    success: bool           # 是否成功
    data: Any              # 返回数据
    message: str           # 人类可读的消息
    error: Optional[str]   # 错误代码
    metadata: Dict         # 额外元数据
```

### 4. 错误处理

工具应该：
- 验证输入参数
- 提供有意义的错误信息
- 尽可能提供替代方案

```python
async def execute(self, **kwargs) -> ToolResult:
    try:
        # 验证参数
        if not kwargs.get("title"):
            return ToolResult(
                success=False,
                data=None,
                message="缺少日程标题",
                error="MISSING_TITLE"
            )

        # 执行逻辑
        result = await self._do_something(kwargs)

        return ToolResult(
            success=True,
            data=result,
            message=f"成功创建日程: {result.title}"
        )

    except Exception as e:
        return ToolResult(
            success=False,
            data=None,
            message=f"创建失败: {str(e)}",
            error="CREATE_ERROR"
        )
```

## 参考资源

### 官方指南

- [Anthropic: Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: Writing Effective Tools for AI Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [OpenAI: A Practical Guide to Building Agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)

### 设计模式

- Tool-driven Architecture
- Repository Pattern
- Strategy Pattern (for LLM providers)
