# Schedule Agent 规划能力提升调研报告

> 基于现有工具实现、用户 journal 讨论和业界最佳实践的深度调研  
> 调研日期：2026-02-27

---

## 一、现状分析

### 1.1 现有工具能力矩阵

| 工具 | 能做什么 | 缺失能力 |
|------|----------|----------|
| **get_free_slots** | 计算空闲时段、排除睡眠、合并相邻、min_duration 过滤 | 不识别 Maker Time 块；不识别精力偏好；无法输出「拥挤度」指标 |
| **plan_tasks** | 按优先级+deadline 排序、填空闲、创建事件、prefer_morning | **一次性规划，不重排**；时长用原始 estimate，无 buffer；无任务依赖；新任务来了不会挤占旧规划 |
| **add_event / add_task** | CRUD 操作 | 仅被动检测冲突（add_event 有 find_conflicts 警告），**不主动评估影响、不提议重排** |
| **update_event / update_task** | 改状态、时间、截止日期 | 改完不触发「影响分析」，不知道会不会挤占其他任务 |
| **maker-time-protector** | 设计理想周、保护专注块、复盘 | **固定规则**，新任务来了 Maker Time 不会自动让位或重排 |
| **记忆系统** | 存偏好、历史 | **不参与规划决策**，LLM 可查但工具层无「历史作业估算」接口 |

### 1.2 核心问题归纳

```
有「手脚」无「大脑」——能执行 CRUD 和简单填空，但缺动态决策能力
```

1. **无冲突预警与影响评估**：add_event 有冲突会提示，但没有「这周排太满」「deadline 可能完不成」的主动评估
2. **无重排能力**：plan_tasks 是一次性填空，新任务来了不会自动重排已规划任务
3. **无 buffer 机制**：时长估不准是常态，现有规划用原始 estimate，不留 buffer
4. **无估算依据**：任务时长靠用户拍脑袋，没有「基于文件分析/历史数据」的估算
5. **无任务依赖**：Task 模型无 `depends_on`，无法建模「Lab 必须在作业 A 之后」

---

## 二、业界最佳实践调研

### 2.1 约束满足（CSP）与运筹学

- **变量**：任务 = 变量（start_time, duration, priority, deadline）
- **约束**：硬约束（deadline、固定会议）+ 软约束（偏好时段、精力曲线）
- **求解**：找可行解优先，不一定最优
- **实践**：OR-Tools CP-SAT 提供 `IntervalVar`，适合区间调度；可先用手写启发式，复杂场景再上 OR-Tools

### 2.2 滚动时域规划（Rolling Horizon）

- **思想**：每天/每周重新扫描新任务，评估影响 → 重排 → 通知用户
- **落地**：可做成「周度重排建议」工具：输入「本周新任务」→ 输出「建议调整的已规划任务 + 理由」

### 2.3 鲁棒性 > 最优性

- 时长估不准是常态，计划要留 buffer
- **Buffer 前置**：别全堆在 deadline 前一周
- **启发式**：`estimated_minutes × 1.5` 作为规划用时长

### 2.4 研究式估算（Evidence-Based Estimation）

- **步骤**：获取材料 → 分析复杂度 → 查历史数据 → 给出有依据的估算
- **落地**：新建 `estimate_task` 工具，可调用 read_file 分析项目结构，结合 memory_search 查历史作业

---

## 三、工具升级建议

### 3.1 短期（核心逻辑打磨）

#### 1）新建 `check_schedule_health` 工具（冲突检测 + 拥挤度）

**职责**：主动评估日程健康度，而非被动等 add_event 时才发现冲突。

| 参数 | 说明 |
|------|------|
| date / days | 评估范围 |
| include_tasks | 是否把待办任务「拟排」进去一起算 |

**输出**：
- 时间冲突列表（已有 find_conflicts，可复用）
- 拥挤度：本周总需求时长 vs 总可用时长
- 预警：`本周排太满，建议减少 X 小时任务` 或 `deadline 可能完不成`

**实现**：
- 复用 GetFreeSlotsTool 的空闲计算
- 汇总待办任务的 estimated_minutes
- 简单启发式：`(总需求时长 × 1.5) > 总空闲时长` → 拥挤

#### 2）升级 `plan_tasks`：Buffer 与启发式

| 升级项 | 说明 |
|--------|------|
| buffer_ratio | 规划时长 = estimated_minutes × (1 + buffer_ratio)，默认 0.5（即 ×1.5） |
| buffer_front_load | buffer 前置：deadline 近的任务优先安排，避免全堆最后 |
| slot_choice | 可选「尽早排」vs「按 deadline 倒推」策略 |

#### 3）新建 `suggest_reschedule` 工具（重排建议，不直接改）

**职责**：给定新任务/新事件，输出「建议调整的已规划任务 + 理由」，**不执行修改**，需用户确认。

| 参数 | 说明 |
|------|------|
| new_task_ids | 新增要安排的任务 ID |
| new_events | 新增的固定事件（如新会议） |
| days | 规划范围 |

**输出**：
- 冲突列表
- 建议调整：`任务 A 建议从 X 移到 Y，因为...`
- 可选：直接返回 ToolResult，由 LLM 生成自然语言建议

**实现**：
- 调用 plan_tasks 的排序逻辑，但**模拟**插入新任务
- 若空闲不足，找出「优先级最低 / deadline 最远」的已规划任务，建议移走
- 与 update_event / update_task 解耦，仅输出建议

#### 4）新建 `estimate_task` 工具（研究式估算）

**职责**：基于文件分析 + 记忆检索，给出带依据的时长估算。

| 参数 | 说明 |
|------|------|
| task_id / task_title | 任务 |
| project_path | 项目路径（可选，用于分析代码规模） |
| use_memory | 是否查历史作业记忆 |

**输出**：
- 估算范围：如 `4–6 小时`
- 依据：`starter code 3 个 TODO，参考 Lab2 规模...`
- 建议：`建议分两次完成`

**实现**：
- 若有 project_path，调用 read_file 分析：文件数、TODO 数量、依赖
- 调用 memory_search 查「历史作业」「类似任务耗时」
- 用 LLM 做轻量总结（或纯规则 + 模板）

### 3.2 中期（工具整合）

#### 5）Task 模型扩展：依赖与元数据

```python
# 建议扩展 Task 模型
depends_on: Optional[List[str]] = None  # 依赖的任务 ID
source_path: Optional[str] = None       # 关联的项目路径（用于估算）
actual_minutes: Optional[int] = None    # 实际耗时（完成后填写，用于校准）
```

#### 6）历史作业数据结构

| 字段 | 说明 |
|------|------|
| assignment_name | 作业名称 |
| course | 课程 |
| estimated_minutes | 最初估算 |
| actual_minutes | 实际耗时 |
| complexity_score | 1–5 |
| tech_stack | 技术栈 |
| notes | 意外因素（debug、队友等） |

可存于内容记忆（content memory）或单独 JSONL，供 estimate_task 检索。

#### 7）周度重排工作流

- 用户触发：「这周来了新作业，帮我重排」
- Agent：get_tasks(todo) → suggest_reschedule(new_task_ids) → 展示建议 → 用户确认 → update_event/update_task

#### 8）Canvas 工具接入（若已有）

- 自动下载作业 → 分析 → estimate_task → plan_tasks / suggest_reschedule

### 3.3 长期（系统优化）

- 引入 OR-Tools CP-SAT 做复杂约束调度（多资源、强依赖）
- 基于 actual_minutes 持续校准估算模型
- Maker Time 与规划器联动：plan_tasks 可优先填入 Maker Time 块

---

## 四、工作流设计

### 4.1 建议的规划工作流架构

```
┌─────────────────────────────────────────────────────────────────┐
│  感知层：get_events + get_tasks + get_free_slots                 │
│  （现有工具，保持不动）                                           │
└───────────────────────────────┬─────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│  评估层（新增）                                                   │
│  - check_schedule_health：冲突、拥挤度、deadline 风险            │
│  - estimate_task：研究式估算（可选）                              │
└───────────────────────────────┬─────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│  决策层（新增 + 升级）                                            │
│  - suggest_reschedule：重排建议（不直接改）                       │
│  - plan_tasks（升级）：buffer、buffer_front_load                 │
│  - LLM 负责：何时调用、如何解释、是否需用户确认                   │
└───────────────────────────────┬─────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│  执行层（现有）                                                   │
│  - add_event / add_task / update_event / update_task             │
│  - 用户确认后再执行                                               │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 典型用户场景与工作流

| 场景 | 工作流 |
|------|--------|
| 用户添加新任务 | add_task → （可选）check_schedule_health → 若拥挤则 suggest_reschedule → LLM 解释 |
| 用户添加新事件 | add_event → find_conflicts 已有 → （可选）check_schedule_health 评估本周 |
| 用户说「帮我规划」 | get_tasks + get_free_slots → plan_tasks（升级版）→ 展示结果 |
| 用户说「这周来了新作业」 | get_tasks → suggest_reschedule(new_task_ids) → LLM 解释建议 → 用户确认 → update_* |
| 用户说「这个 lab 要多久」 | estimate_task(project_path=...) → 返回有依据的估算 |
| 每周复盘 | maker-time-protector 的周度复盘 + check_schedule_health 的拥挤度趋势 |

### 4.3 决策原则（LLM 提示词可强调）

1. **评估优先**：在 add_event / plan_tasks 前，有需要时先 check_schedule_health
2. **建议优先**：重排、大改先用 suggest_reschedule 出建议，用户确认后再 update
3. **Buffer 默认开启**：plan_tasks 默认 buffer_ratio=0.5
4. **估算可选**：用户问「要多久」或给路径时，优先 estimate_task

---

## 五、实现优先级建议

| 优先级 | 任务 | 预估工作量 | 依赖 |
|--------|------|------------|------|
| P0 | plan_tasks 增加 buffer_ratio、buffer_front_load | 小 | 无 |
| P0 | 新建 check_schedule_health | 中 | get_free_slots, get_tasks |
| P1 | 新建 suggest_reschedule | 中 | plan_tasks 逻辑复用 |
| P1 | 新建 estimate_task（简化版：仅查记忆） | 中 | memory_search, read_file |
| P2 | Task 模型扩展 depends_on, actual_minutes | 小 | 无 |
| P2 | 历史作业存储结构 + estimate_task 集成 | 中 | 内容记忆 / JSONL |
| P3 | estimate_task 文件分析（代码规模、TODO） | 中 | read_file |
| P3 | OR-Tools 集成（可选） | 大 | 复杂约束场景 |

---

## 六、与现有组件的集成要点

1. **maker-time-protector**：suggest_reschedule 可识别 Maker Time 标签，优先移走非 Maker 任务
2. **记忆系统**：estimate_task 调用 memory_search(scope=content) 查历史作业
3. **add_event 冲突**：check_schedule_health 可复用 EventRepository.find_conflicts，并扩展为「周度视角」
4. **get_free_slots**：check_schedule_health 内部可调用或复用其计算逻辑

---

## 七、参考资料

- [OR-Tools Scheduling Overview](https://developers.google.com/optimization/scheduling)
- [Constraint Programming for Disjunctive Scheduling (CP 2024)](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.CP.2024.12)
- [Efficient Task Scheduling Using Constraint Programming (MDPI 2024)](https://www.mdpi.com/2076-3417/14/23/11396)
- 项目 journal：`machiatto/journal/2026-02-27.md`
- 项目架构：`docs/WEB_ARCHITECTURE.md`

---

*本报告为调研结论，具体实现以 feature_list 和开发规范为准。*
