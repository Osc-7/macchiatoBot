# MULTI-AGENT - 多会话与 Subagent 协作

本架构**原生多会话**：每个 `session_id` 在 Kernel 里对应一个独立 **AgentCore**（独立上下文与 `CoreProfile`），由 **KernelScheduler** 调度——跨会话并行、同一会话串行。父子关系与对等 P2P 共用同一套 `inject_turn` 与 **AgentMessage** 信封（常量见代码包 `system.multi_agent`）。

### 架构一览


| 概念       | 实现要点                                                                                                                        |
| -------- | --------------------------------------------------------------------------------------------------------------------------- |
| 会话 / 对等体 | `session_id`（如 `feishu:user:…`、`sub:<uuid>`、`cli:root`）                                                                     |
| 「进程」     | `CorePool` 中 `CoreEntry` + `AgentCore`，权限由 `CoreProfile.mode`（如 `full` / `sub`）约束                                           |
| 调度       | `KernelScheduler`：多 session **并发**，同一 session **串行**（避免上下文竞争）                                                               |
| 父子委托     | `create_subagent` → 子跑完后系统向父 **inject** 完成通知（**不是**子用 `send_message` 汇报）                                                    |
| 对等 P2P   | `send_message_to_agent` / `reply_to_message` → `inject_turn` + 信封；同步回复依赖双方按协议调工具                                            |
| **进程表**  | `list_agents(scope=…)`：本 Kernel 进程内会话快照（内存态）；`my_children` 仅自己的子，`namespace` 同根命名空间，`siblings` 同父兄弟；用于查找 `session_id` 再 P2P |


**子任务完成 vs 回收**：子终态后系统先把完成/失败文案放入 **暂存区**，仅在父会话 **kernel 侧无并发请求** 或 **本轮 `_run_and_route` 结束** 时再 `inject_turn`（类比 OS 延后信号到安全点）；阻塞式 `**wait_subagent` 开始**时会丢弃对应暂存，终态只经工具返回。**complete 后**父仍可 `send_message_to_agent` 多轮协作，或再 `**reap_subagent`** 收尾；二者独立，不必完成即 reap。

```mermaid
flowchart LR
  Q[KernelScheduler 队列]
  P[CorePool]
  Q --> P
  P --> M[主会话 feishu/cli mode=full]
  P --> C[子会话 sub:… mode=sub]
  M <-->|inject_turn P2P / 完成通知| Q
  C <-->|inject_turn P2P / 完成通知| Q
```



### 收到 Agent 间消息：必须用 `reply_to_message` 回复（硬性）

看到 `[来自 [{sender}]] 的消息（message_id=…，需要回复）]` 这类注入，说明**另一个 Agent 正在同步阻塞等你的工具返回值**（不是用户闲聊）。

- **直接输出自然语言，对方收不到**：该请求 `frontend_id=agent_msg`，飞书 / 水源 / CLI 都不投递你的最终文本，它只落在你自己的 session log；对方只会等到 `P2P_REPLY_TIMEOUT`。
- **对方拿到的回复只有 `reply_to_message` 的 `content`**：必须调用 `reply_to_message(correlation_id="<message_id>", sender_session_id="<sender>", content="…")`。
- 你同时写给「用户」的内容（论坛回帖、飞书答复）**不会**顺带传回对方，两者是两条独立通道。
- 答不上来 / 需要更多信息也照样回一条，沉默只会让对方超时拿到空错误；`require_reply=false` 的通知类消息不需要回复。

当任务可以拆分或并行时，可使用以下工具委托子 Agent 处理：

### 工具速查


| 工具                          | 使用场景                                                                                                 |
| --------------------------- | ---------------------------------------------------------------------------------------------------- |
| `create_subagent`           | 派生单个后台子任务；立即返回，不等待结果                                                                                 |
| `create_parallel_subagents` | 派生多个并行后台子任务；谁先完成谁先通知                                                                                 |
| `list_agents`               | **进程表**：按 `scope` 列出可见会话（含 `session_id`、父指针、状态）；寻址 P2P 前优先调用，避免猜 id                                  |
| `get_subagent_status`       | **只读**查看子任务状态；`include_full_result=true` 时拉取完整输出（不收割、不删盘）；**仅创建该子的父会话**可查询                           |
| `reap_subagent`             | **父侧必做**：子任务已结束（含 cancel）且你已不再需要其工作区时 **必须** 调用；回收 zombie、删除对应隔离工作区（legacy 目录或 Linux home）、释放内存；**仅父会话**可调用                        |
| `send_message_to_agent`     | 向任意 session 发送 P2P 消息；**子 Agent 仅用于向父询问**，不用于汇报完成                                                    |
| `reply_to_message`          | 回复收到的 query 消息（correlation_id 关联）                                                                    |
| `cancel_subagent`           | **终止**正在运行的子 Agent（不可逆；**不删盘**；无论 legacy 或 Linux home 工作区都需另调 `reap_subagent` 才会删除）；**仅父会话**可调用                   |
| `wait_subagent`             | 父会话等待子终态：`**subagent_ids` 一次多个**；`wait_mode` 为 any 或 all；`wnohang=true` 为 **WNOHANG** 快照不阻塞；不代替 reap |
| `wait_for_agent_message`    | 子 Agent **阻塞**等待下一条 P2P 消息发往本会话                                                                      |


### 收割义务（必做）

- 每创建一个 `subagent_id`，在父会话侧**处理完该子任务**（已用预览或 `get` 拿到所需内容、不再需要其隔离工作区下的文件，无论是 legacy 目录还是 Linux home）后，**必须**调用 `reap_subagent(subagent_id=...)`。
- **不要**依赖「系统会自动清理」：zombie 在内存中**不会**随会话 TTL 自动回收；不 reap 会长期占用 zombie 表与磁盘工作区。
- 典型顺序：**按需** `get_subagent_status`（只读全文）→ **务必** `reap_subagent`（收尾）。并行多路：对**每一个**已结束的分支分别 reap（含已 `cancel_subagent` 的分支，若不再需要其目录）。
- 若仍需从子工作区 `read_file` 拷贝文件，**先拷贝再 reap**；reap 后无法再查询该 id。
- **已 reap 的子任务**：若完成/失败通知因调度排队晚于 reap，**系统不会再向父会话注入**该条生命周期通知（与「进程已回收不再投递信号」一致）；`get_subagent_status` 对仍记得的 id 会返回 `status=reaped` 以区别于从未创建。
- `**wait_subagent`**：可传 `**subagent_ids` 并行等多子**；`**wait_mode=any`** 任一终态即返回（类似等到任意子进程结束），`**all**` 则全部终态；`**wnohang=true**` 不阻塞，立即返回各子快照并带 `**condition_met**`（类比 WNOHANG）。默认超时常与 `agent.subagent_wait_timeout_seconds` 一致。
- `**wait_for_agent_message**`（子 Agent）：可阻塞等待下一条发往本会话的 P2P 消息。
- `**create_subagent` 未传 `allowed_tools**`：子 Agent **默认继承父会话**的工具模板与白/黑名单；需最小权限时请显式传入 `allowed_tools` 收窄。

### 使用原则

- `create_subagent` / `create_parallel_subagents` 适合**可独立执行、耗时较长、不需要实时交互**的任务（如「整理这份报告的关键数据」「搜索并总结某主题近期新闻」）；同一问题要多角度分析或 A/B 比较时用并行版，拿到满意结果即可 cancel 其余。
- `create_subagent` 只负责**创建**，不是同步拿结果；结果等系统通知后再决定是否 `get`。
- `context` 里必须写清「完成后父 Agent 的下一步计划」（例：`"完成后将结果整合进技术分析报告第三节"`），这样父 Agent 从 checkpoint 恢复后能理解期望。

### 典型工作流（Notify-and-Pull）

```
# 1. 创建并行子任务（立即返回，不阻塞）
create_parallel_subagents(tasks=[{task: "从技术角度分析...", context: "汇总到主报告"}, ...])
# 2. 收到完成通知：[子任务 id1 完成] + 结果预览（前 200 字符，不是全文）
# 3. 预览不够 → 只读拉全文（不收割）
get_subagent_status(subagent_id="id1", include_full_result=True)   # → data.result
# 4. 必做：结果取用完毕、不再需要子工作区 → reap（含已 cancel 的分支）
reap_subagent(subagent_id="id1")
# 5. 若已有足够结果，其余分支 cancel_subagent（不删盘）后仍须 reap
# 6. 子 Agent 向父询问用 send_message_to_agent；完成信号由系统推送，不要用它汇报
# 7. 收到 query 用 reply_to_message(correlation_id=…, sender_session_id=…, content=…)
```

### 权限说明

- 子 Agent（mode="sub"）默认只有 `send_message_to_agent` 和 `reply_to_message`（系统自动注入），
不能再创建子 Agent（防止无限递归）
- 父 Agent 指定 `allowed_tools` 时，系统会自动合并上述通信工具
- **完成信号**：子 Agent 完成后由系统自动推送，子 Agent **切勿**用 send_message_to_agent 汇报完成，否则重复通知
- **send_message_to_agent**（子 Agent）：仅用于向父**询问**任务细节、实现要求、澄清歧义；**默认会阻塞等待**父侧 `reply_to_message`，仅单向通知时须显式 `require_reply=false`
- **权限审批走父会话 Broker**：子 Agent 不应直接 `request_permission`；遇到权限不足时，必须先 `send_message_to_agent(require_reply=true)` 向父会话提交审批请求，由父会话统一决定是否调用 `request_permission` 并回传执行指令
- **get** 与 **reap** 分工：`get_subagent_status` 只读；`reap_subagent` 才是 `waitpid/reap`（回收 zombie、删子工作区）
- `**reap` 是父侧义务**：每个子任务在结束前都应 reap；未完成则勿 reap（会失败）。
- `**cancel_subagent` 不删盘**：被取消的子任务终态仍为 `cancelled`，**仍须**按需 `reap_subagent` 释放隔离工作区目录（legacy 或 Linux home，规则与完成/失败分支一致）

### 消息来源区分（重要）

对话中可能出现多种来源的消息，**必须正确区分**：


| 消息格式                                     | 来源                     | 处理方式                                                                                          |
| ---------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------- |
| 普通自然语言（无特殊前缀）                            | **用户**                 | 响应用户需求                                                                                        |
| `[子任务 {subagent_id} 完成]` 开头              | **子 Agent 完成通知（系统注入）** | 预览 ≠ 全文；需要时用 `get_subagent_status(..., include_full_result=True)`；**处理完毕后必须 `reap_subagent`** |
| `[来自 [{session_id}] 的消息]` 或 `[来自 X 的回复]` | **其他 Agent**           | 处理来自其他 Agent 的汇报或回复                                                                           |


**切勿将子任务完成通知误认为完整结果或用户输入**：

- 通知中的「结果预览」只是前 200 字符，**不是完整输出**
- 若需要完整输出才能继续，必须主动调用 `get_subagent_status(include_full_result=True)` 只读拉取
- 收割（删 zombie PCB、删对应隔离工作区：legacy `data/workspace/subagent/<id>/` 或 Linux home 子目录）**必须**通过 `reap_subagent` 完成，与只读 `get` 分开
- 若预览已足够、无需 `get` 全文，**仍须在收尾时对对应 `subagent_id` 调用 `reap_subagent`**（除非你还在使用该子工作区内的文件）
