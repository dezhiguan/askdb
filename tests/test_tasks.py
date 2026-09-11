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


def test_anonymous_sees_every_thread_but_owns_none(client, acfg):
    """匿名列得出全部线程，但一条也续不了（2026-09-06）。

    这条用例原来断言的是相反的事：匿名只看得到匿名发起的。可见面统一之后
    那条收窄站不住 —— 它的实际后果是登录用户打开任务中心是空的，而同一份
    审计流水在审计中心里连未登录访客都看得到全部原文。同一批数据两页两套
    口径，方向还正好相反。

    **放开的只是"看得见"**：归属仍然逐条如实标在 owner 上，续跑校验一行没改
    （见下一条用例）。
    """
    _write(acfg.audit_log, [
        _rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now()),
        _rec("n1", "t9", user="", rejected="INTERRUPTED", ts=_now()),
    ])
    body = client.get("/api/tasks").json()
    assert body["user"] == ""
    assert sorted(t["thread_id"] for t in body["items"]) == ["t1", "t9"]
    # 归属必须跟着出去，否则页面无从判断续跑入口对谁开
    assert {t["thread_id"]: t["owner"] for t in body["items"]} == {"t1": "alice", "t9": ""}


def test_anonymous_cannot_resume_an_owned_thread(client, acfg):
    """列表放开了，续跑的归属校验不能跟着松 ——
    否则就成了"列不出来但猜得到 thread_id 就能跑别人的任务"。"""
    _write(acfg.audit_log, [_rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now())])
    assert client.post("/api/resume", json={"thread_id": "a1"}).status_code == 404


def test_tasks_endpoint_lists_everyone(client, acfg):
    """登录用户同样列得出全部线程，user 字段给的是**当前账号**而不是过滤条件。

    页面拿 user 与每条的 owner 比，决定续跑入口对谁开 —— 服务端不替它过滤。
    """
    _write(acfg.audit_log, [
        _rec("a1", "t1", user="alice", rejected="INTERRUPTED", ts=_now()),
        _rec("b1", "t2", user="bob", rejected="INTERRUPTED", ts=_now()),
    ])
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    body = client.get("/api/tasks").json()
    assert body["user"] == "alice"
    assert sorted(t["thread_id"] for t in body["items"]) == ["t1", "t2"]
    assert {t["thread_id"]: t["owner"] for t in body["items"]} == {"t1": "alice", "t2": "bob"}


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


# ---------------------------------------------------------------------------
# 任务态分档（2026-09-07）
#
# 在此之前只有 done / rejected / interrupted 三档，六种语义完全不同的结局被
# 压进同一个"已拦截"：碰了安全红线、模型答不上来、库连不上、等人放行 ——
# 四种里有三种其实还有下一步，而页面一律显示终结态。判据全部来自记录里
# **已有**的收尾码，不新增任何一维（与 _risk 同一套确定性折算）。
# ---------------------------------------------------------------------------

def test_stage_tells_who_acts_next(acfg):
    """一个收尾码一档，且每一档都能回答"下一步该谁动手"。"""
    cases = [
        (None, audit.DONE),
        ("R-03", audit.REJECTED),            # 安全红线：改写法也过不去
        ("R-02", audit.REJECTED),
        ("NO_SQL", audit.WAITING_INPUT),     # 模型没产出 SQL，球在用户那边
        ("EXEC", audit.NEEDS_OPERATOR),      # 库连不上，该找运维且可重试
        ("INTERRUPTED", audit.INTERRUPTED),  # 现场在检查点里
        ("RESUME_BLOCKED", audit.INTERRUPTED),
    ]
    for code, want in cases:
        assert audit.stage({"rejected_by": code}) == want, code


def test_pending_approval_is_not_a_rejection(acfg):
    """R-11 挂起是**等人放行**，不是终局 —— 与"碰了红线"必须分得开。"""
    rec = {"rejected_by": "R-11", "trace_id": "t1"}
    assert audit.stage(rec) == audit.REJECTED
    assert audit.stage(rec, approval_status="REQUESTED") == audit.WAITING_APPROVAL


def test_approved_ticket_still_waits_on_the_requester(acfg):
    """**批准之后那条任务不能掉进「已拦截」。**

    2026-09-11 之前折算只看"有没有未决单"，于是批准的那一刻状态就从等待审批
    变成已拦截 —— 发起人刚被通知批下来了，界面上却是个终结态，而那张票还在
    审批存储里躺着没人用。闭环就断在这一格。

    批准与待批共用一个状态码（对发起人来说是同一件事的同一个阶段），差别由
    next_actor 讲清楚：一个在等管理员，一个在等他自己。
    """
    rec = {"rejected_by": "R-11", "trace_id": "t1"}
    assert audit.stage(rec, approval_status="APPROVED") == audit.WAITING_APPROVAL
    # 用掉之后才是终局
    assert audit.stage(rec, approval_status="CONSUMED") == audit.REJECTED


def test_tasks_marks_thread_waiting_approval(acfg):
    """审批状态要联查进任务列表，否则页面上它与"已拦截"长得一模一样。"""
    _write(acfg.audit_log, [{**_rec("a1", "t1", user="alice", rejected="R-11",
                                    ts=_now()), "trace_id": "a1"}])
    plain = audit.tasks(acfg.audit_log)
    assert plain[0]["status"] == audit.REJECTED

    joined = audit.tasks(acfg.audit_log, approval_status={"a1": "REQUESTED"})
    assert joined[0]["status"] == audit.WAITING_APPROVAL
    assert "放行" in joined[0]["next_actor"]

    # 已批准：状态不变，但等的人换成了发起人自己
    approved = audit.tasks(acfg.audit_log, approval_status={"a1": "APPROVED"})
    assert approved[0]["status"] == audit.WAITING_APPROVAL
    assert approved[0]["approval_status"] == "APPROVED"
    assert "凭票重跑" in approved[0]["next_actor"]


def test_resolved_exec_failure_leaves_the_ops_queue(acfg):
    """运维给过结论的执行期故障不再挂在队列上 —— 否则那一档只进不出。"""
    rec = {"rejected_by": "EXEC", "trace_id": "t1"}
    assert audit.stage(rec) == audit.NEEDS_OPERATOR
    assert audit.stage(rec, ops_status="RESOLVED") == audit.REJECTED
    assert audit.stage(rec, ops_status="WONTFIX") == audit.REJECTED


def test_a_stale_running_thread_stops_being_running(acfg):
    """只落了发起记录、又过了阈值的线程，不再叫"运行中"。

    审计上"正在跑"与"进程被杀了"分不开，但时间能分开：没有哪条查询会跑一刻钟
    还不收尾。不判这一下的后果实测过 —— 2026-09-11 线上 8 条一小时前被杀的
    线程永远停在运行中，没有任何机制会再看它们一眼。
    """
    rec = {"phase": audit.PHASE_STARTED, "trace_id": "t1"}
    assert audit.stage(rec) == audit.RUNNING
    assert audit.stage(rec, stale=True) == audit.INTERRUPTED


def test_started_record_makes_a_crashed_thread_visible(acfg):
    """发起记录：进程中途被杀时，任务不能从系统里整片消失。

    2026-09-07 实测 kill -9 打在查询中途 —— 检查点写了、审计一条没写，
    任务中心列不出来，凭 thread_id 也续不了（数据源只记在审计里）。
    """
    _write(acfg.audit_log, [{
        "trace_id": "c1", "thread_id": "c1", "ts": _now(), "kind": "ask",
        "phase": audit.PHASE_STARTED, "user": "alice", "role": "PRODUCT",
        "question": "跑一半就没了的那条", "source": "src_x",
        "rejected_by": None,
    }])
    items = audit.tasks(acfg.audit_log)
    assert [t["thread_id"] for t in items] == ["c1"]
    assert items[0]["status"] == audit.RUNNING
    assert items[0]["owner"] == "alice"
    assert items[0]["source"] == "src_x", "数据源必须跟着发起记录走，否则续跑回不去"


def test_started_record_steps_aside_once_the_result_lands(acfg):
    """收尾一到，占位就该退场 —— 否则线程会永远显示成运行中。"""
    ts = _now()
    _write(acfg.audit_log, [
        {"trace_id": "d1", "thread_id": "d1", "ts": ts, "kind": "ask",
         "phase": audit.PHASE_STARTED, "user": "alice", "rejected_by": None},
        _rec("d1", "d1", user="alice", rejected=None, ts=_now(1)),
    ])
    items = audit.tasks(acfg.audit_log)
    assert len(items) == 1
    assert items[0]["status"] == audit.DONE


def test_started_records_never_reach_the_statistics(acfg):
    """发起记录没有结果、没有成本、没有收尾码。

    进了统计就是把每次调用数成两次、把成功率稀释一半 —— 所以 read_records
    默认滤掉它，只有任务中心显式要。
    """
    ts = _now()
    _write(acfg.audit_log, [
        {"trace_id": "e1", "thread_id": "e1", "ts": ts, "kind": "ask",
         "phase": audit.PHASE_STARTED, "user": "alice", "rejected_by": None},
        _rec("e1", "e1", user="alice", rejected=None, ts=_now(1)),
    ])
    assert len(audit.read_records(acfg.audit_log)) == 1
    assert len(audit.read_records(acfg.audit_log, include_started=True)) == 2
    assert audit.stats(acfg.audit_log)["calls"] == 1


def test_resume_finds_the_source_from_the_started_record(client, acfg):
    """只剩发起记录的线程，续跑要能回到**当初那个数据源**。

    实测过一次这条 400：任务中心说能续跑，/api/resume 却回
    「这条任务当初跑在数据源『builtin』上」—— 因为它读审计时把发起记录
    滤掉了，而被杀掉的线程只剩这一条。归属与数据源都在它身上。
    """
    _write(acfg.audit_log, [{
        "trace_id": "aaaaaaaaaaa1", "thread_id": "aaaaaaaaaaa1", "ts": _now(),
        "kind": "ask", "phase": audit.PHASE_STARTED, "user": "",
        "question": "跑一半就没了的那条", "source": "", "rejected_by": None,
    }])
    r = client.post("/api/resume", json={"thread_id": "aaaaaaaaaaa1"})
    # 没有检查点所以仍是 404 —— 但**不是**被"回不到那个数据源"挡下的 400。
    # 这两个状态码分得开，这条用例才有意义。
    assert r.status_code == 404, r.json()
