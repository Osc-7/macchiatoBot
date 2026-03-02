# Schedule Agent

基于 LLM 的智能日程管理助手，采用**工具驱动**架构，支持自然语言交互、多轮对话与智能规划。

## 特性

- **自然语言交互**：用自然语言创建、查询、修改日程
- **工具驱动**：LLM 决策 + 工具执行，职责清晰
- **多 LLM 支持**：豆包、阿里云百炼 Qwen
- **可扩展工具**：日程、任务、时间解析、空闲时段、规划、文件读写、网页抓取
- **自动化骨架**：定时同步（课表/邮件 stub）、日/周总结、通知 Outbox、后台调度 Worker
- **MCP 客户端**：可按配置接入外部 MCP Server 并自动注册远程工具

## 快速开始

```bash
# 1. 初始化环境
source init.sh

# 2. 复制并编辑配置
cp config.example.yaml config.yaml
# 填写 llm.api_key 和 llm.model，或使用环境变量 QWEN_API_KEY / QWEN_MODEL

# 3. 运行
python main.py                    # 交互式
python main.py 明天下午3点开会    # 单条命令

# 启动自动化后台 Worker（定时同步/定时总结/通知链路）
python automation_worker.py
```

## 项目结构

```
src/schedule_agent/
├── core/           # 核心
│   ├── agent/      # Agent 主循环
│   ├── context/    # 对话与时间上下文
│   ├── llm/        # LLM 客户端（豆包/Qwen）
│   └── tools/      # 工具定义与注册表
├── models/         # 数据模型（Event、Task）
├── storage/        # JSON 持久化
├── prompts/        # 系统提示（可组合的 Markdown 片段）
├── cli/            # 命令行交互
└── config.py       # 配置加载
```

## 配置

主配置 `config.yaml`，关键项：

| 配置项 | 说明 |
|--------|------|
| `llm.provider` | `qwen` 或 `doubao` |
| `llm.api_key` | 可改用 `QWEN_API_KEY` / `DOUBAO_API_KEY` |
| `llm.model` | 如 `qwen3.5-plus` 或豆包推理端点 ID |
| `storage.data_dir` | 日程数据目录 |
| `file_tools.enabled` | 是否启用文件读写工具 |
| `mcp.enabled` | 是否启用 MCP 客户端 |
| `mcp.servers` | MCP Server 列表（stdio） |

详见 `config.example.yaml`。

## 开发

```bash
source init.sh
pytest tests/ -v
```

## 本地工具 MCP 化

项目内置了 MCP Server 入口，可将现有工具以 stdio 方式暴露：

```bash
python mcp_server.py
```

若要让 Agent 通过 MCP 调用你自己的本地工具，可在 `config.yaml` 中配置：

```yaml
mcp:
  enabled: true
  servers:
    - name: "schedule_tools"
      transport: "stdio"
      command: "python"
      args: ["mcp_server.py"]
      cwd: "."
      tool_name_prefix: "mcp_local"
```

开发规范见 [AGENTS.md](AGENTS.md)。
