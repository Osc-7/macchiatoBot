# 架构说明

本文档是当前 macchiatoBot 运行架构的主说明。根 README 只保留快速入口和主线，细节放在这里。

## 设计原则

- 项目是 daemon-first：长期状态属于 `macchiato-daemon` / `automation_daemon.py`，不属于某一次 CLI 调用。
- 前端只是适配层：CLI、飞书、MCP、自动化任务都通过 daemon IPC 或自动化队列进入。
- 推理与执行分离：`AgentCore` 负责 prompt 与 LLM；`AgentKernel` 负责工具执行、权限和运行时控制。
- 远程工作区只是部分工具的路由选择，不是一套并行 agent 架构。

## 包与入口

| 入口 | 源码 | 作用 |
|---|---|---|
| `macchiato` | `src/macchiato_bot_cli/main.py` | CLI IPC 客户端，支持交互与单条命令 |
| `macchiato-daemon` | `src/macchiato_bot_cli/daemon.py` | 常驻 daemon，管理会话、队列、IPC、调度与前端集成 |
| `macchiato-remote` | `src/macchiato_remote/cli.py` | worker CLI，将一个本机授权目录暴露给 daemon |
| `uv run main.py` | `main.py` | 仓库内运行 `macchiato` 的兼容入口 |
| `uv run automation_daemon.py` | `automation_daemon.py` | 仓库内运行 `macchiato-daemon` 的兼容入口 |
| `uv run mcp_server.py` | `mcp_server.py` | 本地 MCP stdio server |
| `uv run feishu_ws_gateway.py` | `feishu_ws_gateway.py` | 飞书 websocket 网关 |

## 请求链路

```text
Frontend / trigger
  - CLI
  - 飞书 gateway
  - MCP stdio server
  - 定时自动化任务
        |
        v
Automation 层
  - AutomationIPCServer / AutomationIPCClient
  - AutomationCoreGateway
  - AgentTaskQueue
  - AutomationScheduler
        |
        v
Kernel 层
  - KernelScheduler
  - CorePool
  - SessionSummarizer
        |
        v
AgentKernel
  - 工具执行
  - 权限检查
  - 路径授权
  - 上下文压缩
  - 输出路由
        |
        v
AgentCore
  - prompt 组装
  - working set 与工具选择
  - LLM provider 路由
  - 记忆召回与持久化
  - 多轮 tool-calling 循环
```

## 分层地图

| 层 | 主要模块 | 负责 | 不负责 |
|---|---|---|---|
| Frontend | `src/frontend/*`、根入口脚本 | 渠道解析、展示、渠道回调 | Agent 状态、直接工具执行 |
| Automation | `src/system/automation/*` | IPC、队列、任务定义、会话注册、调度 | LLM prompt 细节 |
| Kernel | `src/system/kernel/*` | Core 池、kernel 请求、terminal、总结压缩 | provider 选择细节 |
| Agent runtime | `src/agent_core/agent/*`、`src/agent_core/llm/*`、`src/agent_core/context/*` | Agent 循环、prompt、provider 适配、记忆/上下文状态 | 前端传输细节 |
| Tools | `src/agent_core/tools/*`、`src/system/tools/*`、`src/agent_core/mcp/*` | 工具定义、校验、执行行为、MCP 代理 | 发版打包 |
| Remote worker | `src/macchiato_remote/*`、`src/agent_core/remote/*` | 远程协议、worker 注册、工作区路由 | 完整 bot daemon 状态 |

## 工具执行

工具通过注册表暴露给 LLM，但是否可见、能否执行、路径是否允许、结果如何压缩，都由 Kernel 控制。

Kernel 是以下事项的权威：

- 当前 profile 下某个工具是否可见
- 路径或命令是否允许
- 工具应该在本地执行还是路由到远程工作区
- 大结果如何摘要或 overflow

这样 LLM 循环可以保持简单：它请求工具调用，Kernel 决定如何安全执行。

## 状态与生成文件

运行状态不进入版本库：

| 路径 | 用途 |
|---|---|
| `data/` | 应用数据、会话、自动化仓库 |
| `logs/` | daemon / gateway 日志 |
| `.macchiato/` | 工作区本地状态：job 日志、日记、本机 rules/skills、scratch |
| `dist/`、`build/`、`*.egg-info/` | 打包构建产物 |
| `.venv/`、`.pytest_cache/`、`__pycache__/` | 本地开发产物 |

`docs/` 下的文档是源码，应进入版本库。

## 新代码放在哪里

- 新前端行为：`src/frontend/<channel>/`。
- 新 daemon / session / queue 行为：`src/system/automation/`。
- 新调度或 core pool 行为：`src/system/kernel/`。
- 新 LLM 循环、prompt、上下文或记忆行为：`src/agent_core/`。
- 新用户可见工具：应用级工具优先放 `src/system/tools/`，低层核心工具放 `src/agent_core/tools/`。
- 远程 worker：worker 侧放 `src/macchiato_remote/`；daemon 侧路由放 `src/agent_core/remote/` 以及 kernel/tool 相关模块。
- 只与发版打包有关的变化：`pyproject.toml`、`packages/macchiato-remote/pyproject.toml`、`.github/workflows/`、`deploy/`。

## Release 边界

Release 提交不应该重新定义运行架构。它们只应处理：

- 版本号
- 包元数据
- CI / release workflow
- 安装与发版文档

如果某次发版需要运行时代码变化，先用正常 feature/fix 提交合入，再从稳定基线做一个小的 release 提交。
