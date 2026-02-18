"""
配置管理模块

负责加载和验证 config.yaml 配置文件。
支持环境变量覆盖敏感配置。
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """LLM 配置"""

    provider: str = Field(
        default="doubao",
        description="LLM 提供商: doubao(豆包) | qwen(阿里云百炼)",
    )
    api_key: str = Field(..., description="API 密钥")
    base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3",
        description="API 基础 URL",
    )
    model: str = Field(..., description="模型名称或推理端点 ID")
    temperature: float = Field(default=0.7, ge=0, le=2, description="生成温度")
    max_tokens: int = Field(default=4096, ge=1, description="最大 token 数")


class TimeConfig(BaseModel):
    """时间配置"""

    timezone: str = Field(default="Asia/Shanghai", description="时区")
    sleep_start: str = Field(default="23:00", description="睡眠开始时间")
    sleep_end: str = Field(default="08:00", description="睡眠结束时间")


class StorageConfig(BaseModel):
    """存储配置"""

    type: str = Field(default="json", description="存储类型")
    data_dir: str = Field(default="./data", description="数据目录")
    events_file: str = Field(default="events.json", description="事件文件名")
    tasks_file: str = Field(default="tasks.json", description="任务文件名")


class AgentConfig(BaseModel):
    """Agent 配置"""

    max_iterations: int = Field(default=10, ge=1, description="最大工具调用迭代次数")
    enable_debug: bool = Field(default=False, description="是否启用调试模式")


class LoggingConfig(BaseModel):
    """日志配置"""

    session_log_dir: str = Field(
        default="./logs/sessions",
        description="Session 日志目录",
    )
    enable_session_log: bool = Field(
        default=True,
        description="是否启用 session 日志",
    )
    enable_detailed_log: bool = Field(
        default=False,
        description="是否记录完整 prompt",
    )
    max_system_prompt_log_len: int = Field(
        default=2000,
        ge=0,
        description="详细模式下 system prompt 截断长度",
    )


class Config(BaseModel):
    """应用配置"""

    llm: LLMConfig
    time: TimeConfig = Field(default_factory=TimeConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def find_config_file() -> Path:
    """
    查找配置文件。

    查找顺序：
    1. 当前工作目录下的 config.yaml
    2. 项目根目录下的 config.yaml

    Returns:
        配置文件路径

    Raises:
        FileNotFoundError: 未找到配置文件
    """
    # 当前工作目录
    cwd_config = Path.cwd() / "config.yaml"
    if cwd_config.exists():
        return cwd_config

    # 项目根目录（src 的父目录）
    project_root = Path(__file__).parent.parent.parent
    project_config = project_root / "config.yaml"
    if project_config.exists():
        return project_config

    raise FileNotFoundError(
        "未找到配置文件 config.yaml。"
        "请复制 config.example.yaml 为 config.yaml 并填写配置。"
    )


def load_config(config_path: Optional[Path] = None) -> Config:
    """
    加载配置文件。

    Args:
        config_path: 配置文件路径，如果为 None 则自动查找

    Returns:
        Config 对象

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置文件格式错误
    """
    if config_path is None:
        config_path = find_config_file()

    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    if raw_config is None:
        raise ValueError(f"配置文件为空: {config_path}")

    # 支持环境变量覆盖敏感配置
    if "llm" in raw_config:
        provider = raw_config["llm"].get("provider", "doubao")
        if provider == "qwen":
            # 阿里云百炼 Qwen：默认 base_url 为 OpenAI 兼容端点，支持多轮工具调用
            raw_config["llm"].setdefault(
                "base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
            env_api_key = os.environ.get("QWEN_API_KEY") or os.environ.get(
                "DASHSCOPE_API_KEY"
            )
            if env_api_key:
                raw_config["llm"]["api_key"] = env_api_key
            env_model = os.environ.get("QWEN_MODEL")
            if env_model:
                raw_config["llm"]["model"] = env_model
        else:
            # 豆包
            env_api_key = os.environ.get("DOUBAO_API_KEY")
            if env_api_key:
                raw_config["llm"]["api_key"] = env_api_key
            env_model = os.environ.get("DOUBAO_MODEL")
            if env_model:
                raw_config["llm"]["model"] = env_model

    return Config(**raw_config)


# 全局配置实例（延迟加载）
_config: Optional[Config] = None


def get_config() -> Config:
    """
    获取全局配置实例。

    Returns:
        Config 对象
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """重置全局配置实例（用于测试）"""
    global _config
    _config = None
