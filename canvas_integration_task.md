# Canvas API 集成开发任务书

## 任务目标
为玛奇朵 Agent 开发 Canvas LMS 集成功能，自动抓取用户的作业、考试、日历事件并同步到日程系统。

---

## 一、Canvas API 核心端点

### 1. 基础信息
- **Base URL**: `https://sjtu.instructure.com/api/v1` (上海交通大学 Canvas)
- **认证方式**: Bearer Token (API Key)
- **你的 API Key**: `Ysy5OJTYexDIgsBiOwDoa27xZWRh1s4chXd1CNVCVfN1h8ayl3IQvTz8gyr9PyIl`

### 2. 必须实现的 API 接口

#### 2.1 获取当前用户信息
```
GET /api/v1/users/self/profile
```
**用途**: 验证 API Key 有效性，获取用户 ID
**返回字段**: `id`, `name`, `login_id`, `avatar_url`

#### 2.2 获取用户所有课程
```
GET /api/v1/courses?enrollment_state[]=active&include[]=term
```
**用途**: 获取用户本学期所有课程
**关键参数**:
- `enrollment_state[]=active` - 只返回活跃课程
- `include[]=term` - 包含学期信息
**返回字段**: `id`, `name`, `course_code`, `start_at`, `end_at`, `term`

#### 2.3 获取课程作业列表
```
GET /api/v1/courses/{course_id}/assignments?include[]=submission&all_dates=true
```
**用途**: 获取某课程的所有作业
**关键参数**:
- `include[]=submission` - 包含提交状态（是否已提交、成绩等）
- `all_dates=true` - 包含所有日期（due_at, lock_at, unlock_at）
**返回字段**:
- `id`, `name`, `description`
- `due_at` - 截止时间
- `lock_at` - 锁定时间
- `points_possible` - 总分
- `submission_types` - 提交类型 (online_upload, text_entry 等)
- `submission` - 提交状态对象:
  - `submitted_at` - 提交时间
  - `workflow_state` - 状态 (submitted, graded, missing, late)
  - `grade` - 成绩
  - `attempt` - 提交次数

#### 2.4 获取日历事件（含作业）
```
GET /api/v1/calendar_events?start_date={YYYY-MM-DD}&end_date={YYYY-MM-DD}&event_types[]=assignment&event_types[]=event
```
**用途**: 获取指定时间范围内的所有日历事件和作业
**关键参数**:
- `start_date`, `end_date` - 时间范围
- `event_types[]=assignment` - 包含作业
- `event_types[]=event` - 包含日历事件（考试、讲座等）
**返回字段**:
- `id`, `title`, `description`
- `start_at`, `end_at`
- `type` - 类型 (Assignment, Event)
- `course_id`, `course_name`
- `all_day` - 是否全天事件

#### 2.5 获取作业提交详情
```
GET /api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self
```
**用途**: 获取特定作业的详细提交状态
**返回字段**: 同 2.3 的 submission 对象

---

## 二、功能设计

### 1. 模块结构
```
/work/src/canvas_integration/
├── __init__.py
├── client.py           # Canvas API 客户端
├── models.py           # 数据模型
├── sync.py             # 同步逻辑（Canvas → 本地日程）
├── config.py           # 配置管理
└── README.md           # 使用说明
```

### 2. 核心功能

#### 2.1 CanvasClient 类 (client.py)
```python
class CanvasClient:
    def __init__(self, api_key: str, base_url: str = "https://sjtu.instructure.com/api/v1")
    
    # 基础方法
    async def request(self, method: str, endpoint: str, params: dict = None) -> dict
    async def get(self, endpoint: str, params: dict = None) -> dict
    
    # API 方法
    async def get_user_profile(self) -> dict
    async def get_courses(self, enrollment_state: str = "active") -> list[dict]
    async def get_assignments(self, course_id: int, include_submission: bool = True) -> list[dict]
    async def get_calendar_events(self, start_date: str, end_date: str, event_types: list = None) -> list[dict]
    async def get_submission(self, course_id: int, assignment_id: int) -> dict
    
    # 便捷方法
    async def get_all_assignments(self, course_ids: list[int] = None) -> list[dict]
    async def get_upcoming_events(self, days: int = 30) -> list[dict]
```

#### 2.2 数据模型 (models.py)
```python
@dataclass
class CanvasAssignment:
    id: int
    name: str
    course_id: int
    course_name: str
    due_at: datetime
    points_possible: float
    submission_types: list[str]
    is_submitted: bool
    submitted_at: datetime | None
    workflow_state: str  # submitted, graded, missing, late
    grade: str | None
    url: str

@dataclass
class CanvasEvent:
    id: int
    title: str
    description: str
    start_at: datetime
    end_at: datetime
    course_id: int | None
    course_name: str | None
    event_type: str  # Assignment, Event
    all_day: bool
    url: str
```

#### 2.3 同步逻辑 (sync.py)
```python
class CanvasSync:
    def __init__(self, canvas_client: CanvasClient, schedule_agent)
    
    # 同步方法
    async def sync_assignments_to_schedule(self, days_ahead: int = 60) -> SyncResult
    async def sync_calendar_events_to_schedule(self, days_ahead: int = 60) -> SyncResult
    
    # 转换方法
    def _assignment_to_event(self, assignment: CanvasAssignment) -> dict
    def _calendar_event_to_event(self, event: CanvasEvent) -> dict
    
    # 智能处理
    async def detect_conflicts(self, events: list) -> list[Conflict]
    async def prioritize_events(self, events: list) -> list[dict]
```

#### 2.4 配置管理 (config.py)
```python
# 配置文件位置：/work/.env 或 /work/config.yaml
# 新增配置项：
# CANVAS_API_KEY=你的 API Key
# CANVAS_BASE_URL=https://sjtu.instructure.com/api/v1
# CANVAS_SYNC_ENABLED=true
# CANVAS_SYNC_INTERVAL_HOURS=6
```

### 3. 与日程系统集成

#### 3.1 同步策略
- **首次同步**: 获取所有未来 60 天的作业和事件
- **增量同步**: 每 6 小时检查一次更新
- **状态跟踪**: 记录已同步的事件 ID，避免重复创建

#### 3.2 日程事件格式
```python
# 作业 → 日程事件
{
    "title": f"[作业] {course_name}: {assignment_name}",
    "start_time": due_at - timedelta(hours=2),  # 提前 2 小时提醒
    "end_time": due_at,
    "priority": "high" if days_left < 3 else "medium",
    "tags": ["canvas", "assignment", course_code],
    "metadata": {
        "source": "canvas",
        "canvas_id": assignment_id,
        "course_id": course_id,
        "points": points_possible,
        "submission_url": url
    }
}

# 考试/事件 → 日程事件
{
    "title": f"[考试] {course_name}: {event_title}" if "exam" in title.lower() else event_title,
    "start_time": start_at,
    "end_time": end_at,
    "priority": "high" if is_exam else "medium",
    "tags": ["canvas", "event", course_code],
    "metadata": {
        "source": "canvas",
        "canvas_id": event_id,
        "course_id": course_id
    }
}
```

---

## 三、开发步骤

### 第 1 步：创建模块结构
```bash
mkdir -p /work/src/canvas_integration
```
创建基础文件：`__init__.py`, `client.py`, `models.py`, `sync.py`, `config.py`

### 第 2 步：实现 CanvasClient
- 实现 HTTP 请求封装（使用 `aiohttp` 或 `requests`）
- 实现所有 API 方法
- 添加错误处理（401 认证失败、429 限流、网络错误）
- 添加分页支持（Canvas API 返回分页数据）

### 第 3 步：实现数据模型
- 定义 `CanvasAssignment` 和 `CanvasEvent` 数据类
- 实现从 API 响应到模型的转换方法

### 第 4 步：实现同步逻辑
- 调用日程 Agent 的工具（需要先 `search_tools` 查找日程创建工具）
- 实现 Canvas 事件 → 日程事件的转换
- 添加去重逻辑（基于 canvas_id）
- 添加冲突检测

### 第 5 步：配置管理
- 在 `.env` 中添加 Canvas 配置
- 在 `config.yaml` 中添加同步设置
- 实现配置加载和验证

### 第 6 步：测试
- 测试 API 连接（获取用户信息）
- 测试获取课程列表
- 测试获取作业和事件
- 测试同步到日程

### 第 7 步：文档
- 编写 `README.md` 说明使用方法
- 添加配置说明
- 添加常见问题解答

---

## 四、安全注意事项

1. **API Key 保护**
   - 不要硬编码在代码中
   - 从环境变量或配置文件读取
   - 不要提交到 Git（添加到 `.gitignore`）

2. **错误处理**
   - 认证失败时提示用户检查 API Key
   - 限流时自动重试（Canvas 限制：每分钟 700 次请求）
   - 网络错误时优雅降级

3. **权限最小化**
   - 只读取必要的数据
   - 不修改 Canvas 数据（只读模式）

---

## 五、验收标准

- [ ] 能成功连接 Canvas API 并获取用户信息
- [ ] 能获取所有活跃课程
- [ ] 能获取每门课程的作业列表（含提交状态）
- [ ] 能获取日历事件（含考试）
- [ ] 能将作业和事件同步到日程系统
- [ ] 同步的事件包含正确的元数据（来源、ID、链接）
- [ ] 不会重复创建已存在的事件
- [ ] 有完善的错误处理和日志记录
- [ ] 配置管理完善（API Key 安全存储）

---

## 六、扩展功能（可选）

1. **智能提醒**: 根据作业截止时间和用户习惯，自动设置提醒时间
2. **进度跟踪**: 定期检查作业提交状态，更新日程事件
3. **冲突检测**: 发现多个作业/考试在同一时间段时提醒用户
4. **优先级排序**: 根据分数占比、剩余时间自动设置优先级
5. **通知推送**: 新作业发布时主动通知用户

---

## 七、参考资源

- Canvas API 文档: https://canvas.instructure.com/doc/api/
- 作业 API: https://canvas.instructure.com/doc/api/assignments.html
- 日历事件 API: https://canvas.instructure.com/doc/api/calendar_events.html
- 课程 API: https://canvas.instructure.com/doc/api/courses.html
- 提交 API: https://canvas.instructure.com/doc/api/submissions.html

---

**开始开发前，请先：**
1. 阅读本任务书，理解整体设计
2. 搜索项目中现有的日程 Agent 工具，了解如何创建日程事件
3. 确认 Python 环境中有 `aiohttp` 或 `requests` 库
4. 从获取用户信息开始，逐步验证每个 API 方法

**开发原则：**
- 遵循 coding skill 的最佳实践（DRY, KISS, SOLID）
- 代码清晰易读，注释说明"为什么"而非"是什么"
- 添加完善的错误处理
- 编写测试用例
- 不硬编码敏感信息
