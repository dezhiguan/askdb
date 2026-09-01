"""审计读取层：分页、检索、统计的口径必须与落盘记录逐字段对得上。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from askdb import audit


def _rec(trace_id: str, ts: str, **kw) -> dict:
    base = {
        "trace_id": trace_id, "ts": ts, "org_id": 65,
        "question": "各知识库分别有多少文档",
        "rejected_by": None, "attempts": 1, "rows_returned": 4,
        "elapsed_ms": 1200, "tok_in": 800, "tok_out": 60,
        "cost_cny": 0.001, "step_count": 1, "multi_step": False,
        "sql_raw": "SELECT 1", "sql_final": "SELECT 1 LIMIT 1000",
        "steps": [{"step": "generate", "ms": 900, "status": "ok"}],
    }
    base.update(kw)
    return base


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _now(offset_days: float = 0) -> str:
    return (datetime.now().astimezone() - timedelta(days=offset_days)).isoformat(
        timespec="seconds"
    )


def test_missing_file_is_empty(tmp_path: Path):
    out = audit.list_audits(tmp_path / "nope.jsonl")
    assert out == {"total": 0, "page": 1, "page_size": 10, "items": []}
    assert audit.stats(tmp_path / "nope.jsonl")["calls"] == 0


def test_torn_line_skipped_not_fatal(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    good = _rec("aaaaaaaaaaa1", _now())
    p.write_text(
        json.dumps(good, ensure_ascii=False) + "\n" + '{"trace_id": "半行被撕',
        encoding="utf-8",
    )
    out = audit.list_audits(p)
    assert out["total"] == 1
    assert out["items"][0]["trace_id"] == "aaaaaaaaaaa1"


def test_pagination_newest_first(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [_rec(f"{i:012x}", _now(offset_days=5 - i)) for i in range(5)])
    page1 = audit.list_audits(p, page=1, page_size=2)
    page3 = audit.list_audits(p, page=3, page_size=2)
    assert page1["total"] == 5
    # 文件尾部（最新写入）排在最前
    assert page1["items"][0]["trace_id"] == "000000000004"
    assert len(page3["items"]) == 1


def test_summary_has_no_sql_text(tmp_path: Path):
    """列表接口的红线：SQL 文本与结果行绝不出现在摘要里。"""
    p = tmp_path / "audit.jsonl"
    _write(p, [_rec("aaaaaaaaaaa1", _now())])
    item = audit.list_audits(p)["items"][0]
    assert "sql_raw" not in item and "sql_final" not in item
    assert "steps" not in item and "rows" not in item
    assert item["ok"] is True and item["kind"] == "ask"


def test_search_matches_trace_id_and_question(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now(), question="卡住的文档有哪些"),
        _rec("bbbbbbbbbbb2", _now(), question="失败率是多少"),
    ])
    assert audit.list_audits(p, q="卡住")["total"] == 1
    assert audit.list_audits(p, q="BBBBBBBBBBB2")["total"] == 1
    assert audit.list_audits(p, q="不存在")["total"] == 0


def test_kind_filter_and_default(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now()),                       # 老记录无 kind
        _rec("bbbbbbbbbbb2", _now(), kind="sql"),
    ])
    assert audit.list_audits(p, kind="ask")["total"] == 1
    assert audit.list_audits(p, kind="sql")["total"] == 1


def test_get_audit_returns_full_record_last_wins(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now(), rows_returned=1),
        _rec("aaaaaaaaaaa1", _now(), rows_returned=9),
    ])
    rec = audit.get_audit(p, "aaaaaaaaaaa1")
    assert rec is not None and rec["rows_returned"] == 9
    assert rec["sql_final"]                       # 完整记录才带 SQL
    assert audit.get_audit(p, "ffffffffffff") is None


def test_stats_window_and_block_rate(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now(1), cost_cny=0.002),
        _rec("bbbbbbbbbbb2", _now(2), rejected_by="R-02", cost_cny=0.0,
             kind="sql", steps=[]),
        _rec("ccccccccccc3", _now(40), cost_cny=9.9),      # 窗口外
        _rec("ddddddddddd4", "不是时间戳"),                  # 坏 ts 不计入
    ])
    st = audit.stats(p, days=30)
    assert st["calls"] == 2 and st["blocked"] == 1
    assert st["block_rate"] == 0.5
    assert st["cost_cny"] == 0.002
    assert st["by_kind"] == {"ask": 1, "sql": 1}
    assert st["by_rule"] == {"R-02": 1}
    # 直查不计模型维度；老 ask 记录无 model 字段 → 如实归"未记录"
    assert st["by_model"] == {"（未记录）": {"calls": 1, "cost_cny": 0.002}}
    # 带步骤 trace 的只有 1/2 —— 这格必须按实情算，不写死 100%
    assert st["trace_complete"] == 0.5
    assert len(st["daily"]) == 2


def test_sql_endpoint_writes_audit_even_when_blocked(cfg, monkeypatch):
    """直查模式一调用一条审计，拦截也留痕（kind=sql）。"""
    from fastapi.testclient import TestClient

    from askdb import server

    monkeypatch.setattr(server, "load", lambda _p: cfg)
    client = TestClient(server.create_app("ignored.yaml"))

    r1 = client.post("/api/sql", json={"sql": "DELETE FROM documents"}).json()
    assert r1["ok"] is False and r1["trace_id"]
    r2 = client.post("/api/sql", json={"sql": "SELECT file_name FROM documents"}).json()
    assert r2["ok"] is True and r2["trace_id"]

    recs = audit.read_records(cfg.audit_log)
    assert len(recs) == 2
    blocked, passed = recs[0], recs[1]
    assert blocked["kind"] == "sql" and blocked["rejected_by"]
    assert passed["rejected_by"] is None and passed["rows_returned"] > 0
    assert blocked["trace_id"] == r1["trace_id"]
    # 流水页据此区分两类调用
    assert audit.list_audits(cfg.audit_log, kind="sql")["total"] == 2


def test_audit_endpoints_paginate_and_stats(cfg, monkeypatch):
    """/api/audit 分页检索 + /api/audit/stats 统计与 replay_api 开关透出。"""
    from fastapi.testclient import TestClient

    from askdb import server

    monkeypatch.setattr(server, "load", lambda _p: cfg)
    client = TestClient(server.create_app("ignored.yaml"))
    for _ in range(3):
        client.post("/api/sql", json={"sql": "SELECT file_name FROM documents"})
    client.post("/api/sql", json={"sql": "DROP TABLE documents"})

    d = client.get("/api/audit", params={"page": 1, "page_size": 2}).json()
    assert d["total"] == 4 and len(d["items"]) == 2
    assert "sql_raw" not in d["items"][0]              # 列表不带 SQL 文本
    assert client.get("/api/audit", params={"q": d["items"][0]["trace_id"]}).json()["total"] == 1

    st = client.get("/api/audit/stats").json()
    assert st["calls"] == 4 and st["blocked"] == 1
    assert st["block_rate"] == 0.25 and st["by_kind"] == {"sql": 4}
    assert st["replay_api"] is False                   # 默认关，前端据此置灰复放入口
    assert st["daily"] and st["daily"][-1]["calls"] == 4


def test_langsmith_status_from_env(monkeypatch):
    """观测状态只认环境变量，如实报告 —— 不发请求也不编成功率。"""
    from askdb.trace import langsmith_status

    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
                "LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    assert langsmith_status() == {"enabled": False, "project": None}

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "askdb-prod")
    assert langsmith_status() == {"enabled": True, "project": "askdb-prod"}


def test_stats_and_health_expose_observability(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from askdb import server

    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
                "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    client = TestClient(server.create_app("ignored.yaml"))
    assert client.get("/api/audit/stats").json()["tracing"]["enabled"] is False
    obs = client.get("/api/health").json()["observability"]
    assert obs["tracing"]["backend"] is None and obs["replay_api"] is False


def test_observability_prefers_selfhosted_langfuse(monkeypatch):
    """Langfuse 与 LangSmith 都配了按 Langfuse 算 —— 国内机房出网到
    LangSmith 云未必通，自托管是默认推荐。"""
    from askdb.trace import observability_status

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setenv("LANGFUSE_HOST", "http://172.25.90.183:3000")
    st = observability_status()
    assert st["backend"] == "langfuse" and st["enabled"] is True
    assert st["host"].endswith(":3000")
    assert st["url"] == st["host"]                    # 没配跳转地址时退回上报地址

    monkeypatch.setenv("LANGFUSE_PUBLIC_URL", "http://localhost:3000")
    assert observability_status()["url"] == "http://localhost:3000"

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY")
    assert observability_status()["backend"] == "langsmith"


def test_latency_percentiles_are_real_observations(tmp_path: Path):
    """执行追踪页把 P95 当核心指标显示，它必须是真发生过的一次耗时。

    用最近秩法而不是插值：插值会造出一个从没出现过的数字，
    而这页要回答的是「实际最慢的那次有多慢」。
    """
    p = tmp_path / "a.jsonl"
    _write(p, [_rec(f"{i:012x}", _now(), elapsed_ms=ms)
               for i, ms in enumerate([10, 20, 30, 40, 5000])])

    s = audit.stats(p, days=30)
    assert s["elapsed_p50_ms"] == 30
    assert s["elapsed_p95_ms"] == 5000
    # 任何分位都必须落在真实观测值集合里
    assert s["elapsed_p50_ms"] in {10, 20, 30, 40, 5000}


def test_latency_percentiles_none_when_no_calls(tmp_path: Path):
    """窗口内没有调用时给 null，而不是 0 —— 0 会被读成「快得不得了」。"""
    p = tmp_path / "a.jsonl"
    _write(p, [_rec("aaaaaaaaaaa1", _now(offset_days=90))])

    s = audit.stats(p, days=30)
    assert s["calls"] == 0
    assert s["elapsed_p50_ms"] is None and s["elapsed_p95_ms"] is None
