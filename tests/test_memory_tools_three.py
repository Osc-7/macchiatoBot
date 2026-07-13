"""记忆三工具面测试：store / update / search"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_core.config import Config, LLMConfig, MemoryConfig
from agent_core.memory.memory_corpus import MemoryCorpus
from agent_core.memory.memory_docs import normalize_memory_doc_name, resolve_memory_doc_path
from system.tools.memory_tools import MemorySearchTool, MemoryStoreTool, MemoryUpdateTool


@pytest.mark.asyncio
async def test_memory_store_then_search():
    with tempfile.TemporaryDirectory() as tmpdir:
        corpus = MemoryCorpus(tmpdir)
        store = MemoryStoreTool(corpus)
        search = MemorySearchTool(corpus, top_n=5)

        result = await store.execute(
            content="用户喜欢深色模式界面",
            title="ui-pref",
            category="notes",
        )
        assert result.success
        sr = await search.execute(query="深色")
        assert sr.success
        assert sr.data["results"]
        assert any("深色" in hit.get("snippet", "") for hit in sr.data["results"])


@pytest.fixture
def minimal_config():
    return Config(llm=LLMConfig(api_key="t", model="t"), memory=MemoryConfig(enabled=True))


@pytest.mark.asyncio
async def test_memory_store_file_path_md():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        src = tmp / "lecture.md"
        src.write_text("# 讲义\n\n向量数据库入门", encoding="utf-8")
        corpus = MemoryCorpus(tmp / "corpus")
        store = MemoryStoreTool(corpus)
        search = MemorySearchTool(corpus)

        stored = await store.execute(file_path=str(src), category="docs", title="lecture")
        assert stored.success
        found = await search.execute(query="向量")
        assert found.success
        assert found.data["results"]


@pytest.mark.asyncio
async def test_memory_update_memory_doc(minimal_config):
    with tempfile.TemporaryDirectory() as tmpdir:
        owner = Path(tmpdir) / "cli" / "root" / "long_term"
        owner.mkdir(parents=True)
        mem_md = owner / "MEMORY.md"
        mem_md.write_text("# MEMORY\n\n## 用户长期偏好\n", encoding="utf-8")

        cfg = minimal_config.model_copy(
            update={
                "memory": MemoryConfig(
                    enabled=True,
                    memory_base_dir=str(Path(tmpdir)),
                )
            }
        )
        tool = MemoryUpdateTool(cfg)
        result = await tool.execute(
            doc="memory",
            mode="append",
            content="\n- 偏好深色模式",
            __execution_context__={"source": "cli", "user_id": "root"},
        )
        assert result.success
        assert result.data["backend"] == "daemon"
        text = mem_md.read_text(encoding="utf-8")
        assert "深色模式" in text


@pytest.mark.asyncio
async def test_memory_update_rejects_invalid_doc(minimal_config):
    tool = MemoryUpdateTool(minimal_config)
    result = await tool.execute(doc="evil", mode="append", content="x")
    assert not result.success
    assert result.error == "INVALID_DOC"


def test_normalize_memory_doc_alias():
    name, err = normalize_memory_doc_name("memory/soul")
    assert err is None
    assert name == "soul"


def test_resolve_memory_doc_path_soul(minimal_config):
    path, err = resolve_memory_doc_path("soul", config=minimal_config)
    assert err is None
    assert path is not None
    assert path.name == "soul.md"


@pytest.mark.asyncio
async def test_memory_search_finds_legacy_content_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        owner = Path(tmpdir) / "cli" / "root"
        legacy_content = owner / "content" / "notes"
        legacy_content.mkdir(parents=True)
        (legacy_content / "api-notes.md").write_text(
            "JWT 认证与 API 网关配置说明", encoding="utf-8"
        )
        corpus = MemoryCorpus(owner / "corpus")
        search = MemorySearchTool(corpus, top_n=5)

        result = await search.execute(query="JWT 认证")
        assert result.success
        assert result.data["results"]
        assert any(
            hit.get("source") == "legacy" and "JWT" in hit.get("snippet", "")
            for hit in result.data["results"]
        )
