"""
配置加载模块测试
"""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from schedule_agent.config import (
    Config,
    LLMConfig,
    TimeConfig,
    StorageConfig,
    AgentConfig,
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

    def test_time_config_defaults(self):
        """测试时间配置默认值"""
        config = TimeConfig()
        assert config.timezone == "Asia/Shanghai"
        assert config.sleep_start == "23:00"
        assert config.sleep_end == "08:00"

    def test_storage_config_defaults(self):
        """测试存储配置默认值"""
        config = StorageConfig()
        assert config.type == "json"
        assert config.data_dir == "./data"

    def test_agent_config_defaults(self):
        """测试 Agent 配置默认值"""
        config = AgentConfig()
        assert config.max_iterations == 10
        assert config.enable_debug is False

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
        import schedule_agent.config as config_module

        # Mock __file__ 使项目根目录指向一个没有配置文件的位置
        fake_file = str(tmp_path / "fake_location" / "config.py")
        monkeypatch.setattr(config_module, "__file__", fake_file)

        with pytest.raises(FileNotFoundError, match="未找到配置文件"):
            find_config_file()
