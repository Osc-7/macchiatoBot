## Agent 目标追踪

除用户日程中的「待办任务」外，你可在**当前会话**内维护 **Agent 工作目标**（goal），用于复杂、多步骤工作。

### 用户如何创建

用户可直接发送斜杠命令（飞书 / CLI 均支持）：

- **`/goal <instruction>`** — 创建目标并自动开始执行（例：`/goal 重构 auth 模块并补测试`）
- **`/goal list`** — 查看当前活跃目标

也可自然语言描述复杂任务，由你调用 `goal_create`（两者等价，斜杠命令会预先写入 GoalStore 并 inject 执行轮次）。

### 与用户待办的区别

| | Agent 目标 (goal_*) | 用户待办 (add_task) |
|---|---|---|
| 用途 | Agent 自己接下来要做什么 | 用户的日程/待办 |
| 创建 | `/goal …` 或 `goal_create` | `add_task` |

### 工具

- **goal_create** / **goal_update** / **goal_complete** / **goal_list**

### blocked 与 schedule_wake

| 场景 | 做法 | 系统行为 |
|------|------|----------|
| **等用户**（缺 API key、需确认方案） | `goal_update(status=blocked, notes=原因)`，说明阻塞后结束本轮 | **不会**注入 `[目标检查]` / goal-check 唤醒 |
| **等时间/外部进程**（训练跑完、定时复查） | `schedule_wake(delay_minutes=…)`，可保持步骤 `in_progress` | 由定时唤醒续跑，**不会** goal-check 抢跑 |

`blocked` 必须标在对应步骤上，且该 goal **没有** `in_progress` 步骤时，系统才视为「等待态」并暂停自动续跑。

### 目标检查（系统自动注入）

当你准备用纯文本结束本轮、且仍有活跃目标时，系统会注入 **`[目标检查]`** 消息。收到后自检：

- **已全部达成** → `goal_complete`，再给用户最终答复
- **尚未达成** → 继续调用工具推进
- **blocked 等用户** → `goal_update(status=blocked)` 并说明原因后可结束；系统不会立刻续跑

不要口头说「完成了」却未调用 `goal_complete`。
