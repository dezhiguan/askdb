"""/api/result：最终结果（答案 + 已脱敏结果行）。

单独一道门：**要登录**（不受 auth.required 影响）、按可见表收窄、不返回 SQL 全文。
被拦/旧记录返回 null。见 server.result_api / audit.result_block。
"""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from askdb import audit, auth, server
from askdb.graph import AskResult, _audit_of


def _client(cfg, monkeypatch, *, login: bool):
    c = copy.deepcopy(cfg)
    c.raw = copy.deepcopy(cfg.raw)
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    c.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [{"username": "ops", "roles": ["DATA_OWNER"],
                      "password_hash": auth.hash_password("ops-pw")}],
    }
    monkeypatch.setattr(server, "load", lambda _p: c)
    client = TestClient(server.create_app("ignored.yaml"))
    if login:
        assert client.post("/api/auth/login",
                           json={"username": "ops", "password": "ops-pw"}).status_code == 200
    return client


def _make_trace(client) -> str:
    r = client.post("/api/sql", json={"sql": "SELECT file_name FROM documents"}).json()
    assert r["ok"] is True
    return r["trace_id"]


# ---- 端点 ----
def test_result_requires_login(cfg, monkeypatch):
    """匿名一律 401 —— 结果行含数据，绝不给访客（auth.required=false 时也挡住）。"""
    logged = _client(cfg, monkeypatch, login=True)
    tid = _make_trace(logged)
    anon = _client(cfg, monkeypatch, login=False)
    assert anon.get(f"/api/result?trace_id={tid}").status_code == 401


def test_result_returns_masked_rows_when_logged_in(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, login=True)
    tid = _make_trace(client)
    r = client.get(f"/api/result?trace_id={tid}")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res is not None
    assert "file_name" in res["columns"]
    assert isinstance(res["rows_preview"], list) and len(res["rows_preview"]) > 0
    assert res["rows_returned"] >= 1


def test_result_malformed_id_is_404(cfg, monkeypatch):
    client = _client(cfg, monkeypatch, login=True)
    assert client.get("/api/result", params={"trace_id": "../nope"}).status_code == 404
    assert client.get("/api/result", params={"trace_id": "abcdef123456"}).status_code == 404


def test_result_preview_capped(cfg, monkeypatch):
    """结果行只存前 N 行（脱敏后），不把整表灌进审计。"""
    client = _client(cfg, monkeypatch, login=True)
    r = client.post("/api/sql", json={"sql": "SELECT file_name FROM documents"}).json()
    res = client.get(f"/api/result?trace_id={r['trace_id']}").json()["result"]
    assert len(res["rows_preview"]) <= audit.RESULT_PREVIEW_ROWS


# ---- result_block 纯函数 ----
def test_result_block_branches():
    assert audit.result_block({"rejected_by": "R-11", "rows_preview": [[1]]}) is None
    assert audit.result_block({"rows_returned": 0}) is None
    blk = audit.result_block({"answer": "x", "columns": ["n"], "rows_preview": [[23]],
                              "rows_returned": 1, "masked_columns": []})
    assert blk == {"answer": "x", "columns": ["n"], "rows_preview": [[23]],
                   "rows_returned": 1, "masked_columns": []}


# ---- _audit_of 落最终结果字段 ----
def test_audit_of_carries_result(cfg):
    r = AskResult(ok=True, question="q", trace_id="t", org_id=1,
                  reasoning="共 3 个", columns=["n"], rows=[[1], [2], [3]], row_count=3)
    rec = _audit_of(r, cfg, "ask")
    assert rec["answer"] == "共 3 个"
    assert rec["columns"] == ["n"]
    assert rec["rows_preview"] == [[1], [2], [3]]
