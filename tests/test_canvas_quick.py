#!/usr/bin/env python3
"""Canvas 集成快速测试"""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

# 加载环境变量
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("export "):
                parts = line[7:].split("=", 1)
                if len(parts) == 2:
                    os.environ[parts[0].strip()] = parts[1].strip().strip('"').strip("'")

sys.path.insert(0, str(Path(__file__).parent.parent))

from canvas_integration import CanvasConfig, CanvasClient

async def quick_test():
    """快速测试核心功能"""
    print("=" * 60)
    print("Canvas 集成快速测试")
    print("=" * 60)
    
    config = CanvasConfig.from_env()
    
    async with CanvasClient(config) as client:
        # 1. 测试连接
        print("\n[1/3] 测试连接...")
        profile = await client.get_user_profile()
        print(f"✓ 用户：{profile['name']} ({profile['login_id']})")
        
        # 2. 获取课程
        print("\n[2/3] 获取课程...")
        courses = await client.get_courses()
        print(f"✓ 共 {len(courses)} 门课程")
        
        # 取前 3 门课程测试
        test_courses = courses[:3]
        for course in test_courses:
            print(f"  - {course['name']}")
        
        # 3. 获取作业
        print("\n[3/3] 获取作业（前 3 门课程）...")
        all_assignments = []
        for course in test_courses:
            assignments = await client.get_assignments(course['id'])
            all_assignments.extend(assignments)
            print(f"  - {course['name']}: {len(assignments)} 个作业")
        
        # 总结
        print("\n" + "=" * 60)
        print("测试结果")
        print("=" * 60)
        print(f"✓ 用户：{profile['name']}")
        print(f"✓ 课程：{len(courses)} 门")
        print(f"✓ 作业：{len(all_assignments)} 个（前 3 门课程）")
        
        # 显示即将到来的作业
        upcoming = [a for a in all_assignments if a.due_at and not a.is_submitted]
        upcoming.sort(key=lambda x: x.due_at)
        
        if upcoming:
            print(f"\n即将到来的作业（未提交）:")
            for i, a in enumerate(upcoming[:5], 1):
                due = a.due_at.strftime("%m-%d %H:%M")
                print(f"  {i}. {a.name} (截止：{due})")
        
        print("\n✓ Canvas 集成模块工作正常!")
        return True

if __name__ == "__main__":
    asyncio.run(quick_test())
