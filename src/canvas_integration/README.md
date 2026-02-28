# Canvas LMS 集成模块

为玛奇朵 Agent 提供 Canvas LMS 集成功能，自动抓取作业、考试和日历事件并同步到日程系统。

## 功能特性

- ✅ **自动获取课程信息**：获取用户所有活跃课程
- ✅ **作业跟踪**：获取所有作业的截止时间、提交状态、成绩
- ✅ **日历事件**：获取考试、讲座等日历事件
- ✅ **智能同步**：将作业和事件自动同步到日程系统
- ✅ **状态追踪**：记录已提交作业和已评分作业
- ✅ **优先级排序**：根据截止时间自动设置优先级
- ✅ **错误处理**：完善的错误处理和重试机制

## 快速开始

### 1. 配置环境变量

在 `.env` 文件中添加 Canvas API 配置：

```bash
export CANVAS_API_KEY="你的 API Key"
export CANVAS_BASE_URL="https://sjtu.instructure.com/api/v1"
export CANVAS_SYNC_ENABLED="true"
export CANVAS_SYNC_INTERVAL_HOURS="6"
export CANVAS_DEFAULT_DAYS_AHEAD="60"
```

### 2. 基本使用

```python
import asyncio
from canvas_integration import CanvasConfig, CanvasClient, CanvasSync

async def main():
    # 加载配置
    config = CanvasConfig.from_env()
    
    # 创建客户端
    async with CanvasClient(config) as client:
        # 测试连接
        if not await client.test_connection():
            print("连接失败，请检查 API Key")
            return
        
        # 获取用户信息
        profile = await client.get_user_profile()
        print(f"欢迎，{profile['name']}!")
        
        # 获取所有课程
        courses = await client.get_courses()
        print(f"你有 {len(courses)} 门课程:")
        for course in courses:
            print(f"  - {course['name']}")
        
        # 获取即将到来的作业
        assignments = await client.get_upcoming_assignments(days=30)
        print(f"\n未来 30 天有 {len(assignments)} 个作业:")
        for assignment in assignments:
            status = "✓" if assignment.is_submitted else "○"
            print(f"  {status} {assignment.name} (截止：{assignment.due_at})")
        
        # 同步到日程系统
        sync = CanvasSync(client)
        result = await sync.sync_to_schedule(days_ahead=60)
        print(f"\n同步完成：新建 {result.created_count} 个事件")

if __name__ == "__main__":
    asyncio.run(main())
```

### 3. 与日程系统集成

```python
from canvas_integration import CanvasConfig, CanvasClient, CanvasSync

async def sync_to_agent_schedule():
    config = CanvasConfig.from_env()
    
    async with CanvasClient(config) as client:
        sync = CanvasSync(client)
        
        # 获取待同步的事件数据
        assignments = await client.get_upcoming_assignments(days=60)
        
        for assignment in assignments:
            # 转换为日程事件格式
            event_data = sync._assignment_to_event(assignment)
            
            # 调用日程工具创建事件
            # event_id = await call_tool("add_event", event_data)
            # print(f"创建事件：{event_id}")
```

## API 参考

### CanvasConfig

配置管理类。

```python
config = CanvasConfig.from_env()
```

**属性：**
- `api_key`: Canvas API 密钥
- `base_url`: API 基础 URL
- `sync_enabled`: 是否启用同步
- `sync_interval_hours`: 同步间隔（小时）
- `default_days_ahead`: 默认同步未来多少天

### CanvasClient

Canvas API 客户端。

```python
async with CanvasClient(config) as client:
    # 获取用户信息
    profile = await client.get_user_profile()
    
    # 获取课程列表
    courses = await client.get_courses()
    
    # 获取课程作业
    assignments = await client.get_assignments(course_id=123)
    
    # 获取日历事件
    events = await client.get_calendar_events("2026-02-28", "2026-03-31")
    
    # 获取即将到来的作业
    upcoming = await client.get_upcoming_assignments(days=30)
    
    # 测试连接
    is_connected = await client.test_connection()
```

### CanvasSync

同步器类。

```python
sync = CanvasSync(client)
result = await sync.sync_to_schedule(days_ahead=60)

print(f"新建：{result.created_count}")
print(f"更新：{result.updated_count}")
print(f"跳过：{result.skipped_count}")
print(f"错误：{result.errors}")
```

### 数据模型

**CanvasAssignment** - 作业模型：
- `id`: 作业 ID
- `name`: 作业名称
- `course_name`: 课程名称
- `due_at`: 截止时间
- `points_possible`: 总分
- `is_submitted`: 是否已提交
- `workflow_state`: 提交状态
- `grade`: 成绩
- `days_left`: 剩余天数
- `html_url`: Canvas 链接

**CanvasEvent** - 日历事件模型：
- `id`: 事件 ID
- `title`: 事件标题
- `start_at`: 开始时间
- `end_at`: 结束时间
- `course_name`: 课程名称
- `event_type`: 事件类型
- `all_day`: 是否全天事件

**SyncResult** - 同步结果：
- `created_count`: 新建数量
- `updated_count`: 更新数量
- `skipped_count`: 跳过数量
- `errors`: 错误列表

## Canvas API 端点

| 功能 | 端点 | 说明 |
|------|------|------|
| 用户信息 | `GET /users/self/profile` | 获取当前用户信息 |
| 课程列表 | `GET /courses` | 获取用户所有课程 |
| 作业列表 | `GET /courses/{id}/assignments` | 获取课程作业 |
| 日历事件 | `GET /calendar_events` | 获取日历事件 |
| 提交详情 | `GET /courses/{c}/assignments/{a}/submissions/self` | 获取提交状态 |

## 错误处理

模块定义了以下异常类：

```python
from canvas_integration import CanvasAPIError, CanvasAuthError, CanvasRateLimitError

try:
    async with CanvasClient(config) as client:
        await client.get_user_profile()
except CanvasAuthError:
    print("认证失败，请检查 API Key")
except CanvasRateLimitError:
    print("请求过于频繁，请稍后重试")
except CanvasAPIError as e:
    print(f"API 错误：{e}")
```

## 日志配置

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("canvas_integration")
```

## 安全注意事项

1. **保护 API Key**：
   - 不要硬编码在代码中
   - 从环境变量读取
   - 不要提交到 Git

2. **限流处理**：
   - Canvas 限制 700 次请求/分钟
   - 模块会自动处理限流和重试

3. **只读模式**：
   - 当前实现只读取数据
   - 不修改 Canvas 中的任何内容

## 故障排查

### 认证失败（401）
- 检查 API Key 是否正确
- 确认 Canvas URL 是否正确（上海交大：`https://sjtu.instructure.com`）

### 空数据
- 确认课程状态（只返回活跃课程）
- 检查时间范围是否正确

### 同步失败
- 检查日程工具是否可用
- 查看错误日志获取详细信息

## 开发计划

- [ ] 支持增量同步（只同步更新的内容）
- [ ] 支持作业提交状态更新提醒
- [ ] 支持冲突检测（多个作业同一时间）
- [ ] 支持智能提醒（根据用户习惯设置提醒时间）
- [ ] 支持 Webhook 实时通知

## 许可证

本模块作为玛奇朵 Agent 的一部分，遵循相同的开源协议。

---

**作者**: Machiatto  
**版本**: 1.0.0  
**最后更新**: 2026-02-28
