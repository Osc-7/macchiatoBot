"""
配置加载模块测试
"""

from pathlib import Path

import pytest

from agent_core.config import (
    Config,
    FeishuConfig,
    LLMConfig,
    TimeConfig,
    StorageConfig,
    AgentConfig,
    MultimodalConfig,
    CanvasIntegrationConfig,
    PlanningConfig,
    PlanningWeightsConfig,
    FileToolsConfig,
    CommandToolsConfig,
    MCPConfig,
    MCPServerConfig,
    ToolsConfig,
    UIConfig,
    load_config,
    get_config,
    reset_config,
    find_config_file,
)
from agent_core.kernel_interface.profile import CoreProfile


@pytest.fixture(autouse=True)
def reset_global_config():
    """每个测试前重置全局配置"""
    reset_config()
    yield
    reset_config()


class TestConfigModels:
    """测试配置模型"""

    def test_llm_config_defaults(self):
        """测试 LLM 配置默认值"""
        config = LLMConfig(api_key="test-key", model="test-model")
        assert config.provider == "openai_compatible"
        assert config.api_key == "test-key"
        assert config.model == "test-model"
        assert config.base_url == "https://api.openai.com/v1"
        assert config.temperature == 0.7
        assert config.max_tokens == 4096
        assert config.request_timeout_seconds == 120.0
        assert config.stream is False
        assert config.vendor_params == {}
        assert config.parallel_tool_calls is True
        assert config.context_window is None

    def test_time_config_defaults(self):
        """测试时间配置默认值"""
        config = TimeConfig()
        assert config.timezone == "Asia/Shanghai"
        assert config.sleep_start == "23:00"
        assert config.sleep_end == "08:00"

    def test_feishu_automation_ipc_timeout_default(self):
        """飞书 automation IPC 读超时与 LLM HTTP 超时解耦，默认应足够长。"""
        fei = FeishuConfig()
        assert fei.automation_ipc_timeout_seconds == 1800.0

    def test_multimodal_config_defaults(self):
        """测试多模态配置默认值"""
        mm = MultimodalConfig()
        assert mm.enabled is False
        assert mm.model is None
        assert mm.max_image_size_mb == 8.0

    def test_storage_config_defaults(self):
        """测试存储配置默认值"""
        config = StorageConfig()
        assert config.type == "json"
        assert config.data_dir == "./data"

    def test_planning_config_defaults(self):
        """测试规划配置默认值"""
        planning = PlanningConfig()
        assert planning.timezone == "Asia/Shanghai"
        assert planning.lookahead_days == 7
        assert planning.min_block_minutes == 30
        assert planning.working_hours == []
        assert isinstance(planning.weights, PlanningWeightsConfig)

    def test_canvas_config_defaults(self):
        """测试 Canvas 配置默认值"""
        canvas = CanvasIntegrationConfig()
        assert canvas.enabled is False
        assert canvas.api_key is None
        assert canvas.base_url == "https://oc.sjtu.edu.cn/api/v1"
        assert canvas.default_days_ahead == 60
        assert canvas.include_submitted is False

    def test_agent_config_defaults(self):
        """测试 Agent 配置默认值"""
        config = AgentConfig()
        assert config.max_iterations == 10
        assert config.enable_debug is False
        assert config.working_set_size == 6

    def test_tools_config_defaults(self):
        """测试工具模板配置默认值"""
        config = ToolsConfig()
        assert config.core_tools == [
            "search_tools",
            "call_tool",
            "bash",
            "request_permission",
            "ask_user",
        ]
        assert "read_file" in config.pinned_tools
        assert "list_scheduled_jobs" in config.pinned_tools
        assert "delete_scheduled_job" in config.pinned_tools
        assert config.templates["default"].exposure == "pinned"
        assert "shuiyuan_browse_topic" in config.templates["shuiyuan"].extra

    def test_shuiyuan_profile_is_full_mode(self):
        """水源前端默认走 full profile，而不是受限 sub。"""
        profile = CoreProfile.for_shuiyuan(dialog_window_id="alice")
        assert profile.mode == "full"
        assert profile.allowed_tools is None
        assert profile.frontend_id == "shuiyuan"
        assert profile.dialog_window_id == "alice"

    def test_full_config(self):
        """测试完整配置"""
        config = Config(
            llm=LLMConfig(api_key="test-key", model="test-model"),
            time=TimeConfig(),
            storage=StorageConfig(),
            agent=AgentConfig(),
        )
        assert config.llm.api_key == "test-key"
        assert config.time.timezone == "Asia/Shanghai"
        assert config.storage.type == "json"
        assert config.agent.max_iterations == 10
        assert config.multimodal.enabled is False
        assert config.canvas.enabled is False

    def test_file_tools_config_defaults(self):
        """测试文件工具配置默认值"""
        ft = FileToolsConfig()
        assert ft.enabled is True
        assert ft.allow_read is True
        assert ft.allow_write is False
        assert ft.allow_modify is False
        assert ft.base_dir == "."

    def test_config_has_file_tools_default(self):
        """测试 Config 未指定 file_tools 时使用默认值"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
        )
        assert config.file_tools is not None
        assert config.file_tools.enabled is True
        assert config.file_tools.allow_write is False

    def test_command_tools_config_defaults(self):
        """测试命令工具（bash 会话）配置默认值"""
        ct = CommandToolsConfig()
        assert ct.enabled is True
        assert ct.allow_run is True
        assert ct.base_dir == "."
        assert ct.default_timeout_seconds == 30.0
        assert ct.max_timeout_seconds == 300.0
        assert ct.default_output_limit == 12000
        assert ct.max_output_limit == 200000
        assert ct.workspace_base_dir == "./data/workspace"
        assert ct.workspace_isolation_enabled is True
        assert ct.workspace_admin_memory_owners == []
        assert ct.bash_extra_write_roots == []
        assert ct.bash_real_home_path_suffixes == []
        assert ct.acl_base_dir == "./data/acl"

    def test_config_has_command_tools_default(self):
        """测试 Config 未指定 command_tools 时使用默认值"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
        )
        assert config.command_tools is not None
        assert config.command_tools.enabled is True
        assert config.command_tools.allow_run is True

    def test_mcp_config_defaults(self):
        """测试 MCP 配置默认值"""
        mcp = MCPConfig()
        assert mcp.enabled is False
        assert mcp.inject_builtin_schedule_mcp is False
        assert mcp.call_timeout_seconds == 30
        assert mcp.servers == []

    def test_mcp_server_config(self):
        """测试 MCP Server 配置"""
        server = MCPServerConfig(
            name="demo",
            command="python",
            args=["-m", "demo_server"],
        )
        assert server.name == "demo"
        assert server.transport == "stdio"
        assert server.enabled is True
        assert server.args == ["-m", "demo_server"]

    def test_config_has_mcp_default(self):
        """测试 Config 未指定 mcp 时使用默认值"""
        config = Config(llm=LLMConfig(api_key="x", model="x"))
        assert config.mcp is not None
        assert config.mcp.enabled is False

    def test_ui_config_defaults(self):
        """测试 UI 配置默认值"""
        ui = UIConfig()
        assert ui.show_draft == "summary"
        assert ui.draft_max_chars == 500
        assert ui.dim_draft is True

    def test_config_has_ui_default(self):
        """测试 Config 未指定 ui 时使用默认值"""
        config = Config(llm=LLMConfig(api_key="x", model="x"))
        assert config.ui is not None
        assert config.ui.show_draft == "summary"

    def test_config_has_planning_default(self):
        """测试 Config 未指定 planning 时使用默认值"""
        config = Config(llm=LLMConfig(api_key="x", model="x"))
        assert config.planning is not None
        assert config.planning.working_hours == []


class TestLoadConfig:
    """测试配置加载"""

    def test_load_valid_config(self, tmp_path):
        """测试加载有效配置文件"""
        config_content = """
llm:
  provider: "doubao"
  api_key: "test-api-key"
  base_url: "https://ark.cn-beijing.volces.com/api/v3"
  model: "ep-20250117123456"
  temperature: 0.5
  max_tokens: 2048

time:
  timezone: "Asia/Shanghai"
  sleep_start: "23:00"
  sleep_end: "08:00"

storage:
  type: "json"
  data_dir: "./data"

agent:
  max_iterations: 5
  enable_debug: true

planning:
  timezone: "Asia/Shanghai"
  lookahead_days: 7
  min_block_minutes: 30
  working_hours:
    - weekday: 1
      start: "09:00"
      end: "18:00"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.llm.api_key == "test-api-key"
        assert config.llm.model == "ep-20250117123456"
        assert config.llm.temperature == 0.5
        assert config.time.timezone == "Asia/Shanghai"
        assert config.agent.max_iterations == 5
        assert config.agent.enable_debug is True
        assert config.planning.working_hours[0].weekday == 1
        assert config.planning.working_hours[0].start == "09:00"

    def test_load_minimal_config(self, tmp_path):
        """测试加载最小配置（只包含必需字段）"""
        config_content = """
llm:
  api_key: "minimal-key"
  model: "minimal-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.llm.api_key == "minimal-key"
        assert config.llm.model == "minimal-model"
        # 验证默认值
        assert config.llm.provider == "openai_compatible"
        assert config.llm.base_url == "https://api.openai.com/v1"
        assert config.llm.temperature == 0.7
        assert config.time.timezone == "Asia/Shanghai"
        assert config.storage.type == "json"

    def test_load_legacy_tool_mode_migrates_to_tools(self, tmp_path):
        """旧 agent.tool_mode/source_overrides/pinned_tools 应迁移为 tools 配置。"""
        config_content = """
llm:
  api_key: "legacy-key"
  model: "legacy-model"
agent:
  tool_mode: "kernel"
  source_overrides:
    shuiyuan: "sub"
  pinned_tools:
    - "read_file"
    - "write_file"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.tools.pinned_tools == ["read_file", "write_file"]
        assert config.tools.templates["default"].exposure == "pinned"
        assert config.tools.templates["shuiyuan"].exposure == "empty"

    def test_load_nonexistent_file(self):
        """测试加载不存在的文件"""
        with pytest.raises(FileNotFoundError):
            load_config(Path("/nonexistent/config.yaml"))

    def test_load_empty_file(self, tmp_path):
        """测试加载空文件"""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("", encoding="utf-8")

        with pytest.raises(ValueError, match="配置文件为空"):
            load_config(config_file)


class TestLegacyLlmMigration:
    """旧版 llm 段字段迁入 vendor_params"""

    def test_legacy_fields_migrate_to_vendor_params(self, tmp_path):
        config_content = """
llm:
  api_key: "k"
  model: "m"
  enable_thinking: true
  thinking_budget: 128
  enable_search: false
  search_options:
    forced_search: true
    search_strategy: "max"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        vp = config.llm.vendor_params
        assert vp.get("enable_thinking") is True
        assert vp.get("thinking_budget") == 128
        assert vp.get("enable_search") is False
        assert isinstance(vp.get("search_options"), dict)
        assert vp["search_options"]["forced_search"] is True


class TestEnvOverride:
    """测试环境变量覆盖"""

    def test_api_key_override(self, tmp_path, monkeypatch):
        """测试环境变量覆盖 API Key"""
        monkeypatch.setenv("DOUBAO_API_KEY", "env-api-key")

        config_content = """
llm:
  provider: "doubao"
  api_key: "file-api-key"
  model: "test-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.llm.api_key == "env-api-key"

    def test_model_override(self, tmp_path, monkeypatch):
        """测试环境变量覆盖模型"""
        monkeypatch.setenv("DOUBAO_MODEL", "env-model")

        config_content = """
llm:
  provider: "doubao"
  api_key: "test-key"
  model: "file-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.llm.model == "env-model"

    def test_openai_compatible_env_override(self, tmp_path, monkeypatch):
        """OPENAI_* 环境变量覆盖通用 OpenAI 兼容端点"""
        monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
        monkeypatch.setenv("OPENAI_SUMMARY_MODEL", "gpt-4o-mini")

        config_content = """
llm:
  api_key: "file-key"
  model: "file-model"
  summary_model: null
  base_url: "https://api.openai.com/v1"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.llm.provider == "openai_compatible"
        assert config.llm.api_key == "oai-key"
        assert config.llm.base_url == "https://example.com/v1"
        assert config.llm.model == "gpt-4o"
        assert config.llm.summary_model == "gpt-4o-mini"

    def test_canvas_api_key_override(self, tmp_path, monkeypatch):
        """测试环境变量覆盖 Canvas API Key"""
        monkeypatch.setenv("CANVAS_API_KEY", "env-canvas-key")

        config_content = """
llm:
  api_key: "test-key"
  model: "test-model"
canvas:
  enabled: true
  api_key: "file-canvas-key"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)
        assert config.canvas.api_key == "env-canvas-key"


class TestProvidersMapMigration:
    """LLMConfig 旧字段 → providers map 的迁移"""

    def test_legacy_top_level_llm_migrates_to_default_provider(self, tmp_path):
        """老 config（仅有顶层 api_key / model）应被迁移到 providers['default']。"""
        config_content = """
llm:
  api_key: "legacy-k"
  base_url: "https://legacy.example/v1"
  model: "legacy-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert "default" in config.llm.providers
        default = config.llm.providers["default"]
        assert default.api_key == "legacy-k"
        assert default.base_url == "https://legacy.example/v1"
        assert default.model == "legacy-model"
        # active 未显式指定时应指向 default
        assert config.llm.active == "default"

    def test_new_providers_map_and_active(self, tmp_path):
        """新风格 providers map 应原样解析，active 指向期望的 provider。"""
        config_content = """
llm:
  api_key: "k"
  model: "m"
  active: "deepseek"
  vision_provider: "qwen3vl"
  providers:
    deepseek:
      base_url: "https://example.com/v1"
      api_key: "a"
      model: "deepseek-chat"
      capabilities:
        vision: false
    qwen3vl:
      base_url: "https://example.com/v1"
      api_key: "b"
      model: "qwen3-vl"
      capabilities:
        vision: true
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert set(config.llm.providers.keys()) == {"deepseek", "qwen3vl"}
        assert config.llm.providers["qwen3vl"].capabilities.vision is True
        assert config.llm.providers["deepseek"].capabilities.vision is False
        assert config.llm.active == "deepseek"
        assert config.llm.vision_provider == "qwen3vl"

    def test_provider_env_var_expansion(self, tmp_path, monkeypatch):
        """providers 下 ${ENV_VAR} 语法应由加载器就地展开。"""
        monkeypatch.setenv("SJTU_API_KEY", "env-sjtu-key")
        config_content = """
llm:
  api_key: "k"
  model: "m"
  providers:
    sjtu:
      base_url: "https://models.sjtu.edu.cn/v1"
      api_key: "${SJTU_API_KEY}"
      model: "deepseek-chat"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")
        config = load_config(config_file)
        assert config.llm.providers["sjtu"].api_key == "env-sjtu-key"

    def test_provider_include_merges_fragments(self, tmp_path):
        """llm.provider_include 应将 YAML 片段合并进 providers。"""
        frag = tmp_path / "extra.yaml"
        frag.write_text(
            """
moonshot_a:
  label: "Kimi A"
  base_url: "https://api.moonshot.cn/v1"
  api_key: "k-a"
  model: "kimi-k2.5"
  capabilities:
    vision: true
""",
            encoding="utf-8",
        )
        main = tmp_path / "config.yaml"
        main.write_text(
            f"""
llm:
  api_key: "k"
  model: "m"
  provider_include:
    - extra.yaml
  providers:
    local:
      base_url: "https://example.com/v1"
      api_key: "k-local"
      model: "local-m"
      capabilities:
        vision: false
""",
            encoding="utf-8",
        )
        config = load_config(main)
        assert set(config.llm.providers.keys()) == {"local", "moonshot_a"}
        assert config.llm.providers["moonshot_a"].label == "Kimi A"
        assert config.llm.providers["moonshot_a"].model == "kimi-k2.5"

    def test_provider_include_override_order(self, tmp_path):
        """同名 provider：后加载的 provider_include 覆盖前者。"""
        (tmp_path / "first.yaml").write_text(
            """
dup:
  base_url: "https://a/v1"
  api_key: "a"
  model: "m1"
""",
            encoding="utf-8",
        )
        (tmp_path / "second.yaml").write_text(
            """
dup:
  base_url: "https://b/v1"
  api_key: "b"
  model: "m2"
""",
            encoding="utf-8",
        )
        main = tmp_path / "config.yaml"
        main.write_text(
            """
llm:
  api_key: "k"
  model: "m"
  provider_include:
    - first.yaml
    - second.yaml
  providers: {}
""",
            encoding="utf-8",
        )
        config = load_config(main)
        assert config.llm.providers["dup"].model == "m2"
        assert config.llm.providers["dup"].base_url == "https://b/v1"

    def test_providers_dir_merges_yaml_files(self, tmp_path):
        """llm.providers_dir 合并目录内全部 *.yaml。"""
        d = tmp_path / "prov.d"
        d.mkdir()
        (d / "01_x.yaml").write_text(
            """
from_dir:
  base_url: "https://x/v1"
  api_key: "x"
  model: "mx"
""",
            encoding="utf-8",
        )
        main = tmp_path / "config.yaml"
        main.write_text(
            """
llm:
  api_key: "k"
  model: "m"
  providers_dir: prov.d
  providers: {}
""",
            encoding="utf-8",
        )
        config = load_config(main)
        assert "from_dir" in config.llm.providers
        assert config.llm.providers["from_dir"].model == "mx"

    def test_providers_dir_resolves_from_repo_root_config(self, tmp_path):
        """主配置在仓库根 config.yaml 时，providers_dir: llm/providers.d 可落到 config/llm/providers.d。"""
        repo = tmp_path / "repo"
        nested = repo / "config" / "llm" / "providers.d"
        nested.mkdir(parents=True)
        (nested / "z.yaml").write_text(
            "remote:\n"
            "  base_url: https://example.com/v1\n"
            "  api_key: rk\n"
            "  model: rm\n",
            encoding="utf-8",
        )
        main = repo / "config.yaml"
        main.write_text(
            "llm:\n  api_key: k\n  model: m\n  providers_dir: llm/providers.d\n",
            encoding="utf-8",
        )
        config = load_config(main)
        assert "remote" in config.llm.providers
        assert config.llm.providers["remote"].model == "rm"

    def test_provider_fragment_with_top_level_providers_key(self, tmp_path):
        """片段文件可写 providers: {{ ... }} 包裹。"""
        (tmp_path / "wrap.yaml").write_text(
            """
providers:
  wrapped:
    base_url: "https://w/v1"
    api_key: "w"
    model: "mw"
""",
            encoding="utf-8",
        )
        main = tmp_path / "config.yaml"
        main.write_text(
            """
llm:
  api_key: "k"
  model: "m"
  provider_include:
    - wrap.yaml
  providers: {}
""",
            encoding="utf-8",
        )
        config = load_config(main)
        assert config.llm.providers["wrapped"].model == "mw"


class TestGlobalConfig:
    """测试全局配置"""

    def test_get_config_singleton(self, tmp_path, monkeypatch):
        """测试全局配置单例"""
        # 修改工作目录到临时目录
        monkeypatch.chdir(tmp_path)

        config_content = """
llm:
  api_key: "singleton-key"
  model: "singleton-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config1 = get_config()
        config2 = get_config()

        assert config1 is config2
        assert config1.llm.api_key == "singleton-key"

    def test_reset_config(self, tmp_path, monkeypatch):
        """测试重置全局配置"""
        monkeypatch.chdir(tmp_path)

        config_content = """
llm:
  api_key: "reset-key"
  model: "reset-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config1 = get_config()
        reset_config()
        config2 = get_config()

        # 重置后应该是新实例
        assert config1 is not config2
        # 但值应该相同
        assert config1.llm.api_key == config2.llm.api_key


class TestFindConfigFile:
    """测试配置文件查找"""

    def test_find_in_cwd(self, tmp_path, monkeypatch):
        """测试在当前工作目录下查找 config/config.yaml"""
        nested = tmp_path / "config"
        nested.mkdir()
        config_file = nested / "config.yaml"
        config_file.write_text("llm:\n  api_key: key\n  model: model", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        found = find_config_file()

        assert found == config_file

    def test_find_not_found(self, tmp_path, monkeypatch):
        """测试未找到配置文件"""
        # 创建一个完全隔离的临时目录
        isolated_dir = tmp_path / "isolated"
        isolated_dir.mkdir()

        # 修改工作目录
        monkeypatch.chdir(isolated_dir)

        # 由于 find_config_file 会回退到项目根目录下的 config/config.yaml，
        # 而真实仓库可能带有该文件，所以这个测试验证的是
        # 当配置文件不在当前目录时能正确回退到项目根目录
        # 我们需要 mock 一个场景让两个位置都没有配置文件
        import agent_core.config as config_module

        # Mock __file__ 使项目根目录指向一个没有配置文件的位置
        fake_file = str(tmp_path / "fake_location" / "config.py")
        monkeypatch.setattr(config_module, "__file__", fake_file)

        with pytest.raises(FileNotFoundError, match="未找到主配置文件"):
            find_config_file()
