"""任务中断恢复（设计说明 V1.1）：中断兜底、断点续跑、审计关联、统一 404。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from askdb import graph, server
from askdb.audit import get_audit, read_records
from tests.test_graph import OK_SQL, FakeLlm


def _interrupted_ask(cfg, ex, monkeypatch) -> graph.AskResult:
    """让 guard 节点抛出进程级异常，制造一次真实中断。"""
    with monkeypatch.context() as m:
        m.setattr(graph.guard, "check",
                  lambda *a, **k: (_ for _ in ()).throw(RuntimeError("进程被杀")))
        return graph.ask("有多少文档", cfg, executor=ex, llm=FakeLlm(OK_SQL))


def test_interrupt_leaves_trace_and_resumable_checkpoint(cfg, ex, monkeypatch):
    r1 = _interrupted_ask(cfg, ex, monkeypatch)
    assert r1.ok is False and r1.rejected_by == "INTERRUPTED"
    assert r1.trace_id and r1.thread_id == r1.trace_id     # 客户端由此持有续跑凭据
    rec = get_audit(cfg.audit_log, r1.trace_id)
    assert rec["rejected_by"] == "INTERRUPTED" and rec["kind"] == "ask"
    # 检查点停在中断节点之前，线程未走完
    snap = graph.build_graph(cfg.checkpoint_db).get_state(
        {"configurable": {"thread_id": r1.trace_id}})
    assert snap.next


def test_resume_completes_and_links_audit(cfg, ex, monkeypatch):
    r1 = _interrupted_ask(cfg, ex, monkeypatch)
    r2 = graph.resume(r1.thread_id, cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert r2 is not None and r2.ok and r2.row_count > 0
    # trace 新开、thread 不变 —— 审计里能看出"这是第 2 次执行"
    assert r2.trace_id != r1.trace_id
    assert r2.thread_id == r1.thread_id
    recs = read_records(cfg.audit_log)
    assert [x["kind"] for x in recs] == ["ask", "resume"]
    assert recs[1]["thread_id"] == r1.thread_id


def test_resume_missing_or_finished_returns_none(cfg, ex):
    assert graph.resume("0123456789ab", cfg, executor=ex) is None      # 不存在
    done = graph.ask("有多少文档", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert done.ok
    assert graph.resume(done.thread_id, cfg, executor=ex) is None      # 已跑完


def test_resume_endpoint_uniform_404_and_success(cfg, ex, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    client = TestClient(server.create_app("ignored.yaml"))

    bodies = set()
    for tid in ("../etc", "ABCDEF123456", "0123456789ab"):
        resp = client.post("/api/resume", json={"thread_id": tid})
        assert resp.status_code == 404
        bodies.add(resp.text)
    assert len(bodies) == 1            # 非法、不存在响应逐字节一致

    r1 = _interrupted_ask(cfg, ex, monkeypatch)
    # 恢复从断点节点继续：generate 产物已在状态里，单步收尾不再调模型，
    # 未配密钥的实例也能完成 —— 与 /api/sql 免密钥同理
    d = client.post("/api/resume", json={"thread_id": r1.thread_id}).json()
    assert d["ok"] is True and d["thread_id"] == r1.thread_id
    assert d["trace_id"] != r1.trace_id
