"""结果复核（WAITING_REVIEW）。

**与审批是两件事，这批用例首先钉的就是这一点。**
  · 审批是事前：这条查询该不该去跑（判据 R-11 扫描量），放行是一次性的票。
  · 复核是事后：跑出来的数字算不算数（判据是审计里的存疑痕迹）。
决策人恰好都是系统管理员、动作恰好都是放行/打回，但触发时机与判定对象
完全不同 —— 若哪天连触发条件也一样了，就该删掉其中一套。
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from askdb import audit, auth, reviews, server

SECRET = "s" * 40


def _client(cfg, monkeypatch, *, roles):
    c = copy.deepcopy(cfg)
    c.raw = copy.deepcopy(cfg.raw)
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, SECRET)
    c.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [
            {"username": "boss", "roles": ["SYS_ADMIN"],
             "password_hash": auth.hash_password("pw")},
            {"username": "dev", "roles": ["DEV"],
             "password_hash": auth.hash_password("pw")},
        ],
    }
    monkeypatch.setattr(server, "load", lambda _p: c)
    client = TestClient(server.create_app("ignored.yaml"))
    assert client.post("/api/auth/login",
                       json={"username": roles, "password": "pw"}).status_code == 200
    return client, c


def _write(cfg, rec):
    with open(cfg.audit_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _ok_rec(trace_id="ab12ab12ab12", **extra):
    return {
        "trace_id": trace_id, "thread_id": trace_id, "kind": "ask",
        "ts": "2026-09-07T10:00:00+08:00", "user": "dev", "role": "DEV",
        "question": "一共有多少个用户", "rejected_by": None, "attempts": 1,
        "rows_returned": 1, "elapsed_ms": 10, "cost_cny": 0.0,
        **extra,
    }


# ---- 触发判据 ---------------------------------------------------------------

def test_only_successful_results_can_need_review():
    """被拦下的那些本来就没给出数字，没有"采不采信"可言。"""
    assert not audit.needs_review({"rejected_by": "R-03", "recall_blind": True})
    assert audit.needs_review({"rejected_by": None, "recall_blind": True})


def test_each_trace_is_a_named_reason():
    """判据是确定性的，且每一条都要能说出**为什么** ——
    复核这件事的全部意义就是说得清，所以不做可信度分数。"""
    why = audit.review_reasons(
        {"recall_blind": True, "mask_degraded": True, "attempts": 4,
         "converged_early": "token 触顶"})
    assert len(why) == 4
    assert any("盲选" in w for w in why)
    assert any("脱敏" in w for w in why)
    assert any("4 次" in w for w in why)


def test_clean_result_is_just_done():
    assert audit.stage(_ok_rec()) == audit.DONE
    assert audit.stage(_ok_rec(recall_blind=True)) == audit.WAITING_REVIEW


def test_decision_takes_the_thread_out_of_the_queue():
    """采信后回到普通「已完成」，打回是**另一档**，不能与护栏拦截混为一谈。"""
    rec = _ok_rec(recall_blind=True)
    assert audit.stage(rec, review_status="ACCEPTED") == audit.DONE
    assert audit.stage(rec, review_status="RETURNED") == audit.REVIEW_RETURNED


# ---- 接口 -------------------------------------------------------------------

def test_queue_lists_pending_with_reasons(cfg, monkeypatch):
    client, c = _client(cfg, monkeypatch, roles="boss")
    _write(c, _ok_rec(recall_blind=True))
    body = client.get("/api/reviews").json()
    assert body["can_review"] is True
    assert body["pending"] == 1
    assert any("盲选" in w for w in body["items"][0]["review_why"])


def test_developer_cannot_review(cfg, monkeypatch):
    """与审批同一道门：只有系统管理员。"""
    client, c = _client(cfg, monkeypatch, roles="dev")
    _write(c, _ok_rec(recall_blind=True))
    assert client.get("/api/reviews").json()["can_review"] is False
    r = client.post("/api/reviews/ab12ab12ab12/decide",
                    json={"accepted": True, "note": ""})
    assert r.status_code == 403


def test_reviewer_cannot_review_own_result(cfg, monkeypatch):
    """自核与自批是同一个洞：系统管理员现在也能查数。"""
    client, c = _client(cfg, monkeypatch, roles="boss")
    _write(c, _ok_rec(recall_blind=True, user="boss"))
    r = client.post("/api/reviews/ab12ab12ab12/decide",
                    json={"accepted": True, "note": ""})
    assert r.status_code == 403
    assert "自己" in r.json()["detail"]


def test_cannot_review_a_record_that_needs_none(cfg, monkeypatch):
    """复核队列不是一个用来试探"某条记录存不存在"的入口 ——
    不存在、被拦下、本就不需要复核，一律同一句 404。"""
    client, c = _client(cfg, monkeypatch, roles="boss")
    _write(c, _ok_rec())                       # 干净结果，不需要复核
    clean = client.post("/api/reviews/ab12ab12ab12/decide",
                        json={"accepted": True, "note": ""})
    ghost = client.post("/api/reviews/ffffffffffff/decide",
                        json={"accepted": True, "note": ""})
    assert clean.status_code == ghost.status_code == 404
    assert clean.json() == ghost.json()


def test_decision_shows_up_in_the_task_status(cfg, monkeypatch):
    """打回之后任务态要真的变 —— 不然复核就只是往文件里写了一行字。"""
    client, c = _client(cfg, monkeypatch, roles="boss")
    _write(c, _ok_rec(recall_blind=True))
    assert client.get("/api/tasks").json()["items"][0]["status"] == audit.WAITING_REVIEW

    r = client.post("/api/reviews/ab12ab12ab12/decide",
                    json={"accepted": False, "note": "召回错表，数字不可用"})
    assert r.status_code == 200
    assert r.json()["status"] == reviews.RETURNED

    item = client.get("/api/tasks").json()["items"][0]
    assert item["status"] == audit.REVIEW_RETURNED
    assert "不采信" in item["next_actor"]


def test_accepted_result_returns_to_done(cfg, monkeypatch):
    client, c = _client(cfg, monkeypatch, roles="boss")
    _write(c, _ok_rec(recall_blind=True))
    client.post("/api/reviews/ab12ab12ab12/decide",
                json={"accepted": True, "note": "抽查过，数字对"})
    assert client.get("/api/tasks").json()["items"][0]["status"] == audit.DONE


def test_review_store_is_separate_from_approvals(cfg):
    """同一条 trace 完全可能既有过审批又需要复核，挤在一个键上会互相覆盖。"""
    from askdb import approvals

    assert reviews.store(cfg) != approvals.store(cfg)
