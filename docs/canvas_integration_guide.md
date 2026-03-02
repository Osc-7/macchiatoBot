# Canvas 集成使用指南

## 概述

Canvas 集成模块已成功开发并测试通过，可以：
- ✅ 连接到上海交通大学 Canvas (`oc.sjtu.edu.cn`)
- ✅ 获取用户信息和课程列表
- ✅ 获取作业和日历事件
- ✅ 将作业转换为日程事件格式

**当前状态**: 核心功能已完成，需要在 Agent 中集成实际的日程创建逻辑。

---

## 模块结构

```
/work/src/canvas_integration/
├── __init__.py          # 模块入口
├── config.py            # 配置管理
├── client.py            # Canvas API 客户端
├── models.py            # 数据模型
├── sync.py              # 同步逻辑
├── README.md            # 模块文档
└── (tests)
```

---

## 在 Agent 中集成

### 方案 1: 直接调用（推荐）

在 Agent 的主循环中定期调用 Canvas 同步：

```python
# 在 main.py 或专门的 schedule_sync.py 中
import asyncio
from canvas_integration import CanvasConfig, CanvasClient, CanvasSync

async def sync_canvas_schedule():
    """同步 Canvas 到日程"""
    config = CanvasConfig.from_env()
    
    async with CanvasClient(config) as client:
        # 测试连接
        if not await client.test_connection():
            logger.error("Canvas 连接失败")
            return
        
        # 获取即将到来的作业
        assignments = await client.get_upcoming_assignments(days=60)
        logger.info(f"获取到 {len(assignments)} 个即将到来的作业")
        
        # 创建同步器
        sync = CanvasSync(client)
        
        # 逐个创建日程事件
        for assignment in assignments:
            # 跳过已提交的
            if assignment.is_submitted:
                continue
            
            # 转换为日程事件格式
            event_data = sync._assignment_to_event(assignment)
            
            # 调用日程工具创建事件
            # 注意：这里需要调用实际的日程工具
            event_id = await create_schedule_event(event_data)
            
            if event_id:
                logger.info(f"创建日程事件：{assignment.name}")
            else:
                logger.error(f"创建失败：{assignment.name}")

async def create_schedule_event(event_data: dict) -> str:
    """创建日程事件（调用日程工具）
    
    这里需要调用实际的日程工具，例如：
    - 使用 call_tool("add_event", event_data)
    - 或者通过其他方式调用日程 API
    """
    # TODO: 实现实际的日程创建逻辑
    # 这是需要在 Agent 主代码中实现的部分
    pass
```

### 方案 2: 定时任务

设置定时任务，每 6 小时同步一次：

```python
import asyncio
from datetime import datetime, timedelta

async def scheduled_canvas_sync():
    """定时同步 Canvas"""
    while True:
        try:
            logger.info("开始定时同步 Canvas...")
            await sync_canvas_schedule()
            logger.info("Canvas 同步完成")
        except Exception as e:
            logger.error(f"Canvas 同步失败：{e}")
        
        # 等待 6 小时
        await asyncio.sleep(6 * 3600)

# 在 Agent 启动时启动定时任务
asyncio.create_task(scheduled_canvas_sync())
```

### 方案 3: 手动触发

通过命令或消息触发同步：

```python
# 用户说"同步 Canvas 作业"时触发
async def handle_canvas_sync_command():
    await sync_canvas_schedule()
    return "Canvas 作业同步完成！"
```

---

## 关键代码片段

### 1. 创建日程事件

```python
from canvas_integration import CanvasSync

sync = CanvasSync(client)

# 转换作业为日程事件
event_data = sync._assignment_to_event(assignment)

# event_data 格式:
# {
#     "title": "[作业] 课程名：作业名",
#     "start_time": "2026-03-01T10:00:00",
#     "end_time": "2026-03-01T12:00:00",
#     "description": "课程：xxx\n总分：100\n状态：未提交\n链接：https://...",
#     "priority": "high",  # urgent/high/medium/low
#     "tags": ["canvas", "作业", "课程名"],
#     "metadata": {
#         "source": "canvas",
#         "canvas_id": 123,
#         "course_id": 456,
#         "type": "assignment"
#     }
# }
```

### 2. 调用日程工具

根据项目中的日程工具，调用方式可能不同。参考已有的日程工具：

```python
# 如果使用 call_tool
event_id = await call_tool(
    "add_event",
    arguments={
        "title": event_data["title"],
        "start_time": event_data["start_time"],
        "end_time": event_data["end_time"],
        "description": event_data["description"],
        "priority": event_data["priority"],
        "tags": event_data["tags"],
    }
)

# 或者直接使用 add_event 工具
from schedule_tools import add_event

event_id = await add_event(
    title=event_data["title"],
    start_time=event_data["start_time"],
    end_time=event_data["end_time"],
    description=event_data["description"],
    priority=event_data["priority"],
    tags=event_data["tags"],
)
```

### 3. 去重逻辑

避免重复创建事件：

```python
# 记录已同步的事件 ID
synced_ids = set()

for assignment in assignments:
    if assignment.id in synced_ids:
        continue
    
    # 创建事件
    event_id = await create_schedule_event(...)
    
    if event_id:
        synced_ids.add(assignment.id)
```

### 4. 智能优先级

根据截止时间自动设置优先级：

```python
days_left = assignment.days_left

if days_left <= 1:
    priority = "urgent"  # 紧急
elif days_left <= 3:
    priority = "high"    # 高
elif days_left <= 7:
    priority = "medium"  # 中
else:
    priority = "low"     # 低
```

---

## 测试验证

### 运行快速测试

```bash
cd /work
python tests/test_canvas_quick.py
```

### 预期输出

```
============================================================
Canvas 集成快速测试
============================================================

[1/3] 测试连接...
✓ 用户：刘宇轩 (524030910153)

[2/3] 获取课程...
✓ 共 36 门课程

[3/3] 获取作业（前 3 门课程）...
  - 操作系统：0 个作业
  - 程序设计与数据结构 -Ⅰ: 17 个作业
  - 程序设计与数据结构-Ⅱ: 20 个作业

============================================================
测试结果
============================================================
✓ 用户：刘宇轩
✓ 课程：36 门
✓ 作业：37 个（前 3 门课程）

✓ Canvas 集成模块工作正常!
```

---

## 配置说明

### 环境变量

在 `/work/.env` 中配置：

```bash
# Canvas API 配置
export CANVAS_API_KEY="Ysy5OJTYexDIgsBiOwDoa27xZWRh1s4chXd1CNVCVfN1h8ayl3IQvTz8gyr9PyIl"
export CANVAS_BASE_URL="https://oc.sjtu.edu.cn/api/v1"
export CANVAS_SYNC_ENABLED="true"
export CANVAS_SYNC_INTERVAL_HOURS="6"
export CANVAS_DEFAULT_DAYS_AHEAD="60"
```

### 配置类

```python
from canvas_integration import CanvasConfig

config = CanvasConfig.from_env()

# 配置属性
config.api_key              # API Key
config.base_url             # API 基础 URL
config.sync_enabled         # 是否启用同步
config.sync_interval_hours  # 同步间隔（小时）
config.default_days_ahead   # 默认同步未来多少天
```

---

## 下一步

### 1. 集成到 Agent 主循环

在 `main.py` 或相关模块中添加 Canvas 同步逻辑。

### 2. 实现日程创建

调用实际的日程工具创建事件。

### 3. 添加用户命令

支持用户通过消息触发同步：
- "同步 Canvas 作业"
- "查看即将到来的作业"
- "Canvas 上有什么新作业"

### 4. 优化性能

- 使用并发请求加速作业获取
- 实现增量同步（只同步更新的内容）
- 添加缓存机制

### 5. 错误处理

- 网络错误的重试逻辑
- API 限流的处理
- 用户友好的错误提示

---

## API 限流说明

Canvas API 限制：
- **700 次请求/分钟** per user
- 模块已自动处理限流（429 错误会自动重试）

优化建议：
- 批量获取作业（使用 `get_all_assignments`）
- 避免频繁同步（建议 6 小时间隔）
- 使用分页减少单次请求数据量

---

## 故障排查

### 认证失败

```
CanvasAuthError: Authentication failed
```

**解决**: 检查 API Key 是否正确，确认 Canvas URL 是 `https://oc.sjtu.edu.cn`

### 空数据

```
找到 0 个作业
```

**解决**: 
- 确认课程状态（只返回活跃课程）
- 检查时间范围是否正确
- 确认作业确实存在

### 超时

```
Command timed out
```

**解决**:
- 减少同步的课程数量
- 增加超时时间
- 使用并发请求

---

## 联系与支持

如有问题，请查看：
- 模块文档：`/work/src/canvas_integration/README.md`
- 实现计划：`/work/canvas_implementation_plan.md`
- 测试脚本：`/work/tests/test_canvas_quick.py`

---

**开发完成时间**: 2026-02-28  
**版本**: 1.0.0  
**开发者**: Machiatto
