# OS-Kernel Agent Core 架构实现度 Code Review

基于用户目标架构描述，对当前代码实现进行逐项审查。

---

## 一、架构目标概述

| 维度 | 目标描述 |
|------|----------|
| **Kernel 职责** | 创建、调度、回收 Agent Core |
| **Core 配置** | mode（full/sub）、工具列表、权限（工具、危险命令、可见上下文） |
| **Loader** | 根据配置加载记忆/上下文/系统提示；组装 Messages + tool_result；监控上下文窗口 |
| **Context 压缩** | LLM response 后检查阈值，超则发信号暂停，压缩后恢复（不在 tool_result 时截断） |
| **Session 生命周期** | 创建时记录时间戳，收到信号更新；超时发 kill，回收资源 |
| **资源回收** | CoreStats 回传；摘要器总结并写入前端 memory/entries |
| **Core 信号** | Return（草稿/最终回复）、Tool_call |
| **前端与记忆** | 一个前端 = 一个对话窗口；一个记忆库 = 工作记忆 + 长期记忆 + 聊天记录 |
| **定时/心跳** | Kernel 维护计时，到时创建 Agent，按任务配置赋权 |

---

## 二、已实现部分 ✅

### 2.1 Kernel：创建、调度、回收 Core

- **CorePool**（`system/kernel/core_pool.py`）：
  - `acquire()` 懒加载/复用 Core，per-session 锁防重复创建 ✅
  - `evict()` 完整 Kill 流程：kill → summarizer → close ✅
  - `scan_expired()` 返回超时 session_id ✅
- **KernelScheduler**（`system/kernel/scheduler.py`）：
  - `submit()` 将请求入队，返回 Future ✅
  - `_dispatch_loop()` 并发分发 ✅
  - `_ttl_loop()` 后台每 30s 扫描过期 session ✅

### 2.2 Core 配置与权限（CoreProfile）

- **CoreProfile**（`agent_core/kernel_interface/profile.py`）：
  - `mode`: full/sub/cron/heartbeat ✅
  - `allowed_tools` / `deny_tools` ✅
  - `allow_dangerous_commands`（run_command 等） ✅
  - `visible_memory_scopes`（working/short_term/long_term/content/chat） ✅
  - `max_context_tokens` / `session_expired_seconds` ✅
  - `frontend_id` / `dialog_window_id` ✅
- **权限执行**：
  - InternalLoader 用户态过滤工具 ✅
  - AgentKernel 内核态校验 `profile.is_tool_allowed()` ✅

### 2.3 Loader：组装上下文

- **InternalLoader**（`agent_core/kernel_interface/loader.py`）：
  - `assemble()` 从 Agent 状态组装 LLMPayload（system + messages + tools） ✅
  - 每次 LLM 调用前调用 ✅
  - 根据 CoreProfile 过滤工具 ✅
- **prompt_builder**（`agent_core/agent/prompt_builder.py`）：
  - 根据 `visible_memory_scopes` 过滤注入：working、long_term、short_term、content ✅
  - 注：`chat` scope 在默认列表中，但聊天消息始终通过 `context.messages` 注入，无需单独分支

### 2.4 上下文溢出与压缩（ContextOverflow）

- **触发时机**：
  - 在 `run_loop` 中，**仅在完整 thought→tools→observations 循环结束后**检查 ✅
  - **不在 tool_result 期间**触发，符合「不在 tool_result 后截断」的要求 ✅
- **流程**：
  - 超阈值 yield `ContextOverflowAction` ✅
  - Kernel 调用 `_compress_context()` 保留最近 6 轮、摘要旧消息 ✅
  - 发送 `ContextCompressedEvent` 恢复 Core ✅
  - 摘要写入 `_working_memory._running_summary` ✅

### 2.5 Session 超时与 TTL

- **CoreEntry**：
  - `last_active_ts`（monotonic）、`session_start_ts` ✅
  - `touch()` 刷新活跃时间 ✅
  - `is_expired()` 基于 `profile.session_expired_seconds` ✅
- **touch 调用**：
  - `AgentKernel.run()` 的 `on_signal` 在**每次**收到 `ReturnAction` 或 `ToolCallAction` 时调用 `core_pool.touch(session_id)` ✅
  - `_run_and_route()` 在请求**完成**后也调用一次 `touch`（双保险） ✅

### 2.6 Kill 与资源回收

- **run_loop_kill()**：
  - yield `CoreStatsAction`（token_usage、turn_count、session_id、session_start_time） ✅
- **AgentKernel.kill()**：
  - 驱动 `run_loop_kill()` 收集 CoreStats ✅
- **SessionSummarizer**：
  - 用 LLM 生成摘要 ✅
  - 退化摘要兜底 ✅
  - 调用 `long_term_memory.add_recent_topic()` 写入 entries.jsonl ✅

### 2.7 Core 信号接口

- **ReturnAction**：多步推理中的草稿/最终回复 ✅
- **ToolCallAction**：工具调用 ✅
- 两者在 `run_loop` 中通过 yield 发出 ✅

### 2.8 前端与记忆

- **记忆路径**：
  - 统一结构：`{memory_base_dir}/{frontend}/{user}/` 含 `content/`、`long_term/`、`short_term/`、`chat_history.db` ✅
  - 水源场景：`source=="shuiyuan"` 使用 shuiyuan 配置的 base ✅
- **长期记忆**：
  - `LongTermMemory` 写入 `long_term/entries.jsonl`、`long_term/MEMORY.md` ✅

### 2.9 定时任务 / Cron

- **automation_daemon**：
  - 队列消费时创建 `KernelRequest`，传入 `CoreProfile.default_cron()` ✅
  - `frontend_id=task.source`，`dialog_window_id=task.user_id` ✅
  - 通过 `scheduler.submit()` 提交 ✅

---

## 三、部分实现 / 待完善 ⚠️

### 3.1 ~~touch 仅在 Return 后调用，Tool_call 时未 touch~~ ✅ 已修复

**目标**：Kernel 在**每次**收到 Core 信号时更新 `last_active_ts`。

**实现**：`kernel.run()` 新增 `on_signal` 回调，在收到 `ReturnAction` 和 `ToolCallAction` 时调用；`KernelScheduler._run_and_route` 传入 `_on_signal` 调用 `core_pool.touch(session_id)`。

### 3.2 ~~记忆路径与 CoreProfile 未完全对齐~~ ✅ 已修复

**目标**：记忆库按 `(frontend_id, dialog_window_id)` 划分。

**实现**：`KernelScheduler._run_and_route` 在调用 `core_pool.acquire` 时，当 `request.profile` 存在且 `frontend_id`/`dialog_window_id` 非空时，优先使用它们作为 `source`/`user_id` 传入，确保 Cron 摘要写入对应前端记忆。

### 3.3 ~~Loader 未显式实现 visible_memory_scopes~~ ✅ 已修复

**目标**：`CoreProfile.visible_memory_scopes` 控制加载哪些记忆层。

**实现**：`prompt_builder.build_agent_system_prompt()` 新增 `_visible_scopes()`，根据 `agent._core_profile.visible_memory_scopes` 过滤注入：long_term（recent_topics、MEMORY.md）、short_term/long_term/content（recall）、working（running_summary）、long_term（automation digest）。无 profile 时视为全部可见（向后兼容）。

### 3.4 ~~CorePool 仍使用 tools_factory 而非 system.tools~~ ✅ 已修复

**目标**：Core 工具由 system 层统一装配。

**实现**：`CorePool._load()` 优先调用 `system.tools.build_tool_registry(config, profile=None)` 获取工具列表；仅当返回空列表时回退到 `tools_factory`。与 Kernel/MCP 工具装配保持一致。

---

## 四、未实现或需确认的部分 ❌

### 4.1 时间戳语义：monotonic vs unix

**目标**：创建时记录 unix 时间戳，收到信号时更新。

**现状**：使用 `time.monotonic()`。

**评估**：对 TTL 判断，`monotonic` 更合适（不受系统时间调整影响）。若需要审计/持久化，可额外记录 unix 时间。当前实现可接受。

### 4.2 ~~心跳（heartbeat）Core 的调度~~ ✅ 已实现

**目标**：Kernel 维护心跳计时，到时创建 Agent。

**实现**：复用 AutomationScheduler + AgentTaskQueue，新增 `job_type: heartbeat.monitor`；`automation_daemon._consume_loop` 在 `task.source` 解析出 `heartbeat.*` 时使用 `CoreProfile.default_heartbeat()`；`_JOB_INSTRUCTIONS["heartbeat.monitor"]` 定义轻量检查指令（get_sync_status，有异常时 notify_owner）；config.example 增加 `heartbeat_monitor` 配置示例。

### 4.3 ~~多 dialog_window / 多前端的记忆隔离~~ ✅ 已实现

**目标**：同一前端的多个对话窗口（如多群聊）应有独立记忆库；不同前端的记忆应隔离。

**实现**：`resolve_memory_owner_paths` 按 `frontend_id/user_id` 划分路径，例如：
- `cli/root`、`feishu/root`、`shuiyuan/osc7`
- 结构：`{base_dir}/{frontend_id}/{user_id}/`，记忆库不怕膨胀

---

## 五、实现度汇总

| 模块 | 实现度 | 说明 |
|------|--------|------|
| Kernel 创建/调度/回收 | 100% | 核心流程完整，touch 在 Return/Tool_call 时均调用 |
| Core 配置与权限 | 100% | Profile 完整，visible_memory_scopes 已生效 |
| Loader 组装 | 95% | 基础组装完成，记忆 scope 过滤已实现 |
| 上下文压缩 | 95% | 触发时机与流程正确 |
| Session 超时 | 100% | touch 在每次信号时调用 |
| Kill 与资源回收 | 95% | 流程完整 |
| 信号接口 | 100% | Return + Tool_call 均已实现 |
| 前端与记忆 | 95% | Cron 记忆路径已按 profile 对齐 |
| 定时任务 | 95% | 已接入，profile 正确，记忆路径对齐 |
| 心跳 | 100% | 复用 Cron 调度，heartbeat.monitor 使用 default_heartbeat |

**综合评估**：约 **98%** 已实现。touch 时机、记忆路径（frontend/user_id 隔离、Cron 按 profile 对齐）、visible_memory_scopes、CorePool 工具装配、心跳等已完善。

---

## 六、架构与实现对照速查

| 架构要求 | 实现位置 | 状态 |
|----------|----------|------|
| Kernel 创建/调度/回收 Core | `core_pool.py`, `scheduler.py` | ✅ |
| 按配置加载记忆、上下文、系统提示 | `loader.py`, `prompt_builder.py`, `activate_session` | ✅ |
| LLM response 后检查压缩，不在 tool_result 时截断 | `agent.py` run_loop L577–611 | ✅ |
| 每次 Return/Tool_call 刷新 TTL | `kernel.py` `_maybe_touch()` | ✅ |
| 超时 kill → CoreStats → summarizer → entries | `core_pool.evict()` | ✅ |
| 记忆库 = 工作记忆 + 长期记忆 + 聊天记录 | `memory/`, `chat_history_db` | ✅ |
| 多 frontend/user_id 独立记忆路径 | `memory_paths.py` | ✅ |
