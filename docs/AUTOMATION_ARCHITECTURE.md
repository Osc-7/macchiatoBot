# Automation 架构（2026-03）

## 目标

- 将 session 过期与摘要触发统一收敛到 automation 常驻进程。
- CLI 仅做交互层，不再在本地做过期检查。
- 支持多终端共享会话视图与上下文延续。

## 组件

1. `automation_daemon.py`
- 长期运行进程。
- 包含：调度器 + 队列消费者 + IPC Server。

2. `AutomationScheduler` + `AgentTaskQueue`
- 定时任务写入持久化队列。
- 消费者用 `SessionManager` 执行任务。

3. `AutomationCoreGateway`
- 对接 interactive 会话能力（session/run_turn/context/token）。
- 维护 session 活跃时间并执行过期切分（idle + 4am）。

4. `AutomationIPCServer` / `AutomationIPCClient`
- 本地 Unix Socket IPC。
- CLI 优先连接 daemon；不可用时回退本地直连模式。

## 会话过期路径

1. IPC Server 后台 loop 周期检查 `gateway.should_expire_session(session_id)`。
2. 命中后执行 `gateway.expire_session(...)`。
3. 网关内部执行：`finalize_session -> reset_session`。
4. 摘要写入 `recent_topic`（由 Agent finalize 逻辑完成）。

## CLI 职责边界

- 保留：输入输出、流式显示、session 命令。
- 移除：本地过期检查（before_user_turn / timer）。

## “已通过 automation 指令执行”的链路确认

当前已具备两类：

1. 调度任务指令化执行（队列驱动）
- `summary.daily` / `summary.weekly` / `sync.course` / `sync.email`
- 由 scheduler 生成自然语言 instruction 入队，Agent 执行并回写结果。

2. 动态自定义定时任务
- `create_scheduled_job` 可写入 `instruction`。
- scheduler 读取 job 定义后同样走队列 + Agent 指令执行。

补充：
- `sync_sources`、`get_digest` 等工具可被 Agent 直接调用（工具路径）。
- 上述能力与指令化任务执行可以并存。

