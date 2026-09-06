"""/api/replay：白名单 + 双开关 + 统一 404 + 独立限流（设计说明 V1.1）。"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from askdb import auth, server


def _client(cfg, monkeypatch, replay_api: bool):
    c = copy.deepcopy(cfg)
    c.raw = copy.deepcopy(cfg.raw)
    c.raw["observability"]["replay_api"] = replay_api
    # 回放于 2026-09-05 起要登录：它返回 SQL 全文与问题原文。这一组用例测的是
    # **里面那几条规则**（字段白名单、统一 404、独立限流），所以先把身份补上，
    # 否则测到的全是外面那道"未登录"。匿名拿 404 由 test_auth 单独钉。
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    c.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [{"username": "ops", "roles": ["DATA_OWNER"],
                      "password_hash": auth.hash_password("ops-pw")}],
    }
    monkeypatch.setattr(server, "load", lambda _p: c)
    monkeypatch.setattr(server, "_REPLAY_RL", server._RateLimit())
    client = TestClient(server.create_app("ignored.yaml"))
    assert client.post("/api/auth/login",
                       json={"username": "ops", "password": "ops-pw"}).status_code == 200
    return client


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


def test_anonymous_replay_is_indistinguishable_from_not_found(cfg, monkeypatch):
    """未登录回放与「不存在」同一响应。

    回放返回的是这次调用的 SQL 全文与问题原文 —— 比列表那一行敏感得多。
    用 404 而不是 401，是沿用本接口既有的"三种结局同一响应"约定：
    区分「存在但你没权限」和「不存在」，本身就是信息泄露。
    """
    c = _client(cfg, monkeypatch, replay_api=True)
    tid = _make_trace(c)
    assert c.get(f"/api/replay?trace_id={tid}").status_code == 200

    c.post("/api/auth/logout")
    anon = c.get(f"/api/replay?trace_id={tid}")
    missing = c.get("/api/replay?trace_id=0123456789ab")
    assert anon.status_code == 404
    assert anon.text == missing.text          # 逐字节一致



def test_trace_of_a_runtime_source_record_is_visible(cfg, monkeypatch):
    """跑在运行时数据源上的记录，执行链路必须打得开。

    2026-09-07：这里原来拿**启动配置**的白名单判可见性。多数据源之后，
    任何运行时源上的记录都不可能是它的子集 —— careermate 源的记录
    （users / resume_versions）在 ragforge 实例上一律 404，成功的、被拦下的
    全都点不开右半屏。表现是"任务列得出来、点进去空白"，最容易被读成
    "被拦截的没有执行链路"，而这跟拦不拦截根本无关。
    """
    import dataclasses
    import json

    from askdb.config import Column, Table

    client = _client(cfg, monkeypatch, replay_api=True)
    other = {"users": Table(name="users", desc="", aliases=[],
                            columns={"id": Column("id", "BIGINT")}, tenant_exempt=True)}
    # 一条"别的库上的"审计记录：它命中的表不在本实例白名单里，正是出问题的形状
    rec = {"trace_id": "cc11cc11cc11", "thread_id": "cc11cc11cc11", "kind": "ask",
           "ts": "2026-09-07T03:15:02+08:00", "user": "ops", "role": "DATA_OWNER",
           "source": "src_other", "source_name": "另一个库",
           "tables_hit": ["users"], "rejected_by": "NO_SQL", "attempts": 1,
           "steps": [{"step": "generate_sql", "ms": 12, "status": "ok",
                      "note": "生成 1 条 SELECT"}]}
    with open(cfg.audit_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    import types

    stub = types.SimpleNamespace(id="src_other", name="另一个库",
                                 tables=[{"name": "users"}])
    monkeypatch.setattr(
        server._sources, "get_source",
        lambda _cfg, sid: stub if sid == "src_other" else None)
    monkeypatch.setattr(
        server._sources, "derive_config",
        lambda base, _src: dataclasses.replace(base, tables=other,
                                               source_id="src_other",
                                               source_name="另一个库"))

    r = client.get("/api/trace?trace_id=cc11cc11cc11")
    assert r.status_code == 200, r.json()
    assert r.json()["steps"], "链路节点必须真的出来，不能是空壳 200"
