"""Protocol models and capabilities for remote MCP (v3+) and blob write (v4)."""

from macchiato_remote.protocol import (
    REMOTE_PROTOCOL_VERSION,
    REMOTE_WORKER_CAPABILITIES,
    RemoteMcpCallToolRequest,
    RemoteMcpCallToolResult,
    RemoteMcpEnsureRequest,
    RemoteMcpEnsureResult,
    RemoteMcpListToolsRequest,
    RemoteMcpListToolsResult,
    RemoteMcpServerRef,
    RemoteMcpShutdownRequest,
    RemoteMcpShutdownResult,
)


def test_protocol_version_at_least_4():
    assert REMOTE_PROTOCOL_VERSION >= 4


def test_capabilities_include_mcp():
    caps = set(REMOTE_WORKER_CAPABILITIES)
    assert {
        "mcp_ensure",
        "mcp_list_tools",
        "mcp_call_tool",
        "mcp_shutdown",
    }.issubset(caps)
    assert "file_blob_write" in caps


def test_mcp_models_roundtrip():
    ensure = RemoteMcpEnsureRequest(
        request_id="r1",
        session_id="s1",
        servers=[RemoteMcpServerRef(name="chrome")],
    )
    assert RemoteMcpEnsureRequest.model_validate(ensure.model_dump()).servers[0].name == (
        "chrome"
    )

    listed = RemoteMcpListToolsResult(
        request_id="r2",
        server_name="chrome",
        tools=[{"name": "nav", "description": "d", "input_schema": {"type": "object"}}],
    )
    assert listed.tools[0].name == "nav"

    call = RemoteMcpCallToolRequest(
        request_id="r3",
        session_id="s1",
        server_name="chrome",
        tool_name="nav",
        arguments={"url": "https://example.com"},
    )
    back = RemoteMcpCallToolRequest.model_validate(call.model_dump())
    assert back.arguments["url"].startswith("https://")

    shut = RemoteMcpShutdownResult(request_id="r4", closed=["chrome"])
    assert RemoteMcpShutdownResult.model_validate(shut.model_dump()).closed == ["chrome"]

    err = RemoteMcpCallToolResult(
        request_id="r5", is_error=True, error="MCP_TOOL_TIMEOUT"
    )
    assert err.is_error and err.error == "MCP_TOOL_TIMEOUT"

    ok = RemoteMcpEnsureResult(
        request_id="r6",
        servers=[{"name": "chrome", "ok": True}],
    )
    assert ok.servers[0].ok
