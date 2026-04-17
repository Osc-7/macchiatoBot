"""Kernel 与 call_tool 路径归一：工作区相对路径 → 绝对路径。"""

from agent_core.agent.tool_path_resolution import (
    apply_workspace_path_resolution_to_tool_args,
)
from agent_core.config import (
    CommandToolsConfig,
    Config,
    FileToolsConfig,
    LLMConfig,
    MemoryConfig,
)


def _ws_config(tmp_path):
    ws_parent = tmp_path / "workspace_parent"
    return Config(
        llm=LLMConfig(api_key="t", model="t"),
        memory=MemoryConfig(memory_base_dir=str(tmp_path / "mem")),
        file_tools=FileToolsConfig(
            enabled=True,
            allow_read=True,
            base_dir=str(tmp_path),
        ),
        command_tools=CommandToolsConfig(
            base_dir=str(tmp_path),
            workspace_base_dir=str(ws_parent),
            workspace_isolation_enabled=True,
        ),
    )


def test_apply_resolves_memory_ingest_file_path(tmp_path):
    cfg = _ws_config(tmp_path)
    uid = "u1"
    f = tmp_path / "workspace_parent" / "feishu" / uid / "doc.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF-1.4")

    args = apply_workspace_path_resolution_to_tool_args(
        "memory_ingest",
        {
            "file_path": "doc.pdf",
            "category": "docs",
            "__execution_context__": {"source": "feishu", "user_id": uid},
        },
        cfg,
    )
    assert args["file_path"] == str(f.resolve())


def test_apply_call_tool_nested_arguments(tmp_path):
    cfg = _ws_config(tmp_path)
    uid = "u1"
    f = tmp_path / "workspace_parent" / "feishu" / uid / "note.md"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")

    args = apply_workspace_path_resolution_to_tool_args(
        "call_tool",
        {
            "name": "read_file",
            "arguments": {
                "path": "note.md",
            },
            "__execution_context__": {"source": "feishu", "user_id": uid},
        },
        cfg,
    )
    assert args["arguments"]["path"] == str(f.resolve())


def test_attach_media_paths_list(tmp_path):
    cfg = _ws_config(tmp_path)
    uid = "u1"
    f = tmp_path / "workspace_parent" / "feishu" / uid / "a.png"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\x89PNG\r\n\x1a\n")

    args = apply_workspace_path_resolution_to_tool_args(
        "attach_media",
        {
            "paths": ["a.png", "https://example.com/x.png"],
            "__execution_context__": {"source": "feishu", "user_id": uid},
        },
        cfg,
    )
    assert args["paths"][0] == str(f.resolve())
    assert args["paths"][1] == "https://example.com/x.png"
