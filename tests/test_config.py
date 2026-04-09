"""
配置加载模块测试
"""

from pathlib import Path

import pytest

from agent_core.config import (
    Config,
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
    UIConfig,
    load_config,
    get_config,
    reset_config,
    find_config_file,
)


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
        assert config.provider == "doubao"
        assert config.api_key == "test-key"
        assert config.model == "test-model"
        assert config.temperature == 0.7
        assert config.max_tokens == 4096
        assert config.request_timeout_seconds == 120.0
        assert config.stream is False
        assert config.thinking_budget is None

    def test_time_config_defaults(self):
        """测试时间配置默认值"""
        config = TimeConfig()
        assert config.timezone == "Asia/Shanghai"
        assert config.sleep_start == "23:00"
        assert config.sleep_end == "08:00"

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
        # 默认使用 kernel 模式；full 作为向后兼容别名，在 Agent 内部被视为 kernel
        assert config.tool_mode == "kernel"
        assert config.working_set_size == 6
        assert "search_tools" in config.pinned_tools
        assert "call_tool" in config.pinned_tools

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
        assert config.llm.provider == "doubao"
        assert config.llm.temperature == 0.7
        assert config.time.timezone == "Asia/Shanghai"
        assert config.storage.type == "json"

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


class TestEnvOverride:
    """测试环境变量覆盖"""

    def test_api_key_override(self, tmp_path, monkeypatch):
        """测试环境变量覆盖 API Key"""
        monkeypatch.setenv("DOUBAO_API_KEY", "env-api-key")

        config_content = """
llm:
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
  api_key: "test-key"
  model: "file-model"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content, encoding="utf-8")

        config = load_config(config_file)

        assert config.llm.model == "env-model"

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
        """测试在当前目录查找"""
        config_file = tmp_path / "config.yaml"
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

        # 由于 find_config_file 会检查项目根目录（src 的父目录），
        # 而项目根目录有 config.yaml，所以这个测试验证的是
        # 当配置文件不在当前目录时能正确回退到项目根目录
        # 我们需要 mock 一个场景让两个位置都没有配置文件
        import agent_core.config as config_module

        # Mock __file__ 使项目根目录指向一个没有配置文件的位置
        fake_file = str(tmp_path / "fake_location" / "config.py")
        monkeypatch.setattr(config_module, "__file__", fake_file)

        with pytest.raises(FileNotFoundError, match="未找到配置文件"):
            find_config_file()
