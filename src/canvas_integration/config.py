"""Canvas LMS 集成配置管理"""
import os
from dataclasses import dataclass
from typing import Optional


# 统一的默认 Base URL 常量
DEFAULT_BASE_URL = "https://oc.sjtu.edu.cn/api/v1"


@dataclass
class CanvasConfig:
    """Canvas API 配置类
    
    Attributes:
        api_key: Canvas API 密钥
        base_url: Canvas API 基础 URL
        sync_enabled: 是否启用同步
        sync_interval_hours: 同步间隔（小时）
        default_days_ahead: 默认同步未来多少天的事件
    """
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    sync_enabled: bool = True
    sync_interval_hours: int = 6
    default_days_ahead: int = 60
    
    @classmethod
    def from_env(cls) -> "CanvasConfig":
        """从环境变量加载配置
        
        Returns:
            CanvasConfig 配置实例
            
        Raises:
            ValueError: 当 CANVAS_API_KEY 未设置时
        """
        api_key = os.getenv("CANVAS_API_KEY")
        if not api_key:
            raise ValueError(
                "CANVAS_API_KEY environment variable not set. "
                "Please add it to your .env file."
            )
        
        return cls(
            api_key=api_key,
            base_url=os.getenv("CANVAS_BASE_URL", DEFAULT_BASE_URL),
            sync_enabled=os.getenv(
                "CANVAS_SYNC_ENABLED", "true"
            ).lower() == "true",
            sync_interval_hours=int(os.getenv(
                "CANVAS_SYNC_INTERVAL_HOURS", "6"
            )),
            default_days_ahead=int(os.getenv(
                "CANVAS_DEFAULT_DAYS_AHEAD", "60"
            )),
        )
    
    def validate(self) -> bool:
        """验证配置是否有效
        
        Returns:
            bool: 配置是否有效
        """
        if not self.api_key or len(self.api_key) < 10:
            return False
        if not self.base_url.startswith("http"):
            return False
        return True
