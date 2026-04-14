"""
配置管理模块

负责加载和验证 config.yaml 配置文件。
支持环境变量覆盖敏感配置。
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


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
    request_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="LLM 请求超时（秒）",
    )
    stream: bool = Field(
        default=False,
        description="是否使用流式输出（推荐在思考模式下开启）",
    )
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
    thinking_budget: Optional[int] = Field(
        default=None,
        ge=1,
        description="思考预算 token 上限（仅部分模型支持）",
    )
    enable_web_extractor: bool = Field(
        default=False,
        description="是否启用网页抓取功能（需 enable_search=true 和 enable_thinking=true，仅支持阿里云百炼 Qwen）",
    )


class MultimodalConfig(BaseModel):
    """多模态（识图）配置。

    现在推荐的用法是：
    - 使用 attach_media 工具声明需要在下一轮对话中附带的图片/视频
    - 由运行时在下一次 LLM 调用前，将这些媒体编码为多模态 messages 的一部分，
      让当前主模型在同一条推理链中同时理解文字与图像/视频内容。
    """

    enabled: bool = Field(
        default=False,
        description="是否启用多模态识图工具",
    )
    model: Optional[str] = Field(
        default=None,
        description="多模态模型名，未配置时复用 llm.model",
    )
    max_image_size_mb: float = Field(
        default=8.0,
        gt=0,
        description="本地图片最大大小（MB），超过则拒绝",
    )
    request_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="识图请求超时（秒），未实现单独超时时由 LLM 全局超时控制",
    )


class CanvasIntegrationConfig(BaseModel):
    """Canvas 集成配置"""

    enabled: bool = Field(
        default=False,
        description="是否启用 Canvas 同步工具",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="Canvas API Key（可为空并改用环境变量 CANVAS_API_KEY）",
    )
    base_url: str = Field(
        default="https://oc.sjtu.edu.cn/api/v1",
        description="Canvas API Base URL",
    )
    default_days_ahead: int = Field(
        default=60,
        ge=1,
        description="默认同步未来多少天的数据",
    )
    include_submitted: bool = Field(
        default=False,
        description="默认是否同步已提交作业",
    )


class SjtuJwConfig(BaseModel):
    """上海交通大学教学信息服务网课表同步配置"""

    cookies_path: str = Field(
        default="./data/sjtu_jw_cookies.json",
        description="从浏览器或 Playwright 导出的教学信息服务网 Cookie JSON 文件路径",
    )


class ShuiyuanMemoryConfig(BaseModel):
    """水源社区记忆配置（每用户独立 DB，无长期记忆）"""

    chat_limit_per_user: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="每用户保留的最近聊天记录条数",
    )
    thread_posts_count: int = Field(
        default=50,
        ge=10,
        le=200,
        description="回复时读取的该楼最近帖子上文条数",
    )
    tool_max_posts: int = Field(
        default=50,
        ge=10,
        le=100,
        description="水源工具（search/get_topic）返回结果的最大帖子数，避免上下文过长",
    )


class ShuiyuanRateLimitConfig(BaseModel):
    """水源社区限流配置"""

    replies_per_minute: int = Field(
        default=5,
        ge=1,
        le=60,
        description="每用户每分钟最多回复次数",
    )


class ShuiyuanConfig(BaseModel):
    """水源社区（上海交通大学 Discourse 论坛）配置"""

    enabled: bool = Field(
        default=False,
        description="是否启用水源社区工具；需配置 user_api_key / user_api_keys 或环境变量 SHUIYUAN_USER_API_KEY",
    )
    user_api_key: Optional[str] = Field(
        default=None,
        description="水源社区 User-Api-Key，用于 API 认证；优先使用环境变量 SHUIYUAN_USER_API_KEY",
    )
    user_api_keys: List[str] = Field(
        default_factory=list,
        description="可选：多个水源社区 User-Api-Key；当某个 Key 触发日级限流时，自动切换到下一把 Key",
    )
    site_url: str = Field(
        default="https://shuiyuan.sjtu.edu.cn",
        description="水源社区站点 URL",
    )
    db_base_dir: str = Field(
        default="./data/shuiyuan",
        description="水源社区数据根目录，每用户 DB 为 {base}/users/{username}/shuiyuan.db",
    )
    memory: ShuiyuanMemoryConfig = Field(
        default_factory=ShuiyuanMemoryConfig,
        description="水源社区记忆配置",
    )
    rate_limit: ShuiyuanRateLimitConfig = Field(
        default_factory=ShuiyuanRateLimitConfig,
        description="水源社区限流配置",
    )
    owner_username: Optional[str] = Field(
        default=None,
        description="主人水源用户名，调用时需被 @ 此用户才触发",
    )
    invocation_trigger: str = Field(
        default="【玛奇朵】",
        description="消息中必须包含此字符串才触发回复（同时需 @ 主人）",
    )
    allowed_topic_ids: List[int] = Field(
        default_factory=list,
        description="仅在这些话题中响应 @。非空时使用 topic 监控模式（解析正文 @owner+trigger），不依赖 user_actions/notifications；为空时使用 user_actions+notifications 模式",
    )


class TimeConfig(BaseModel):
    """时间配置"""

    timezone: str = Field(default="Asia/Shanghai", description="时区")
    sleep_start: str = Field(default="23:00", description="睡眠开始时间")
    sleep_end: str = Field(default="08:00", description="睡眠结束时间")


class PlanningWorkingHoursConfig(BaseModel):
    """单条工作时段配置。"""

    weekday: int = Field(
        ...,
        ge=1,
        le=7,
        description="星期几（1=周一，7=周日）",
    )
    start: str = Field(..., description="开始时间（HH:MM）")
    end: str = Field(..., description="结束时间（HH:MM）")


class PlanningWeightsConfig(BaseModel):
    """规划评分权重配置。"""

    urgency: float = Field(default=0.4, ge=0.0, description="DDL 紧迫度权重")
    difficulty: float = Field(default=0.3, ge=0.0, description="任务难度权重")
    importance: float = Field(default=0.3, ge=0.0, description="用户重视度权重")
    overdue_bonus: float = Field(default=0.2, ge=0.0, description="逾期加权项")


class PlanningConfig(BaseModel):
    """任务规划配置。"""

    timezone: str = Field(default="Asia/Shanghai", description="规划时区")
    lookahead_days: int = Field(
        default=7,
        ge=1,
        description="默认规划窗口天数",
    )
    min_block_minutes: int = Field(
        default=30,
        ge=1,
        description="最小时间块（分钟）",
    )
    break_minutes_after_task: int = Field(
        default=15,
        ge=0,
        description="每个任务后的休息时间（分钟），0 表示不插入休息",
    )
    prefer_weekday_slots: bool = Field(
        default=True,
        description="是否优先使用工作日时段（周一到周五），周末仅作补充",
    )
    working_hours: List[PlanningWorkingHoursConfig] = Field(
        default_factory=list,
        description="每周工作时段配置",
    )
    weights: PlanningWeightsConfig = Field(
        default_factory=PlanningWeightsConfig,
        description="规划评分权重",
    )


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
        description="相对路径的基准目录；绝对路径（如 /etc、~/.config）可访问任意位置",
    )


class CommandToolsConfig(BaseModel):
    """命令执行工具配置（持久化 Bash 会话）"""

    enabled: bool = Field(
        default=True,
        description="是否启用 bash 工具",
    )
    allow_run: bool = Field(
        default=True,
        description="是否允许执行终端命令",
    )
    allow_run_for_subagent: bool = Field(
        default=False,
        description="是否允许受限模式（如 subagent）使用 bash；开启后仅可执行 subagent_command_whitelist 内命令，禁止管道/重定向与危险命令",
    )
    subagent_command_whitelist: List[str] = Field(
        default_factory=lambda: [
            "ls",
            "pwd",
            "cat",
            "head",
            "tail",
            "grep",
            "find",
            "echo",
            "which",
            "file",
            "stat",
            "wc",
            "date",
            "whoami",
            "id",
            "env",
            "printenv",
        ],
        description="受限模式（sub）允许的命令白名单（仅非破坏性只读命令；禁止管道、重定向、危险命令）",
    )
    base_dir: str = Field(
        default=".",
        description="bash 会话初始工作目录；开启工作区隔离时仅作未隔离/超级管理员模式下的 cwd",
    )
    workspace_base_dir: str = Field(
        default="./data/workspace",
        description=(
            "每用户数据根（单元格）的父目录：实际路径为 {base}/{frontend}/{user}/；"
            "该目录同时作为隔离 bash 的 cwd、HOME 与 cd 牢笼根（不再嵌套 .sandbox_home）"
        ),
    )
    workspace_isolation_enabled: bool = Field(
        default=True,
        description="为 True 时各（非管理员）Core 的 bash 初始 cwd 落在 workspace_base_dir 下对应用户目录，并注入 cd 防护",
    )
    workspace_admin_memory_owners: List[str] = Field(
        default_factory=list,
        description=(
            "具有 bash 全盘工作目录权限的 memory_owner 列表（形如 cli:root、feishu:ou_xxx）；"
            "亦可对单个 Core 设置 CoreProfile.bash_workspace_admin"
        ),
    )
    bash_extra_write_roots: List[str] = Field(
        default_factory=list,
        description=(
            "全局额外可写路径前缀（已 resolve）；~ 展开；相对路径相对仓库根。"
            "与每用户 data/acl 下持久列表及 jail/tmp 合并后供 bash 与工作区写校验使用"
        ),
    )
    acl_base_dir: str = Field(
        default="./data/acl",
        description="每用户可写前缀持久化目录（writable_roots.json），勿放在 data/workspace 下",
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
    shell_path: str = Field(
        default="/bin/bash",
        description="bash 可执行文件路径",
    )
    init_commands: List[str] = Field(
        default_factory=list,
        description="bash 启动时执行的初始化命令列表",
    )
    bash_real_home_path_suffixes: List[str] = Field(
        default_factory=list,
        description=(
            "隔离 bash 的「类 Linux 用户」PATH：除内置宿主目录外，追加相对 MACCHIATO_REAL_HOME 的 bin 路径"
            "（目录存在才加入）；用于 pnpm/fnm 等非标准前缀，勿含 .. 或绝对路径"
        ),
    )
    snapshot_enabled: bool = Field(
        default=False,
        description="Core evict 时是否写入 bash 环境快照（用于恢复）",
    )
    snapshot_dir: str = Field(
        default="./data/bash_snapshots",
        description="bash 快照文件存储目录",
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
    init_retries: int = Field(
        default=2,
        ge=0,
        description="初始化失败时的重试次数（0=不重试，仅尝试一次）",
    )
    init_retry_delay_seconds: float = Field(
        default=2.0,
        ge=0,
        description="重试前等待秒数",
    )
    call_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="工具调用超时时间（秒）",
    )


class MCPConfig(BaseModel):
    """MCP 客户端配置。"""

    enabled: bool = Field(default=False, description="是否启用 MCP 客户端")
    inject_builtin_schedule_mcp: bool = Field(
        default=False,
        description=(
            "若为 True：启用 MCP 且 servers 中未配置本地 mcp_server.py 时，"
            "自动追加 schedule_tools stdio（每 AgentCore 一子进程）。"
            "进程内 Agent 默认用 ToolRegistry 即可，无需此项；"
            "仅在为 Claude Desktop 等纯 MCP 宿主暴露日程工具时打开。"
        ),
    )
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

    memory_base_dir: str = Field(
        default="./data/memory",
        description="记忆库根目录；各 owner 路径为 {base}/{frontend}/{user}/ 下含 content/、long_term/、chat_history.db",
    )

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
        description="[已废弃] 短期记忆已移除，会话摘要直接写入 long_term recent_topics",
    )
    long_term_dir: str = Field(
        default="./data/memory/long_term",
        description="[已废弃，由 memory_base_dir 推导] 长期记忆目录",
    )
    memory_md_path: str = Field(
        default="",
        description="[已废弃] 空则用 {base}/{frontend}/{user}/long_term/MEMORY.md",
    )
    chat_history_db_path: str = Field(
        default="./data/memory/chat_history.db",
        description="[已废弃，由 memory_base_dir 推导] 对话历史库路径",
    )
    content_dir: str = Field(
        default="./data/memory/content",
        description="[已废弃，由 memory_base_dir 推导] 内容记忆目录",
    )

    # Session 切分
    idle_timeout_minutes: int = Field(
        default=30,
        ge=1,
        description="用户无操作超过此分钟数后，下次输入前自动切分 session",
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
        description=(
            "非工作区隔离（或 bash 工作区管理员）时 Skills CLI 根目录；"
            "开启隔离且非管理员时实际使用 {workspace_base_dir}/{frontend}/{user}/.agents/skills，与此处默认的进程主目录 ~/.agents/skills 脱钩"
        ),
    )


class ToolTemplateConfig(BaseModel):
    """单个工具模板配置。"""

    exposure: Literal["pinned", "empty"] = Field(
        default="pinned",
        description="工具初始暴露模式：pinned=暴露 core_tools+pinned_tools+extra；empty=仅暴露 core_tools+extra",
    )
    extra: List[str] = Field(
        default_factory=list,
        description="该模板专属追加的工具名列表",
    )


def _default_tool_templates() -> Dict[str, "ToolTemplateConfig"]:
    return {
        "default": ToolTemplateConfig(exposure="pinned", extra=[]),
        "shuiyuan": ToolTemplateConfig(
            exposure="empty",
            extra=[
                "shuiyuan_search",
                "shuiyuan_get_topic",
                "shuiyuan_browse_topic",
                "shuiyuan_get_latest",
                "shuiyuan_get_top",
                "shuiyuan_get_categories",
                "shuiyuan_get_category_topics",
                "shuiyuan_post_retort",
                "attach_image_to_reply",
            ],
        ),
        "cron": ToolTemplateConfig(
            exposure="empty",
            extra=[
                "parse_time",
                "add_event",
                "add_task",
                "get_events",
                "get_tasks",
                "update_event",
                "update_task",
                "delete_schedule_data",
                "get_free_slots",
                "plan_tasks",
                "sync_sources",
                "get_sync_status",
                "get_digest",
                "list_notifications",
                "ack_notification",
                "configure_automation_policy",
                "get_automation_activity",
                "create_scheduled_job",
                "list_scheduled_jobs",
                "delete_scheduled_job",
                "notify_owner",
            ],
        ),
    }


class ToolsConfig(BaseModel):
    """工具模板与初始暴露配置。"""

    core_tools: List[str] = Field(
        default_factory=lambda: ["search_tools", "call_tool", "bash", "request_permission"],
        description="所有 Core 固定携带的核心工具",
    )
    pinned_tools: List[str] = Field(
        default_factory=lambda: [
            "load_skill",
            "web_search",
            "read_file",
            "write_file",
            "modify_file",
            "extract_web_content",
            "attach_media",
            "memory_search_long_term",
            "memory_search_content",
            "memory_store",
            "memory_ingest",
            "list_scheduled_jobs",
            "delete_scheduled_job",
        ],
        description="默认模板在 exposure=pinned 时额外始终暴露给 LLM 的工具名列表",
    )
    templates: Dict[str, ToolTemplateConfig] = Field(
        default_factory=_default_tool_templates,
        description="按模板名定义的工具暴露与专属工具配置",
    )

    def get_template(self, name: Optional[str]) -> ToolTemplateConfig:
        template_name = (name or "default").strip() or "default"
        return self.templates.get(template_name) or self.templates.get(
            "default", ToolTemplateConfig()
        )

    def resolve_initial_tools(self, template_name: Optional[str]) -> List[str]:
        template = self.get_template(template_name)
        names: List[str] = list(self.core_tools)
        if template.exposure == "pinned":
            names.extend(self.pinned_tools)
        names.extend(template.extra)
        deduped: List[str] = []
        for name in names:
            norm = str(name).strip()
            if norm and norm not in deduped:
                deduped.append(norm)
        return deduped


class AgentConfig(BaseModel):
    """Agent 配置"""

    max_iterations: int = Field(default=10, ge=1, description="最大工具调用迭代次数")
    subagent_max_seconds: int = Field(
        default=600,
        ge=1,
        description="子 Agent 单次运行最大时长（秒），超时后强制终止",
    )
    subagent_max_tokens: Optional[int] = Field(
        default=500_000,
        ge=1,
        description="子 Agent 单次运行累计 token 上限，超限后强制结束；None 表示不限制",
    )
    subagent_max_iterations: int = Field(
        default=15,
        ge=1,
        description="子 Agent 默认最大迭代次数（工具未传 max_iterations 时使用）",
    )
    subagent_max_context_tokens: Optional[int] = Field(
        default=None,
        description="子 Agent 上下文压缩阈值（profile.max_context_tokens）；None 表示不设 profile 层上限，仅受 working memory 约束",
    )
    p2p_reply_timeout_seconds: int = Field(
        default=300,
        ge=1,
        description="send_message_to_agent(require_reply=True) 阻塞等待对方 reply_to_message 的最长秒数，超时则工具失败",
    )
    subagent_wait_timeout_seconds: int = Field(
        default=300,
        ge=1,
        description="wait_subagent / wait_for_agent_message 默认最长阻塞等待秒数",
    )
    subagent_zombie_ttl_seconds: Optional[float] = Field(
        default=None,
        description=(
            "终态子会话 zombie 超过该秒数后由系统 reap_zombie 兜底回收（None 表示不启用）；"
            "建议生产环境设较大值（如 7 天）或 None，避免与父侧多轮协作竞态"
        ),
    )
    list_agents_allow_namespace_for_subagent: bool = Field(
        default=False,
        description="mode=sub 时是否允许 list_agents(scope=namespace) 查看同命名空间会话；默认仅 my_children/siblings",
    )
    enable_debug: bool = Field(default=False, description="是否启用调试模式")
    working_set_size: int = Field(
        default=6,
        ge=0,
        description="工具工作集大小（search_tools 将命中的工具加入该 LRU 集合）",
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


class UIConfig(BaseModel):
    """CLI 可视化配置"""

    show_draft: str = Field(
        default="summary",
        description="草稿显示模式: off | summary | full",
    )
    draft_max_chars: int = Field(
        default=500,
        ge=50,
        description="summary 模式下草稿最大显示字符数",
    )
    dim_draft: bool = Field(
        default=True,
        description="是否使用暗色样式显示草稿",
    )


class AutomationJobConfig(BaseModel):
    """单个自动化定时任务配置。

    这是一个高层配置入口，供用户在 config.yaml 中用以下几种方式声明后台定时任务：
    1. “任务描述 + 间隔时间”（interval）
    2. “任务描述 + 每天单个时刻”（daily_time）
    3. “任务描述 + 每天多个时刻”（times）
    4. “任务描述 + 起始时刻 + 间隔时间”（start_time + interval）
    5. “任务描述 + 一次性触发时间”（run_at / once_at，触发一次后自动停用）
    加载时会被转换为 automation 子系统中的 JobDefinition。
    """

    name: str = Field(
        ...,
        description="任务的稳定标识名，与 memory_owner 一起用于 config 同步时的匹配键；对应落盘后的 payload_template.name。",
    )
    description: str = Field(
        ...,
        description="任务触发时给 Agent 的自然语言指令，例如“请调用 sync_sources(source='email') 并输出操作+结果”。",
    )
    interval_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        description="任务执行间隔（分钟）。仅在 interval 模式或与 start_time 搭配时必填；若已配置 daily_time/times，则可以省略。",
    )
    daily_time: Optional[str] = Field(
        default=None,
        description="可选：每天触发的本地时间（HH:MM，采用 time.timezone 时区）。设置后语义为“每天这个时间点执行一次”。",
    )
    times: Optional[List[str]] = Field(
        default=None,
        description='可选：每天多个固定触发时间（HH:MM）列表，例如 ["08:00", "14:00", "20:00"]。若设置则优先于 daily_time。',
    )
    start_time: Optional[str] = Field(
        default=None,
        description="可选：起始时间（HH:MM），与 interval_minutes 搭配，表示“从 start_time 开始，每隔 interval_minutes 分钟触发一次”。",
    )
    run_at: Optional[str] = Field(
        default=None,
        description="可选：一次性触发时间（ISO-8601），例如 2026-03-09T21:30:00+08:00。触发一次后自动停用。",
    )
    one_shot: bool = Field(
        default=False,
        description="是否按一次性任务执行。若提供 run_at 则会自动视为 true。",
    )
    user_id: str = Field(
        default="default",
        description="逻辑用户 ID，用于区分不同用户的后台任务（通常与记忆库 owner 的 user 段一致，如 cli:root 中的 root）。",
    )
    memory_owner: Optional[str] = Field(
        default=None,
        description=(
            '可选：记忆库 owner 标识，例如 "cli:root"、"feishu:some_user"。'
            "配置后，自动化任务将在该 owner 的上下文和记忆下运行；未配置时，不加载任何长期/内容/对话历史记忆。"
        ),
    )
    core_mode: Optional[Literal["full", "sub", "background", "cron", "heartbeat"]] = (
        Field(
            default=None,
            description=(
                "可选：CoreProfile.mode 权限模式。"
                "推荐使用 full/sub/background；为兼容旧配置，仍接受 cron/heartbeat，"
                "但会在内部统一映射为 background。未配置时，自动化队列默认使用 background。"
            ),
        )
    )
    tool_template: Optional[str] = Field(
        default=None,
        description="可选：工具模板名，例如 default / shuiyuan / cron；未配置时按 Core 创建入口自动推导。",
    )
    enabled: bool = Field(
        default=True,
        description="是否启用该任务。",
    )

    @model_validator(mode="before")
    @classmethod
    def _interval_time_alias(cls, data: Any) -> Any:
        """兼容 config 里写的 interval_time（与 interval_minutes 同义）。"""
        if isinstance(data, dict):
            out = dict(data)
            if "interval_minutes" not in out and "interval_time" in out:
                out["interval_minutes"] = out.get("interval_time")
            if "run_at" not in out and "once_at" in out:
                out["run_at"] = out.get("once_at")
            if out.get("run_at"):
                out["one_shot"] = True
            data = out
        return data


class AutomationConfig(BaseModel):
    """自动化定时任务整体配置。"""

    jobs: List[AutomationJobConfig] = Field(
        default_factory=list,
        description="通过配置声明的自动化定时任务列表。",
    )


class FeishuConfig(BaseModel):
    """飞书集成配置。

    用于在飞书机器人中接入 Schedule Agent。所有字段均为可选，默认关闭。
    """

    enabled: bool = Field(
        default=False,
        description="是否启用飞书集成（推荐通过 feishu_ws_gateway.py 对外提供服务）",
    )
    app_id: Optional[str] = Field(
        default=None,
        description="飞书应用的 App ID，可通过环境变量 FEISHU_APP_ID 覆盖",
    )
    app_secret: Optional[str] = Field(
        default=None,
        description="飞书应用的 App Secret，可通过环境变量 FEISHU_APP_SECRET 覆盖",
    )
    verification_token: Optional[str] = Field(
        default=None,
        description="飞书事件订阅 Verification Token，可通过环境变量 FEISHU_VERIFICATION_TOKEN 覆盖",
    )
    encrypt_key: Optional[str] = Field(
        default=None,
        description="飞书事件订阅 Encrypt Key（启用加密时必填），可通过环境变量 FEISHU_ENCRYPT_KEY 覆盖",
    )
    base_url: str = Field(
        default="https://open.feishu.cn",
        description="飞书开放平台 Base URL，国际版可配置为 https://open.larksuite.com",
    )
    domain: str = Field(
        default="feishu",
        description="部署区域标识: feishu(中国大陆版) | lark(国际版) 等，用于日志与后续扩展。",
    )
    timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="调用飞书开放平台 API 的默认超时时间（秒）。",
    )
    automation_ipc_timeout_seconds: float = Field(
        default=1800.0,
        gt=0,
        description=(
            "飞书网关/HTTP 回调与 automation_daemon 之间 Unix socket 流式 IPC 的单次读超时（秒）。"
            "每次等待下一行 JSON（含长工具执行期间无输出）不得超过此时长；与 llm.request_timeout_seconds（LLM HTTP）无关。"
        ),
    )
    automation_activity_enabled: bool = Field(
        default=False,
        description="是否将 automation_activity.jsonl 中的活动简报推送到飞书。",
    )
    automation_activity_chat_id: Optional[str] = Field(
        default=None,
        description="用于接收 automation 活动通知的飞书 chat_id；仅在 automation_activity_enabled=true 且非空时生效。",
    )
    tool_trace_cards_enabled: bool = Field(
        default=True,
        description="是否在飞书会话中推送每次工具调用的交互卡片（Input/Result），便于对齐 CLI 中间输出。",
    )
    reply_format: str = Field(
        default="markdown_card",
        description="Agent 最终回复格式：plain=纯文本（历史行为）；markdown_card=交互卡片内 Markdown 渲染。",
    )
    assistant_reply_stream: bool = Field(
        default=True,
        description=(
            "reply_format=markdown_card 时，是否在生成过程中用 PATCH 流式更新同一条助手卡片；"
            "关闭则仅在整段生成完成后发一条（与 plain 类似的「一次性」体验）。"
        ),
    )
    assistant_cardkit_stream: bool = Field(
        default=True,
        description=(
            "assistant_reply_stream 时是否使用飞书 CardKit 官方流式（创建卡片实体 + PUT 流式更新文本）；"
            "需应用具备「创建与更新卡片 cardkit:card:write」。失败时自动回退为消息内嵌 JSON + PATCH。"
        ),
    )


class Config(BaseModel):
    """应用配置"""

    llm: LLMConfig
    multimodal: MultimodalConfig = Field(
        default_factory=MultimodalConfig,
        description="多模态识图配置",
    )
    canvas: CanvasIntegrationConfig = Field(
        default_factory=CanvasIntegrationConfig,
        description="Canvas 集成配置",
    )
    time: TimeConfig = Field(default_factory=TimeConfig)
    planning: PlanningConfig = Field(
        default_factory=PlanningConfig,
        description="任务规划配置",
    )
    storage: StorageConfig = Field(default_factory=StorageConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(
        default_factory=ToolsConfig,
        description="工具模板与初始暴露配置",
    )
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ui: UIConfig = Field(
        default_factory=UIConfig,
        description="CLI 可视化配置",
    )
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
    sjtu_jw: SjtuJwConfig = Field(
        default_factory=SjtuJwConfig,
        description="上海交通大学教学信息服务网课表同步配置",
    )
    shuiyuan: ShuiyuanConfig = Field(
        default_factory=ShuiyuanConfig,
        description="水源社区（Discourse）配置，用于 Agent 访问水源社区",
    )
    # 注意：当前 automation.jobs 只作为高层声明入口，
    # 实际调度仍以 data/automation/job_definitions.json 为准。
    automation: AutomationConfig = Field(
        default_factory=AutomationConfig,
        description="自动化定时任务配置（声明式配置 job_definitions）。",
    )
    feishu: FeishuConfig = Field(
        default_factory=FeishuConfig,
        description="飞书集成配置，用于在飞书聊天中接入 Schedule Agent。",
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
    # 优先从 .env 加载环境变量（TAVILY_API_KEY 等），即使用户未在 shell 里 source .env / init.sh 也能生效
    try:
        from dotenv import load_dotenv

        for base in [Path.cwd(), Path(__file__).resolve().parents[2]]:
            env_file = base / ".env"
            if env_file.is_file():
                load_dotenv(env_file)
                break
    except ImportError:
        pass

    if config_path is None:
        config_path = find_config_file()

    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    if raw_config is None:
        raise ValueError(f"配置文件为空: {config_path}")

    # 兼容旧工具配置：agent.tool_mode/source_overrides/pinned_tools -> tools
    agent_raw = raw_config.get("agent")
    if not isinstance(agent_raw, dict):
        agent_raw = {}
        raw_config["agent"] = agent_raw
    tools_raw = raw_config.get("tools")
    if not isinstance(tools_raw, dict):
        tools_raw = {}
        raw_config["tools"] = tools_raw
    templates_raw = tools_raw.get("templates")
    if not isinstance(templates_raw, dict):
        templates_raw = {}
        tools_raw["templates"] = templates_raw

    legacy_pinned = agent_raw.pop("pinned_tools", None)
    if legacy_pinned is not None and "pinned_tools" not in tools_raw:
        tools_raw["pinned_tools"] = legacy_pinned

    legacy_tool_mode = str(agent_raw.pop("tool_mode", "") or "").strip().lower()
    if legacy_tool_mode and "default" not in templates_raw:
        templates_raw["default"] = {
            "exposure": "pinned" if legacy_tool_mode in ("kernel", "full") else "empty",
            "extra": [],
        }

    legacy_overrides = agent_raw.pop("source_overrides", None)
    if isinstance(legacy_overrides, dict):
        for source, mode in legacy_overrides.items():
            src = str(source or "").strip()
            raw_mode = str(mode or "").strip().lower()
            if not src:
                continue
            tpl = templates_raw.setdefault(src, {})
            if "exposure" not in tpl:
                tpl["exposure"] = (
                    "pinned" if raw_mode in ("kernel", "full") else "empty"
                )

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

    # Canvas 配置支持环境变量覆盖
    if "canvas" not in raw_config:
        raw_config["canvas"] = {}
    env_canvas_api_key = os.environ.get("CANVAS_API_KEY")
    if env_canvas_api_key:
        raw_config["canvas"]["api_key"] = env_canvas_api_key
    env_canvas_base_url = os.environ.get("CANVAS_BASE_URL")
    if env_canvas_base_url:
        raw_config["canvas"]["base_url"] = env_canvas_base_url

    # 飞书配置支持环境变量覆盖
    if "feishu" not in raw_config:
        raw_config["feishu"] = {}
    env_feishu_app_id = os.environ.get("FEISHU_APP_ID")
    if env_feishu_app_id:
        raw_config["feishu"]["app_id"] = env_feishu_app_id
    env_feishu_app_secret = os.environ.get("FEISHU_APP_SECRET")
    if env_feishu_app_secret:
        raw_config["feishu"]["app_secret"] = env_feishu_app_secret
    env_feishu_verification_token = os.environ.get("FEISHU_VERIFICATION_TOKEN")
    if env_feishu_verification_token:
        raw_config["feishu"]["verification_token"] = env_feishu_verification_token
    env_feishu_encrypt_key = os.environ.get("FEISHU_ENCRYPT_KEY")
    if env_feishu_encrypt_key:
        raw_config["feishu"]["encrypt_key"] = env_feishu_encrypt_key
    env_feishu_automation_chat_id = os.environ.get("FEISHU_AUTOMATION_CHAT_ID")
    if env_feishu_automation_chat_id:
        raw_config["feishu"]["automation_activity_chat_id"] = (
            env_feishu_automation_chat_id
        )

    # 水源社区配置支持环境变量覆盖
    if "shuiyuan" not in raw_config:
        raw_config["shuiyuan"] = {}
    env_shuiyuan_key = os.environ.get("SHUIYUAN_USER_API_KEY")
    if env_shuiyuan_key:
        raw_config["shuiyuan"]["user_api_key"] = env_shuiyuan_key
    # 兼容旧 db_path：迁移为 db_base_dir（取 db_path 的父目录）
    shuiyuan_raw = raw_config["shuiyuan"]
    if (
        isinstance(shuiyuan_raw, dict)
        and "db_path" in shuiyuan_raw
        and "db_base_dir" not in shuiyuan_raw
    ):
        p = Path(str(shuiyuan_raw["db_path"]))
        shuiyuan_raw["db_base_dir"] = str(p.parent)

    # 兼容旧配置：user 已迁移至 prompts/system/user.md
    raw_config.pop("user", None)

    # MCP servers 配置中的环境变量替换（支持 ${ENV_VAR} 语法）
    if "mcp" in raw_config and "servers" in raw_config["mcp"]:
        import re

        def expand_env_vars(obj):
            """递归替换字符串中的 ${ENV_VAR} 为环境变量值"""
            if isinstance(obj, str):
                pattern = r"\$\{([^}]+)\}"

                def replacer(match):
                    var_name = match.group(1)
                    return os.environ.get(var_name, match.group(0))

                return re.sub(pattern, replacer, obj)
            elif isinstance(obj, list):
                return [expand_env_vars(item) for item in obj]
            elif isinstance(obj, dict):
                return {k: expand_env_vars(v) for k, v in obj.items()}
            return obj

        raw_config["mcp"]["servers"] = expand_env_vars(raw_config["mcp"]["servers"])

    cfg = Config(**raw_config)

    # 将 Canvas 段回填到环境变量，方便 frontend.canvas_integration.CanvasConfig.from_env 使用 config.yaml
    try:
        canvas_cfg = cfg.canvas
        if canvas_cfg and canvas_cfg.api_key and not os.environ.get("CANVAS_API_KEY"):
            os.environ["CANVAS_API_KEY"] = canvas_cfg.api_key
        if canvas_cfg and canvas_cfg.base_url and not os.environ.get("CANVAS_BASE_URL"):
            os.environ["CANVAS_BASE_URL"] = canvas_cfg.base_url
    except Exception:
        # 回填失败不影响主流程
        pass

    # 统一进程级时区到配置的 time.timezone（默认 Asia/Shanghai），
    # 确保 logging、datetime.now() 等使用一致的本地时间。
    try:
        import time as _time

        os.environ["TZ"] = cfg.time.timezone
        if hasattr(_time, "tzset"):
            _time.tzset()
    except Exception:
        # 在不支持 tzset 的平台上静默回退，不影响主流程。
        pass

    return cfg


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
