from __future__ import annotations

"""
飞书配置读取封装。

从全局 Config 中提取 FeishuConfig，供前端接入模块复用。
"""

from typing import Optional

from schedule_agent.config import Config, FeishuConfig, get_config


def get_feishu_config(config: Optional[Config] = None) -> FeishuConfig:
    """
    获取飞书配置。

    若传入 config，则直接使用；否则通过 get_config() 加载全局配置。
    """
    cfg = config or get_config()
    return cfg.feishu

