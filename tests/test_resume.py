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


# ---------------------------------------------------------------- 恢复前重新校验


def _blocked(cfg, ex, monkeypatch, tweak) -> tuple[graph.AskResult, graph.AskResult]:
    """先制造一次中断，再按 tweak 改变续跑时的前提，返回（中断、续跑）两次结果。"""
    r1 = _interrupted_ask(cfg, ex, monkeypatch)
    tweak()
    r2 = graph.resume(r1.thread_id, cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert r2 is not None
    return r1, r2


def test_resume_blocked_when_table_no_longer_visible(cfg, ex, monkeypatch):
    """中断期间表被移出可见范围 —— 续跑必须停在校验，不能拿旧前提接着跑。"""
    def revoke() -> None:
        for name in list(cfg.tables):
            cfg.tables.pop(name)

    _, r2 = _blocked(cfg, ex, monkeypatch, revoke)
    assert r2.ok is False and r2.rejected_by == graph.RESUME_BLOCKED
    assert "可见范围" in r2.error
    # 一次模型调用都不该花：校验在配额与图执行之前
    assert r2.tok_in == 0 and r2.tok_out == 0


def test_resume_blocked_on_schema_drift(cfg, ex, monkeypatch):
    """白名单声明的列在库里没了 —— 检查点里那条 SQL 的前提已经不成立。"""
    def drift() -> None:
        from askdb.config import Column

        t = next(iter(cfg.tables.values()))
        t.columns["column_that_never_existed"] = Column(
            name="column_that_never_existed", type="TEXT")

    _, r2 = _blocked(cfg, ex, monkeypatch, drift)
    assert r2.ok is False and r2.rejected_by == graph.RESUME_BLOCKED
    assert "表结构" in r2.error


def test_blocked_resume_keeps_the_task_resumable(cfg, ex, monkeypatch):
    """被挡下不是终态：检查点还在，条件恢复后照样能续 —— 任务中心也得这么看。"""
    from askdb.audit import tasks

    r1 = _interrupted_ask(cfg, ex, monkeypatch)
    saved = dict(cfg.tables)
    cfg.tables.clear()
    blocked = graph.resume(r1.thread_id, cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert blocked is not None and blocked.rejected_by == graph.RESUME_BLOCKED

    row = next(t for t in tasks(cfg.audit_log, None) if t["thread_id"] == r1.thread_id)
    assert row["status"] == "interrupted" and row["resumable"] is True

    cfg.tables.update(saved)          # 条件恢复，续跑应当照常完成
    ok = graph.resume(r1.thread_id, cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert ok is not None and ok.ok is True


def test_blocked_resume_is_audited(cfg, ex, monkeypatch):
    """挡下来这件事必须留痕，否则"我点了续跑没反应"事后查不出原因。"""
    r1 = _interrupted_ask(cfg, ex, monkeypatch)
    cfg.tables.clear()
    r2 = graph.resume(r1.thread_id, cfg, executor=ex, llm=FakeLlm(OK_SQL))
    rec = get_audit(cfg.audit_log, r2.trace_id)
    assert rec["kind"] == "resume" and rec["rejected_by"] == graph.RESUME_BLOCKED
    assert rec["thread_id"] == r1.thread_id
