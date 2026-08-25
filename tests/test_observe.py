"""观测上报是旁路：没配置零动作，配置了也绝不抛错到主链路。"""

from __future__ import annotations

from askdb import observe


def _rec() -> dict:
    return {
        "trace_id": "abc123abc123", "ts": "2026-08-25T10:00:00+08:00",
        "kind": "ask", "org_id": 65, "question": "文档数是多少",
        "sql_final": "SELECT 1", "rejected_by": None, "attempts": 1,
        "cost_cny": 0.001, "tables_hit": ["documents"], "elapsed_ms": 1200,
        "steps": [
            {"step": "schema_recall", "ms": 80, "status": "ok", "note": "命中 1 表"},
            {"step": "generate_sql", "ms": 900, "status": "ok",
             "tok_in": 800, "tok_out": 60},
        ],
    }


def test_noop_without_env(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(observe, "_client", None)
    observe.report(_rec())              # 不抛错、不上报


def test_report_builds_trace_with_spans_and_generations(monkeypatch):
    calls = {"spans": [], "gens": []}

    class FakeTrace:
        def span(self, **kw): calls["spans"].append(kw)
        def generation(self, **kw): calls["gens"].append(kw)

    class FakeClient:
        def trace(self, **kw): calls["trace"] = kw; return FakeTrace()

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(observe, "_client", FakeClient())
    observe.report(_rec())
    assert calls["trace"]["id"] == "abc123abc123"
    assert calls["trace"]["metadata"]["org_id"] == 65
    # 带 token 的步骤上报为 generation，其余为 span
    assert [g["name"] for g in calls["gens"]] == ["generate_sql"]
    assert calls["gens"][0]["usage"] == {"input": 800, "output": 60, "unit": "TOKENS"}
    assert [s["name"] for s in calls["spans"]] == ["schema_recall"]
    # 红线：上报里不出现结果行与注入提示词
    import json
    dump = json.dumps({k: str(v) for k, v in calls["trace"].items()})
    assert "rows" not in calls["trace"] and "schema_prompt" not in dump


def test_malformed_record_never_raises(monkeypatch):
    class Boom:
        def trace(self, **kw): raise RuntimeError("网络断了")

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(observe, "_client", Boom())
    observe.report({"trace_id": "x", "ts": "不是时间"})
    observe.report(_rec())              # 客户端崩溃也不外溢
