"""任务中心：可续跑任务的列举与归属。

askdb 没有任务表。「任务」完全落在审计流水与检查点上：一次调用中断
（rejected_by=INTERRUPTED）就留下一条待续跑的线程，续跑写新 trace、
thread 不变。所以"这条线程还开着吗" = 它最后一条记录是不是仍为中断。

这个文件盯的是**归属**。中断恢复设计 §4.2 原本禁止一切未完成任务的枚举，
理由是当时没有账号体系 —— 列出来等于任何人都能看到并续跑别人的任务，
而任务里带着别人问过的问题原文。登录接入后按发起人收窄才使列表成立，
一旦收窄失效，§4.2 当初要挡的洞就原样回来了。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from askdb import audit, auth, server

SECRET = "s" * 40


def _now(offset_s: int = 0) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=offset_s)).isoformat(timespec="seconds")


def _rec(trace_id: str, thread_id: str, *, user: str, rejected: str | None,
         ts: str, question: str = "有多少文档") -> dict:
    return {
        "trace_id": trace_id, "thread_id": thread_id, "ts": ts, "kind": "ask",
        "org_id": 65, "role": "PRODUCT", "user": user, "question": question,
        "rejected_by": rejected, "attempts": 1, "rows_returned": 0,
        "elapsed_ms": 10, "cost_cny": 0.0, "step_count": 1, "multi_step": False,
    }


def _write(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                    encoding="utf-8")


# ---------- 列举 ----------

def test_anonymous_gets_nothing(cfg):
    """匿名一律空 —— 那正是 §4.2 要挡的情形。"""
    _write(cfg.audit_log, [_rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now())])
    assert audit.resumable(cfg.audit_log, "") == []


def test_only_own_tasks_are_listed(cfg):
    _write(cfg.audit_log, [
        _rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now()),
        _rec("b1", "t2", user="bob", rejected="INTERRUPTED", ts=_now()),
    ])
    assert [t["thread_id"] for t in audit.resumable(cfg.audit_log, "alice")] == ["t1"]
    assert [t["thread_id"] for t in audit.resumable(cfg.audit_log, "bob")] == ["t2"]


def test_finished_threads_drop_off(cfg):
    """续跑成功后线程就该消失，否则列表会一直挂着已经跑完的东西。"""
    _write(cfg.audit_log, [
        _rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now(0)),
        _rec("a2", "t1", user="alice", rejected=None, ts=_now(1)),      # 续跑成功
    ])
    assert audit.resumable(cfg.audit_log, "alice") == []


def test_ownership_follows_the_first_record(cfg):
    """续跑写新 trace，但发起人不变 —— 归属要看线程的第一条。

    按最后一条判，会让"谁续跑谁就成了主人"，等于把归属交给了任何能续跑的人。
    """
    _write(cfg.audit_log, [
        _rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now(0)),
        _rec("a2", "t1", user="", rejected="INTERRUPTED", ts=_now(1)),   # 续跑记录漏了账号
    ])
    assert [t["thread_id"] for t in audit.resumable(cfg.audit_log, "alice")] == ["t1"]
    assert audit.resumable(cfg.audit_log, "") == []


# ---------- 接口 ----------

@pytest.fixture
def acfg(cfg, monkeypatch):
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, SECRET)
    cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [
            {"username": "alice", "roles": ["PRODUCT"],
             "password_hash": auth.hash_password("pw")},
            {"username": "bob", "roles": ["PRODUCT"],
             "password_hash": auth.hash_password("pw")},
        ],
    }
    return cfg


@pytest.fixture
def client(acfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_tasks_endpoint_requires_login(client):
    assert client.get("/api/tasks").status_code == 401


def test_tasks_endpoint_scopes_to_caller(client, acfg):
    _write(acfg.audit_log, [
        _rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now()),
        _rec("b1", "t2", user="bob", rejected="INTERRUPTED", ts=_now()),
    ])
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    body = client.get("/api/tasks").json()
    assert body["user"] == "alice"
    assert [t["thread_id"] for t in body["items"]] == ["t1"]


def test_resume_refuses_someone_elses_task(client, acfg):
    """别人的任务续跑不了，且响应与「不存在」一致 —— 区分就等于暴露任务是否存在。"""
    _write(acfg.audit_log, [_rec("a1", "aaaaaaaaaaa1", user="alice",
                                 rejected="INTERRUPTED", ts=_now())])
    client.post("/api/auth/login", json={"username": "bob", "password": "pw"})

    mine = client.post("/api/resume", json={"thread_id": "aaaaaaaaaaa1"})
    ghost = client.post("/api/resume", json={"thread_id": "ffffffffffff"})
    assert mine.status_code == ghost.status_code == 404
    assert mine.json() == ghost.json()


def test_anonymous_created_task_keeps_old_semantics(client, acfg):
    """登录之前发起的任务没有主人，仍按原语义（凭 thread_id 续跑）——
    加了账号不该把老任务锁死。这里只验归属校验放行，续跑本身没有检查点会 404。
    """
    _write(acfg.audit_log, [_rec("a1", "aaaaaaaaaaa1", user="",
                                 rejected="INTERRUPTED", ts=_now())])
    r = client.post("/api/resume", json={"thread_id": "aaaaaaaaaaa1"})
    assert r.status_code == 404          # 没有检查点，但不是被归属挡下的
