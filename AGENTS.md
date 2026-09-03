---

## description:
alwaysApply: true

# 开发规范

本文档定义 macchiatoBot 的开发规范，面向在本仓库工作的 Agent。架构细节见 [docs/architecture_zh.md](docs/architecture_zh.md)；本文只保留必须遵守的工作流、分层边界和代码约定。

---

## 项目概述

macchiatoBot 是 **daemon-first、工具驱动** 的 LLM 助手。常驻 daemon 负责会话、调度、IPC、工具执行、权限、记忆和前端接入；CLI、飞书、Dashboard、MCP 与自动化任务都通过同一套运行时进入。

仓库有两个可安装入口：

| 包 | 作用 |
|---|---|
| `macchiato-bot` | 完整 bot 运行时（`macchiato`、`macchiato-daemon`、`macchiato-remote`、`macchiato-dashboard`） |
| `macchiato-remote` | 轻量 worker，只把本机授权目录暴露给远端 daemon |

根目录 `main.py`、`automation_daemon.py` 只是 `uv run` 兼容薄入口，实现在 `src/macchiato_bot_cli/`。

### 核心能力

- 多轮对话、斜杠命令（`/model`、`/session`、`/goal`、`/skill`、`/remote-*` 等）
- 工具驱动的文件、bash、记忆、日程、技能、子任务与外部集成
- Goal 跟踪、定时唤醒、后台 job 完成后 `inject_turn` 通知
- 远程工作区：云端 daemon 把部分工具路由到 `macchiato-remote` worker
- 多前端：CLI、飞书、Dashboard、MCP、水源、Canvas

## 运行架构

```text
CLI / 飞书 / Dashboard / MCP / 自动化触发
        |
        v
Automation IPC + Core Gateway + 任务队列
        |
        v
KernelScheduler + CorePool + SessionSummarizer
        |
        v
AgentKernel          # 工具执行、权限、路径授权、本地/远程路由、上下文压缩
        |
        v
AgentCore            # prompt、LLM provider、记忆召回、tool-calling 循环
```

一句话：**`AgentCore` 负责思考，`AgentKernel` 负责执行，automation daemon 负责常驻进程、会话、队列和 IPC。**

### 架构原则

- **Daemon-first**：长期状态属于 `macchiato-daemon` / `automation_daemon.py`，不属于某一次 CLI 调用。CLI 在 daemon 未运行时直接退出，不会偷偷拉起私有 agent。
- **前端保持轻量**：渠道只解析输入、展示输出、转发回调，不持有 Agent 状态，也不直接执行工具。
- **推理与执行分离**：`AgentCore` 持有 LLMClient，内部自旋完成多轮推理；只有工具 IO、上下文溢出和本轮结束才 yield 到 Kernel。
- **远程工作区是工具路由**：remote mode 只改变部分工具在哪里运行，不引入第二套 agent 架构。
- **不保留过时兼容层**：项目仍在快速迭代。更新旧功能时，直接把调用点改为新实现，不要为兼容性保留死代码。Release 提交只处理版本号、打包和发版文档。

### 分层与代码放置

| 层 | 路径 | 负责 | 不负责 |
|---|---|---|---|
| Frontend | `src/frontend/*` | 渠道解析、展示、回调 | Agent 状态、直接工具执行 |
| CLI / daemon 入口 | `src/macchiato_bot_cli/` | 打包后的 CLI 与 daemon 入口 | 业务逻辑本体 |
| Automation | `src/system/automation/` | IPC、队列、任务、会话注册、调度 | LLM prompt 细节 |
| Kernel | `src/system/kernel/` | Core 池、kernel 请求、terminal、总结压缩 | provider 选择细节 |
| Agent runtime | `src/agent_core/` | Agent 循环、prompt、provider、记忆/上下文 | 前端传输细节 |
| App tools | `src/system/tools/` | 用户可见应用级工具 | 发版打包 |
| Core tools | `src/agent_core/tools/` | bash、权限、ask_user、search/call_tool 等底层工具 | 渠道 UI |
| Remote worker | `src/macchiato_remote/`、`src/agent_core/remote/` | worker 协议、工作区路由 | 完整 bot daemon 状态 |

新代码放哪里：

- 新前端行为 → `src/frontend/<channel>/`
- 新 daemon / session / queue 行为 → `src/system/automation/`
- 新调度或 Core 池行为 → `src/system/kernel/`
- 新 LLM 循环、prompt、上下文或记忆 → `src/agent_core/`
- 新用户可见工具 → 应用级放 `src/system/tools/`，底层核心工具放 `src/agent_core/tools/`
- 远程 worker 侧 → `src/macchiato_remote/`；daemon 侧路由 → `src/agent_core/remote/` 以及 kernel/tool 相关模块
- 只与发版打包有关 → `pyproject.toml`、`packages/macchiato-remote/pyproject.toml`、`.github/workflows/`、`deploy/`

生成状态不进版本库：`data/`、`logs/`、`.macchiato/`、`.venv/`、`dist/`、`build/`、`*.egg-info/`。

## MANDATORY: Agent 工作流程

**每次会话必须遵循以下流程：**

### Step 1: 初始化环境

```bash
source init.sh
```

这会：同步依赖（`uv sync --all-groups`）、把 `.venv/bin` 加入 `PATH`、设置 `PYTHONPATH=src`、加载 `.env`。

**不要跳过这一步。** 确保环境正确后再继续。

### Step 2: 了解项目状态

1. **确认工作目录**：`pwd` 应该是项目根目录
2. **查看 git 历史**：`git log --oneline -10`
3. 改动前先定位所属分层（见上方「代码放置」），避免把逻辑写进错误的包

### Step 3: 实现任务

- 仔细阅读任务要求
- 按步骤实现，遵循现有代码风格和分层边界
- 每完成一个步骤可以运行相关测试验证
- 前端适配层不要直接调用工具或持有 Agent 状态；工具可见性、权限、路径授权由 Kernel 决定

### Step 4: 测试验证

**强制测试要求：**

1. **核心功能修改**：
   - 分批运行相关测试（避免全量超时）
   - 确保改动相关测试通过
2. **新增功能**：编写对应测试用例并跑通
3. **所有修改必须**：
   - 代码可以正常 import
   - 没有语法错误
   - 相关测试通过
4. **不需要保留老代码兼容性**：直接把项目中的依赖改为新版实现

测试命令见文末「常用命令」。全量 `pytest tests/` 可能因测试间状态泄漏出现偶发失败，相关文件单独跑通常通过；优先对改动文件分批验证。

## Agent 设计规范

### 1. Agent 循环模式

真实循环不是「CLI 直接 `chat_with_tools`」，而是 **Kernel 驱动 Core 的 async generator**：

- `AgentCore.run_loop()` 内部调用 LLM；需要外部 IO 时 `yield ToolCallAction`
- `AgentKernel.run()` 执行工具（含权限/路径/远程路由），再 `asend(ToolResultEvent)`
- 本轮结束 `yield ReturnAction`；上下文超限 `yield ContextOverflowAction`
- 回收前 `AgentKernel.kill()` 驱动 `run_loop_kill()`，收集 `CoreStatsAction`

```python
class AgentCore:
    async def run_loop(self, turn_id: int = 0, hooks=None):
        iteration = 0
        while iteration < self._max_iterations:
            iteration += 1
            response = await self._llm_client.chat_with_tools(
                system_message=build_agent_system_prompt(self),
                messages=self._context.messages,
                tools=self._tool_registry.get_all_definitions(),
            )
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    result = yield ToolCallAction(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.function.name,
                        arguments=tool_call.function.arguments,
                    )
                    self._context.add_tool_result(tool_call.id, result.to_json())
                continue
            yield ReturnAction(message=response.content)
            return
        yield ReturnAction(message="处理超时", status="error")
```

不要在 `AgentCore` 里直接 `tool_registry.execute()`。Kernel 是工具执行、权限检查、本地/远程路由和大结果处理的权威。

### 2. 上下文与 Prompt

每次 LLM 调用必须注入准确的当前时间。System prompt 由 `src/agent_core/agent/prompt_builder.py` 组装（`build_agent_system_prompt`），按 `CoreProfile.mode` 选择 full / minimal：

- `full` / `sub`：完整人设、Workspace 引导、技能索引
- `background`（cron / heartbeat）：minimal，省 token

工作记忆不再单独注入 system：会话状态即 `ConversationContext.messages` 滑动窗口（含 Kernel 折叠产生的摘要条）。长期记忆等进入「记忆上下文」。

记忆是四层：工作记忆、短期/会话、长期记忆、内容记忆（corpus）。召回与持久化在 `src/agent_core/memory/`；跨 session 的 owner 路径由 `memory_owner` 决定。

### 3. 会话、Goal 与子任务

- 会话由 daemon / CorePool 持有；切换、压缩、TTL 驱逐发生在 Kernel / automation 层
- Goal（`src/agent_core/goals/` + `src/system/tools/goal_tools.py`）是结构化多步计划，checkpoint 持久化；`/goal` 会 `inject_turn` 自动开跑
- Subagent 继承父级 `permission_mode`、workspace policy 和 `deny_tools`
- 后台 bash job 完成后由 daemon watcher `inject_turn` 推送通知（见 `command_tools.bash_job_notify_*`）

## 工具系统规范

### 1. 工具定义格式

遵循 OpenAI Function Calling 格式。定义在 `src/agent_core/tools/base.py`：

```python
@dataclass
class ToolDefinition:
    name: str                         # 动词+名词，如 write_file, memory_store
    description: str                  # 详细描述
    parameters: List[ToolParameter]
    examples: List[Dict]              # 使用示例
    usage_notes: List[str]            # 注意事项
    tags: List[str]                   # 供 search_tools 检索
```

工具通过 `VersionedToolRegistry` 暴露给 LLM，但 **Kernel 始终负责**：

- 当前 profile 下工具是否可见
- 路径或命令是否允许
- 在本地执行还是路由到远程工作区
- 大结果如何摘要 / overflow / 清扫陈旧 tool_result

### 2. 工具描述最佳实践

每个工具都必须包含：

1. **清晰的功能描述** — 说明工具做什么
2. **使用场景说明** — 何时使用
3. **参数详细说明** — 类型、格式、默认值
4. **示例用法** — 至少 2–3 个真实场景
5. **注意事项** — 重要约束（权限、路径、远程路由、副作用）

```python
ToolDefinition(
    name="memory_store",
    description="""将重要事实写入长期记忆。

当用户明确要求记住某事，或对话中出现会跨会话复用的稳定事实时使用。
不要把一次性指令或已完成的临时任务写进长期记忆。""",
    parameters=[
        ToolParameter(
            name="content",
            type="string",
            description="要记住的事实，简洁、可独立理解",
            required=True,
        ),
    ],
    examples=[
        {
            "description": "记住用户的时区偏好",
            "params": {"content": "用户日常使用 Asia/Shanghai 时区"},
        }
    ],
    usage_notes=[
        "只存储跨会话仍有价值的事实",
        "更新已有条目时优先用 memory_update，避免重复写入",
    ],
    tags=["memory", "long_term"],
)
```

### 3. 工具返回格式

返回结构化的 `ToolResult`：

```python
@dataclass
class ToolResult:
    success: bool                 # 是否成功
    data: Any                     # 返回数据
    message: str                  # 人类可读消息
    error: Optional[str]          # 错误代码
    metadata: Dict                # 额外元数据
```

LLM 侧完整上下文走 `to_json()`；trace / 飞书卡片可用 `data_preview_str()`（对含 stdout/stderr 的 shell 结果只展示流输出）。

### 4. 错误处理

工具应该：

- 验证输入参数
- 提供有意义的错误信息
- 尽可能提供替代方案

```python
async def execute(self, **kwargs) -> ToolResult:
    try:
        content = (kwargs.get("content") or "").strip()
        if not content:
            return ToolResult(
                success=False,
                data=None,
                message="缺少要写入的记忆内容",
                error="MISSING_CONTENT",
            )
        result = await self._do_something(content)
        return ToolResult(
            success=True,
            data=result,
            message="已写入长期记忆",
        )
    except Exception as e:
        return ToolResult(
            success=False,
            data=None,
            message=f"写入失败: {e}",
            error="STORE_ERROR",
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
- Daemon-first Runtime
- Repository Pattern
- Strategy Pattern（LLM providers）

### 专项文档

- [架构说明](docs/architecture_zh.md)
- [远程工作区](docs/remote-workspace_zh.md)
- [飞书接入](docs/feishu_zh.md)
- [部署 / systemd](deploy/README.md)
- [发版流程](deploy/RELEASING.md)

## 本地 / Cursor Cloud 开发说明

### 常用命令

| 操作 | 命令 |
|------|------|
| 安装依赖 | `uv sync --all-groups` |
| 运行测试 | `uv run pytest tests/ -v --tb=short` |
| 相关单测 | `uv run pytest tests/test_<area>.py -v --tb=short` |
| 代码格式检查 | `black --check src/ tests/` / `isort --check-only src/ tests/` |
| 启动 daemon | `uv run automation_daemon.py` |
| 启动 CLI | `uv run main.py` |
| 单条命令 | `uv run main.py "你的消息"` |
| 飞书网关 | `uv run feishu_ws_gateway.py` |
| MCP stdio | `uv run mcp_server.py` |
| Dashboard | `uv run macchiato-dashboard`（默认 `http://127.0.0.1:8765`） |

安装包之后也可用 `macchiato-daemon` / `macchiato` / `macchiato-dashboard` / `macchiato-remote`。

### 注意事项

- **Python >= 3.11** + **uv**，虚拟环境在 `.venv/`
- **PYTHONPATH** 必须包含 `src/`（`uv run` 会自动处理；直接 `python` 时需 `export PYTHONPATH=$(pwd)/src`）
- **`config/config.yaml` 和 `.env` 必须存在**才能启动 daemon。从 `.env.example` 和 `config/config.example.yaml` 复制。Provider 片段在 `config/llm/providers.d/*.yaml`
- 测试不依赖外部服务或 API key，可直接 `uv run pytest tests/` 跑相关文件
- daemon 启动后 socket 位于 `data/automation/automation.sock`；CLI 会自动找到它
- 没有 LLM API key 时 daemon 可正常启动，但 `run_turn` 会报 401；IPC ping、`model_list` 等管理命令不受影响
- bash 后台任务完成后由 daemon watcher 主动 `inject_turn` 推送通知，相关配置见 `command_tools.bash_job_notify_poll_seconds` / `bash_job_status_min_interval_seconds` / `bash_job_notify_inject_enabled`
- `source init.sh` 会设置 PATH、PYTHONPATH 并 source `.env`；非必需但推荐在交互 shell 中使用
- 飞书附件、远程 workspace 的 inbox 同步、blob 走 WS binary frame；改 remote 协议时同步看 `src/macchiato_remote/` 与 `src/agent_core/remote/`
- Dashboard 的 kernel_exec 等 API 有会话访问控制，改接口时不要绕过用户隔离
)
