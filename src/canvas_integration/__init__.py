"""Canvas LMS 集成模块

为玛奇朵 Agent 提供 Canvas LMS 集成功能，自动抓取作业、考试和日历事件。

Example:
    >>> from canvas_integration import CanvasConfig, CanvasClient, CanvasSync
    >>>
    >>> # 加载配置
    >>> config = CanvasConfig.from_env()
    >>>
    >>> # 创建客户端并测试连接
    >>> async with CanvasClient(config) as client:
    ...     # 测试连接
    ...     if await client.test_connection():
    ...         # 获取用户信息
    ...         profile = await client.get_user_profile()
    ...         print(f"Hello, {profile['name']}!")
    ...
    ...         # 获取即将到来的作业
    ...         assignments = await client.get_upcoming_assignments(days=30)
    ...         for assignment in assignments:
    ...             print(f"{assignment.name} - Due: {assignment.due_at}")
    ...
    ...         # 同步到日程系统
    ...         sync = CanvasSync(client)
    ...         result = await sync.sync_to_schedule(days_ahead=60)
    ...         print(f"Synced {result.created_count} events")

Modules:
    config: 配置管理
    client: Canvas API 客户端
    models: 数据模型
    sync: 同步逻辑
"""

from .config import CanvasConfig
from .client import CanvasClient, CanvasAPIError, CanvasAuthError, CanvasRateLimitError
from .models import CanvasAssignment, CanvasEvent, SyncResult
from .sync import CanvasSync, sync_canvas_to_schedule

__version__ = "1.0.0"
__author__ = "Machiatto"

__all__ = [
    # 配置
    "CanvasConfig",
    
    # 客户端
    "CanvasClient",
    "CanvasAPIError",
    "CanvasAuthError",
    "CanvasRateLimitError",
    
    # 模型
    "CanvasAssignment",
    "CanvasEvent",
    "SyncResult",
    
    # 同步
    "CanvasSync",
    "sync_canvas_to_schedule",
]
