"""
配置管理模块

负责加载和验证 config.yaml 配置文件。
支持环境变量覆盖敏感配置。
"""

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


class SearchOptionsConfig(BaseModel):
    """联网搜索配置选项"""

    forced_search: bool = Field(
        default=False,
        description="是否强制联网搜索（默认模型自动判断）",
    )
    search_strategy: str = Field(
        default="turbo",
        description="搜索策略: turbo(默认) | max | agent | agent_max",
    )
    enable_source: bool = Field(
        default=False,
        description="是否返回搜索来源（仅 DashScope 协议支持）",
    )
    enable_citation: bool = Field(
        default=False,
        description="是否开启角标标注（需 enable_source=True）",
    )
    citation_format: str = Field(
        default="[<number>]",
        description="角标格式: [<number>] | [ref_<number>]",
    )
    enable_search_extension: bool = Field(
        default=False,
        description="是否开启垂域搜索（天气、股票等）",
    )
    freshness: Optional[int] = Field(
        default=None,
        description="搜索时效性（天数）: 7 | 30 | 180 | 365",
    )
    assigned_site_list: List[str] = Field(
        default_factory=list,
        description="限定搜索来源站点列表（最多25个）",
    )


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
    summary_model: Optional[str] = Field(
        default=None,
        description="用于总结/提炼的轻量模型（如 qwen-flash），为空则用主模型",
    )
    temperature: float = Field(default=0.7, ge=0, le=2, description="生成温度")
    max_tokens: int = Field(default=4096, ge=1, description="最大 token 数")
    enable_search: bool = Field(
        default=False,
        description="是否启用联网搜索功能（仅支持阿里云百炼 Qwen）",
    )
    search_options: Optional[SearchOptionsConfig] = Field(
        default=None,
        description="联网搜索配置选项",
    )
    enable_thinking: bool = Field(
        default=False,
        description="是否启用思考模式（用于网页抓取等功能，仅支持阿里云百炼 Qwen）",
    )
    enable_web_extractor: bool = Field(
        default=False,
        description="是否启用网页抓取功能（需 enable_search=true 和 enable_thinking=true，仅支持阿里云百炼 Qwen）",
    )


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


class FileToolsConfig(BaseModel):
    """文件读写工具配置"""

    enabled: bool = Field(
        default=True,
        description="是否启用文件读写工具",
    )
    allow_read: bool = Field(
        default=True,
        description="是否允许读取文件",
    )
    allow_write: bool = Field(
        default=False,
        description="是否允许写入/创建文件（需显式启用）",
    )
    allow_modify: bool = Field(
        default=False,
        description="是否允许修改/追加现有文件（需显式启用）",
    )
    base_dir: str = Field(
        default=".",
        description="允许操作的基础目录，所有文件路径必须在此目录下（安全限制）",
    )


class CommandToolsConfig(BaseModel):
    """命令执行工具配置"""

    enabled: bool = Field(
        default=True,
        description="是否启用 run_command 工具",
    )
    allow_run: bool = Field(
        default=True,
        description="是否允许执行终端命令",
    )
    base_dir: str = Field(
        default=".",
        description="命令允许执行的基础目录，cwd 必须在此目录下",
    )
    default_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="默认命令超时（秒）",
    )
    max_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="允许的最大 timeout（秒）",
    )
    default_output_limit: int = Field(
        default=12000,
        gt=0,
        description="默认输出限制（stdout+stderr 字符总数）",
    )
    max_output_limit: int = Field(
        default=200000,
        gt=0,
        description="允许的最大输出限制（字符）",
    )


class MCPServerConfig(BaseModel):
    """单个 MCP Server 配置。"""

    name: str = Field(..., description="MCP Server 名称，用于工具名前缀和日志定位")
    enabled: bool = Field(default=True, description="是否启用该 MCP Server")
    transport: str = Field(default="stdio", description="传输类型，当前仅支持 stdio")
    command: str = Field(..., description="启动 MCP Server 的命令")
    args: List[str] = Field(default_factory=list, description="MCP Server 命令参数")
    env: dict = Field(default_factory=dict, description="传递给 MCP Server 的环境变量")
    cwd: Optional[str] = Field(default=None, description="MCP Server 工作目录")
    tool_name_prefix: Optional[str] = Field(
        default=None,
        description="本地工具名前缀，默认使用 name",
    )
    init_timeout_seconds: int = Field(
        default=15,
        ge=1,
        description="初始化和获取工具列表超时时间（秒）",
    )
    call_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="工具调用超时时间（秒）",
    )


class MCPConfig(BaseModel):
    """MCP 客户端配置。"""

    enabled: bool = Field(default=False, description="是否启用 MCP 客户端")
    call_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="默认 MCP 工具调用超时时间（秒）",
    )
    servers: List[MCPServerConfig] = Field(
        default_factory=list,
        description="MCP Server 列表",
    )


class MemoryConfig(BaseModel):
    """记忆系统配置"""

    enabled: bool = Field(default=True, description="是否启用记忆系统")

    # 工作记忆
    max_working_tokens: int = Field(
        default=8000,
        ge=1000,
        description="工作记忆最大 token 数，超过阈值触发窗口总结",
    )
    working_summary_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=1.0,
        description="软阈值比例：tokens >= max_working_tokens * 此值且消息数 > keep_recent*2 时触发总结",
    )
    working_summary_hard_ratio: Optional[float] = Field(
        default=None,
        description="硬阈值比例：为 None 不启用；否则 tokens >= max_working_tokens * 此值时强制总结，不受消息条数限制",
    )
    working_keep_recent: int = Field(
        default=4,
        ge=1,
        description="工作记忆总结时保留的最近消息轮次数",
    )

    # 短期记忆
    short_term_k: int = Field(
        default=20,
        ge=1,
        description="短期记忆队列容量（最近 K 个会话摘要）",
    )
    short_term_dir: str = Field(
        default="./data/memory/short_term",
        description="短期记忆存储目录",
    )

    # 长期记忆
    long_term_dir: str = Field(
        default="./data/memory/long_term",
        description="长期记忆存储目录",
    )
    memory_md_path: str = Field(
        default="./MEMORY.md",
        description="核心人类可读记忆文件路径",
    )

    # 内容记忆
    content_dir: str = Field(
        default="./data/memory/content",
        description="内容记忆存储目录（Markdown 文档）",
    )

    # 检索策略
    recall_top_n: int = Field(
        default=5,
        ge=1,
        description="记忆检索返回的最大条目数",
    )
    recall_score_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="记忆检索的最低得分阈值",
    )
    force_recall: bool = Field(
        default=False,
        description="是否强制在每轮对话前执行记忆检索；默认关闭，由 runtime_memory 决策框架引导按需检索",
    )

    # QMD 集成
    qmd_enabled: bool = Field(
        default=False,
        description="是否启用 QMD 作为长期/内容记忆的语义检索后端",
    )
    qmd_command: str = Field(
        default="qmd",
        description="QMD CLI 命令路径",
    )


class SkillsConfig(BaseModel):
    """可选技能配置（prompts/skills/ + 可选的 Skills CLI 目录）"""

    enabled: List[str] = Field(
        default_factory=list,
        description="启用的技能名列表，对应 prompts/skills/{name}/SKILL.md",
    )
    cli_dir: Optional[str] = Field(
        default="~/.agents/skills",
        description="Skills CLI 安装目录（npx skills add -g 默认安装位置），技能仅从此目录读取",
    )


class AgentConfig(BaseModel):
    """Agent 配置"""

    max_iterations: int = Field(default=10, ge=1, description="最大工具调用迭代次数")
    enable_debug: bool = Field(default=False, description="是否启用调试模式")
    tool_mode: str = Field(
        default="full",
        description='工具暴露模式: full(全量暴露) | kernel(核心工具+工作集)',
    )
    working_set_size: int = Field(
        default=6,
        ge=0,
        description="kernel 模式下 LRU 工作集大小",
    )
    pinned_tools: List[str] = Field(
        default_factory=lambda: [
            "search_tools",
            "call_tool",
            "load_skill",  # 技能按需加载；仅 skills.enabled 时注册
            "read_file",
            "write_file",
            "run_command",
            "extract_web_content",
            "memory_search_long_term",
            "memory_search_content",
            "memory_store",
            "memory_ingest",
        ],
        description="kernel 模式下始终暴露给 LLM 的工具名列表（核心+已注册的 pinned）",
    )


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
    file_tools: FileToolsConfig = Field(
        default_factory=FileToolsConfig,
        description="文件读写工具配置",
    )
    command_tools: CommandToolsConfig = Field(
        default_factory=CommandToolsConfig,
        description="命令执行工具配置",
    )
    mcp: MCPConfig = Field(
        default_factory=MCPConfig,
        description="MCP 客户端配置",
    )
    memory: MemoryConfig = Field(
        default_factory=MemoryConfig,
        description="记忆系统配置",
    )
    skills: SkillsConfig = Field(
        default_factory=SkillsConfig,
        description="可选技能配置（load/unload）",
    )


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
            env_summary = os.environ.get("QWEN_SUMMARY_MODEL")
            if env_summary:
                raw_config["llm"]["summary_model"] = env_summary
        else:
            # 豆包
            env_api_key = os.environ.get("DOUBAO_API_KEY")
            if env_api_key:
                raw_config["llm"]["api_key"] = env_api_key
            env_model = os.environ.get("DOUBAO_MODEL")
            if env_model:
                raw_config["llm"]["model"] = env_model

    # 兼容旧配置：user 已迁移至 prompts/system/user.md
    raw_config.pop("user", None)

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
