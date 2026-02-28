#!/usr/bin/env python3
"""Canvas 集成测试脚本

测试 Canvas API 连接和基本功能。

使用方法:
    python test_canvas_connection.py
"""
import asyncio
import logging
import sys
import os
from pathlib import Path
from datetime import datetime

import pytest

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 加载 .env 文件
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    print(f"Loading environment from {env_path}...")
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("export "):
                # 解析 export VAR=value
                parts = line[7:].split("=", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().strip('"').strip("'")
                    os.environ[key] = value
                    print(f"  Set {key}={value[:10]}..." if "KEY" in key else f"  Set {key}={value}")
else:
    print(f"Warning: .env file not found at {env_path}")

from canvas_integration import CanvasConfig, CanvasClient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("CANVAS_ENABLE_LIVE_TESTS", "").lower() != "true",
        reason="这是依赖真实 Canvas API 的集成测试；设置 CANVAS_ENABLE_LIVE_TESTS=true 才执行",
    ),
]


async def test_connection():
    """测试 Canvas API 连接"""
    print("=" * 60)
    print("Canvas API 连接测试")
    print("=" * 60)
    
    # 1. 加载配置
    print("\n[1/5] 加载配置...")
    try:
        config = CanvasConfig.from_env()
        print(f"✓ 配置加载成功")
        print(f"  - Base URL: {config.base_url}")
        print(f"  - API Key: {config.api_key[:10]}...{config.api_key[-5:]}")
        print(f"  - Sync Enabled: {config.sync_enabled}")
        print(f"  - Days Ahead: {config.default_days_ahead}")
    except ValueError as e:
        print(f"✗ 配置加载失败：{e}")
        print("  请确保在 .env 文件中设置了 CANVAS_API_KEY")
        return False
    
    # 2. 验证配置
    print("\n[2/5] 验证配置...")
    if config.validate():
        print("✓ 配置验证通过")
    else:
        print("✗ 配置验证失败")
        return False
    
    # 3. 测试连接
    print("\n[3/5] 测试 API 连接...")
    try:
        async with CanvasClient(config) as client:
            # 获取用户信息
            print("  正在获取用户信息...")
            profile = await client.get_user_profile()
            
            print(f"✓ 连接成功!")
            print(f"  - 用户 ID: {profile.get('id')}")
            print(f"  - 姓名：{profile.get('name')}")
            print(f"  - 邮箱：{profile.get('login_id')}")
            
            # 4. 获取课程列表
            print("\n[4/5] 获取课程列表...")
            courses = await client.get_courses()
            print(f"✓ 找到 {len(courses)} 门课程")
            
            for i, course in enumerate(courses[:5], 1):  # 只显示前 5 门
                course_name = course.get('name', 'Unknown')
                course_code = course.get('course_code', '')
                print(f"  {i}. {course_name} ({course_code})")
            
            if len(courses) > 5:
                print(f"  ... 还有 {len(courses) - 5} 门课程")
            
            # 5. 获取即将到来的作业
            print("\n[5/5] 获取即将到来的作业...")
            assignments = await client.get_upcoming_assignments(days=30)
            print(f"✓ 找到 {len(assignments)} 个未来 30 天的作业")
            
            # 按截止时间排序
            assignments.sort(key=lambda a: a.due_at or datetime.max)
            
            for i, assignment in enumerate(assignments[:5], 1):  # 只显示前 5 个
                status_icon = "✓" if assignment.is_submitted else "○"
                due_str = assignment.due_at.strftime("%m-%d %H:%M") if assignment.due_at else "无截止时间"
                print(f"  {i}. {status_icon} {assignment.name}")
                print(f"     课程：{assignment.course_name}")
                print(f"     截止：{due_str}")
                if assignment.grade:
                    print(f"     成绩：{assignment.grade}")
            
            if len(assignments) > 5:
                print(f"  ... 还有 {len(assignments) - 5} 个作业")
            
            # 测试总结
            print("\n" + "=" * 60)
            print("测试总结")
            print("=" * 60)
            print(f"✓ 所有测试通过!")
            print(f"  - 用户：{profile.get('name')}")
            print(f"  - 课程：{len(courses)} 门")
            print(f"  - 作业：{len(assignments)} 个（未来 30 天）")
            print(f"  - 已提交：{sum(1 for a in assignments if a.is_submitted)} 个")
            print(f"  - 未提交：{sum(1 for a in assignments if not a.is_submitted)} 个")
            
            return True
            
    except Exception as e:
        print(f"✗ 测试失败：{e}")
        logger.exception("详细错误:")
        return False


async def test_sync():
    """测试同步功能"""
    print("\n" + "=" * 60)
    print("同步功能测试")
    print("=" * 60)
    
    from canvas_integration import CanvasSync
    
    config = CanvasConfig.from_env()
    
    async with CanvasClient(config) as client:
        sync = CanvasSync(client)
        
        print("\n准备同步未来 60 天的事件...")
        
        # 注意：当前实现中 _create_schedule_event 返回 None
        # 所以这里只测试数据获取和转换
        assignments = await client.get_upcoming_assignments(days=60)
        events = await client.get_upcoming_events(days=60)
        
        print(f"✓ 获取到 {len(assignments)} 个作业")
        print(f"✓ 获取到 {len(events)} 个日历事件")
        
        # 测试转换
        if assignments:
            event_data = sync._assignment_to_event(assignments[0])
            print(f"\n✓ 作业转换测试成功")
            print(f"  标题：{event_data['title']}")
            print(f"  优先级：{event_data['priority']}")
            print(f"  标签：{event_data['tags']}")
        
        print("\n注意：实际的日程创建需要 Agent 调用日程工具")
        print("      当前模块只负责数据获取和转换")
        
        return True


async def main():
    """主函数"""
    from datetime import datetime
    
    # 测试连接
    success = await test_connection()
    
    if success:
        # 测试同步
        await test_sync()
        
        print("\n" + "=" * 60)
        print("所有测试完成!")
        print("=" * 60)
        print("\n下一步:")
        print("1. 在 Agent 中集成 CanvasSync")
        print("2. 调用日程工具创建实际事件")
        print("3. 设置定时同步（建议每 6 小时一次）")
    else:
        print("\n测试失败，请检查配置和网络连接")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
