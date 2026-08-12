"""J 域 · MCP 工具（5 条）。"""
from __future__ import annotations
import inspect
import pytest
from askdb import mcp_server


def test_j01_ask_payload_shape():
    src = inspect.getsource(mcp_server)
    for k in ("sql", "trace_id", "caveat"):
        assert k in src, f"MCP 返回必须含 {k}"


def test_j02_run_sql_uses_same_guard():
    src = inspect.getsource(mcp_server)
    assert "guard.check" in src, "MCP 直查必须走同一套护栏"


def test_j03_schema_tool_present():
    src = inspect.getsource(mcp_server)
    assert "schema" in src


def test_j04_as_of_returned():
    src = inspect.getsource(mcp_server)
    assert "as_of" in src, "MCP 返回必须带数据时间"


def test_j05_stateless():
    src = inspect.getsource(mcp_server)
    assert "session" not in src.lower() or "stateless" in src.lower()
