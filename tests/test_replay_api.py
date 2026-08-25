"""/api/replay：白名单 + 双开关 + 统一 404 + 独立限流（设计说明 V1.1）。"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from askdb import server


def _client(cfg, monkeypatch, replay_api: bool):
    c = copy.deepcopy(cfg)
    c.raw = copy.deepcopy(cfg.raw)
    c.raw["observability"]["replay_api"] = replay_api
    monkeypatch.setattr(server, "load", lambda _p: c)
    monkeypatch.setattr(server, "_REPLAY_RL", server._RateLimit())
    return TestClient(server.create_app("ignored.yaml"))


def _make_trace(client) -> str:
    """经直查真实落一条审计，拿它的 trace_id 来回放。"""
    r = client.post("/api/sql", json={"sql": "SELECT file_name FROM documents"}).json()
    assert r["ok"] is True
    return r["trace_id"]


def test_disabled_by_default_is_404(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, replay_api=False)
    tid = _make_trace(client)
    r = client.get(f"/api/replay?trace_id={tid}")
    # 开关关闭与不存在不可区分
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_malformed_and_missing_are_same_404(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, replay_api=True)
    bodies = set()
    for tid in ("../etc/passwd", "ABCDEF123456", "abc", "a" * 12, "0123456789ab"):
        r = client.get("/api/replay", params={"trace_id": tid})
        assert r.status_code == 404
        bodies.add(r.text)
    assert len(bodies) == 1          # 非法 id 与合法但不存在的 id 响应逐字节一致


def test_allowlist_fields_only(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, replay_api=True)
    tid = _make_trace(client)
    d = client.get(f"/api/replay?trace_id={tid}").json()
    assert d["trace_id"] == tid and d["kind"] == "sql"
    assert d["sql_final"] and d["steps"]
    assert d["snapshots"] == []                    # 直查没有检查点线程
    # 红线字段绝不出现
    assert "rows" not in d and "schema_prompt" not in d
    # 响应字段必须是白名单的子集，防止未来往审计记录加字段后被顺手带出
    from askdb.audit import REPLAY_FIELDS
    assert set(d) <= set(REPLAY_FIELDS) | {"snapshots"}


def test_rate_limited_separately(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, replay_api=True)
    monkeypatch.setattr(server, "_REPLAY_RL", server._RateLimit(limit=3))
    codes = [client.get("/api/replay?trace_id=0123456789ab").status_code
             for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3] == 429
