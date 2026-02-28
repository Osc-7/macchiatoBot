# Canvas Integration Implementation Plan

## 任务概述
为玛奇朵 Agent 实现 Canvas LMS 集成功能，自动抓取作业、考试和日历事件并同步到日程系统。

## Canvas API 信息
- **Base URL**: `https://sjtu.instructure.com/api/v1`
- **API Key**: `Ysy5OJTYexDIgsBiOwDoa27xZWRh1s4chXd1CNVCVfN1h8ayl3IQvTz8gyr9PyIl`
- **认证方式**: `Authorization: Bearer {API_KEY}`

## 核心 API 端点

### 1. 获取用户信息
```
GET /api/v1/users/self/profile
```

### 2. 获取课程列表
```
GET /api/v1/courses?enrollment_state[]=active&include[]=term
```

### 3. 获取课程作业
```
GET /api/v1/courses/{course_id}/assignments?include[]=submission&all_dates=true
```

### 4. 获取日历事件
```
GET /api/v1/calendar_events?start_date={YYYY-MM-DD}&end_date={YYYY-MM-DD}&event_types[]=assignment&event_types[]=event
```

### 5. 获取作业提交状态
```
GET /api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self
```

## 实现步骤

### Step 1: 创建模块目录结构
```bash
mkdir -p /work/src/canvas_integration
```

创建以下文件：
- `/work/src/canvas_integration/__init__.py`
- `/work/src/canvas_integration/client.py`
- `/work/src/canvas_integration/models.py`
- `/work/src/canvas_integration/sync.py`
- `/work/src/canvas_integration/config.py`
- `/work/src/canvas_integration/README.md`

### Step 2: 实现 config.py - 配置管理

```python
"""Canvas 集成配置管理"""
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class CanvasConfig:
    api_key: str
    base_url: str = "https://sjtu.instructure.com/api/v1"
    sync_enabled: bool = True
    sync_interval_hours: int = 6
    default_days_ahead: int = 60
    
    @classmethod
    def from_env(cls) -> "CanvasConfig":
        """从环境变量加载配置"""
        api_key = os.getenv("CANVAS_API_KEY")
        if not api_key:
            raise ValueError("CANVAS_API_KEY environment variable not set")
        
        return cls(
            api_key=api_key,
            base_url=os.getenv("CANVAS_BASE_URL", "https://sjtu.instructure.com/api/v1"),
            sync_enabled=os.getenv("CANVAS_SYNC_ENABLED", "true").lower() == "true",
            sync_interval_hours=int(os.getenv("CANVAS_SYNC_INTERVAL_HOURS", "6")),
            default_days_ahead=int(os.getenv("CANVAS_DEFAULT_DAYS_AHEAD", "60")),
        )
```

**任务**: 
1. 创建 config.py 实现上述配置类
2. 在 `/work/.env` 文件中添加 `CANVAS_API_KEY=Ysy5OJTYexDIgsBiOwDoa27xZWRh1s4chXd1CNVCVfN1h8ayl3IQvTz8gyr9PyIl`

### Step 3: 实现 models.py - 数据模型

创建以下数据类：
1. `CanvasAssignment`: 作业模型
   - 字段：id, name, description, course_id, course_name, due_at, lock_at, points_possible, submission_types, is_submitted, submitted_at, workflow_state, grade, attempt, url

2. `CanvasEvent`: 日历事件模型
   - 字段：id, title, description, start_at, end_at, course_id, course_name, event_type, all_day, url

3. `SyncResult`: 同步结果模型
   - 字段：created_count, updated_count, skipped_count, errors

4. 实现从 API 响应到模型的转换方法

### Step 4: 实现 client.py - Canvas API 客户端

```python
"""Canvas API 客户端"""
import httpx
from typing import Optional, List
from datetime import datetime

from .config import CanvasConfig
from .models import CanvasAssignment, CanvasEvent

class CanvasClient:
    def __init__(self, config: CanvasConfig):
        self.config = config
        self.base_url = config.base_url
        self.headers = {"Authorization": f"Bearer {config.api_key}"}
    
    async def request(self, method: str, endpoint: str, params: Optional[dict] = None) -> dict:
        """发送 HTTP 请求，处理分页和错误"""
        # 实现要点：
        # 1. 使用 httpx.AsyncClient
        # 2. 处理分页（Canvas 使用 Link header）
        # 3. 错误处理：401 认证失败、429 限流、网络错误
        # 4. 自动重试（指数退避）
        pass
    
    async def get_user_profile(self) -> dict:
        """获取当前用户信息"""
        return await self.request("GET", "/users/self/profile")
    
    async def get_courses(self, enrollment_state: str = "active") -> List[dict]:
        """获取用户所有课程"""
        params = {"enrollment_state[]": enrollment_state, "include[]": "term"}
        return await self.request("GET", "/courses", params)
    
    async def get_assignments(self, course_id: int, include_submission: bool = True) -> List[CanvasAssignment]:
        """获取课程作业列表"""
        params = {}
        if include_submission:
            params["include[]"] = "submission"
            params["all_dates"] = "true"
        
        response = await self.request("GET", f"/courses/{course_id}/assignments", params)
        # 转换为 CanvasAssignment 模型
        return response
    
    async def get_calendar_events(
        self, 
        start_date: str, 
        end_date: str, 
        event_types: Optional[List[str]] = None
    ) -> List[CanvasEvent]:
        """获取日历事件"""
        if event_types is None:
            event_types = ["assignment", "event"]
        
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "event_types[]": event_types
        }
        response = await self.request("GET", "/calendar_events", params)
        return response
    
    async def get_submission(self, course_id: int, assignment_id: int) -> dict:
        """获取作业提交详情"""
        return await self.request(
            "GET", 
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self"
        )
    
    async def get_all_assignments(self, course_ids: Optional[List[int]] = None) -> List[CanvasAssignment]:
        """获取所有课程的作业"""
        if course_ids is None:
            courses = await self.get_courses()
            course_ids = [c["id"] for c in courses]
        
        all_assignments = []
        for course_id in course_ids:
            assignments = await self.get_assignments(course_id)
            all_assignments.extend(assignments)
        
        return all_assignments
    
    async def get_upcoming_events(self, days: int = 60) -> List[CanvasEvent]:
        """获取未来 N 天的事件"""
        from datetime import datetime, timedelta
        
        start_date = datetime.now().strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        
        return await self.get_calendar_events(start_date, end_date)
```

**实现要点**:
1. 使用 `httpx.AsyncClient` 进行异步请求
2. 处理分页（Canvas API 使用 Link header，格式：`<url>; rel="next"`）
3. 错误处理：
   - 401: 抛出认证错误，提示检查 API Key
   - 429: 等待后重试（Canvas 限制 700 次/分钟）
   - 网络错误：重试 3 次
4. 所有日期时间转换为 datetime 对象
5. 添加日志记录

### Step 5: 实现 sync.py - 同步逻辑

```python
"""Canvas 到日程系统的同步逻辑"""
from datetime import datetime, timedelta
from typing import List, Dict, Any

from .client import CanvasClient
from .models import CanvasAssignment, CanvasEvent, SyncResult

class CanvasSync:
    def __init__(self, canvas_client: CanvasClient):
        self.client = canvas_client
        self.synced_event_ids = set()  # 记录已同步的事件 ID
    
    async def sync_to_schedule(self, days_ahead: int = 60) -> SyncResult:
        """同步 Canvas 事件到日程系统"""
        result = SyncResult()
        
        # 1. 获取所有即将到来的事件
        events = await self.client.get_upcoming_events(days_ahead)
        
        # 2. 转换为日程事件格式
        for event in events:
            # 跳过已同步的事件
            if event.id in self.synced_event_ids:
                result.skipped_count += 1
                continue
            
            # 转换为日程事件
            schedule_event = self._convert_to_schedule_event(event)
            
            # 3. 调用日程工具创建事件
            # 注意：这里需要通过 call_tool 调用 add_event
            # 由于这是在 Python 代码中，需要通过特殊方式调用
            try:
                # TODO: 实现日程事件创建
                # event_id = await self._create_schedule_event(schedule_event)
                # self.synced_event_ids.add(event.id)
                result.created_count += 1
            except Exception as e:
                result.errors.append(str(e))
        
        return result
    
    def _convert_to_schedule_event(self, event: CanvasEvent | CanvasAssignment) -> Dict[str, Any]:
        """将 Canvas 事件转换为日程事件格式"""
        # 判断事件类型
        if isinstance(event, CanvasAssignment):
            title = f"[作业] {event.course_name}: {event.name}"
            priority = "high" if event.days_left < 3 else "medium"
            tags = ["canvas", "assignment", event.course_name]
            
            # 提前 2 小时开始提醒
            start_time = event.due_at - timedelta(hours=2)
            end_time = event.due_at
            
            description = f"截止时间：{event.due_at}\n总分：{event.points_possible}\n提交状态：{event.workflow_state}"
            if event.url:
                description += f"\n提交链接：{event.url}"
        
        else:  # CanvasEvent
            is_exam = "exam" in event.title.lower() or "考试" in event.title
            title = f"[考试] {event.course_name}: {event.title}" if is_exam else event.title
            priority = "high" if is_exam else "medium"
            tags = ["canvas", "event", event.course_name] if event.course_name else ["canvas", "event"]
            
            start_time = event.start_at
            end_time = event.end_at or (start_time + timedelta(hours=2))
            description = event.description or ""
        
        return {
            "title": title,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "description": description,
            "priority": priority,
            "tags": tags,
            "metadata": {
                "source": "canvas",
                "canvas_id": event.id,
                "course_id": event.course_id,
            }
        }
    
    async def _create_schedule_event(self, event_data: Dict[str, Any]) -> str:
        """创建日程事件（需要调用日程工具）"""
        # TODO: 实现日程事件创建
        # 这需要通过 call_tool 调用 add_event 工具
        pass
```

**实现要点**:
1. 记录已同步的事件 ID，避免重复创建
2. 智能判断事件类型（作业/考试/普通事件）
3. 自动设置优先级（临近截止的作业为 high）
4. 添加详细的描述信息（包含 Canvas 链接）
5. 错误处理和日志记录

### Step 6: 创建 __init__.py

```python
"""Canvas LMS 集成模块"""
from .config import CanvasConfig
from .client import CanvasClient
from .sync import CanvasSync
from .models import CanvasAssignment, CanvasEvent, SyncResult

__all__ = [
    "CanvasConfig",
    "CanvasClient",
    "CanvasSync",
    "CanvasAssignment",
    "CanvasEvent",
    "SyncResult",
]
```

### Step 7: 创建 README.md

编写使用说明文档，包括：
1. 功能介绍
2. 配置方法
3. 使用示例
4. API 参考
5. 常见问题

### Step 8: 更新 .env 文件

在 `/work/.env` 中添加：
```
CANVAS_API_KEY=Ysy5OJTYexDIgsBiOwDoa27xZWRh1s4chXd1CNVCVfN1h8ayl3IQvTz8gyr9PyIl
CANVAS_BASE_URL=https://sjtu.instructure.com/api/v1
CANVAS_SYNC_ENABLED=true
CANVAS_SYNC_INTERVAL_HOURS=6
```

### Step 9: 测试

创建测试文件 `/work/tests/test_canvas_integration.py`：
1. 测试 API 连接（获取用户信息）
2. 测试获取课程列表
3. 测试获取作业
4. 测试获取日历事件
5. 测试同步逻辑

## 代码质量要求

1. **遵循 coding skill 最佳实践**:
   - DRY: 避免重复代码
   - KISS: 保持简单
   - SOLID: 单一职责、依赖倒置

2. **错误处理**:
   - 所有外部调用都要有 try-except
   - 提供清晰的错误信息
   - 实现重试机制

3. **类型注解**:
   - 所有函数都要有类型注解
   - 使用 dataclass 定义数据模型

4. **文档**:
   - 每个类和函数都要有 docstring
   - 说明"为什么"而非"是什么"

5. **日志**:
   - 使用 logging 模块
   - 记录关键操作和错误

## 开始执行

请按顺序完成上述步骤，每完成一步都要进行验证：
1. Step 1-2: 创建目录和配置文件后，验证配置能正确加载
2. Step 3: 创建模型后，验证数据转换正确
3. Step 4: 实现客户端后，测试 API 连接
4. Step 5: 实现同步逻辑后，测试同步功能
5. Step 6-9: 完善文档和测试

**重要**: 在实现过程中，如果遇到不确定的地方（如如何调用日程工具），请先暂停并询问，不要猜测实现。
