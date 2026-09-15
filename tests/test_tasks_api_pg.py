"""任务中心与两条队列，**接着真库跑一遍**。

tests/test_tasks_pushdown.py 验的是口径（两条后端同一个答案），这一份验的是
接线：/api/tasks 的筛选参数有没有一路传到 SQL、统计卡与下拉有没有出接口、
复核与运维队列有没有和任务中心走同一条折算链路。

两件事分开守的理由与 test_audit_store_pg 那段一样：口径用例在 Python 里比，
接线错了它一条都不会红。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from askdb import audit, auditstore, server


def _ts(minutes_ago: int) -> str:
    return (datetime.now().astimezone()
            - timedelta(minutes=minutes_ago)).isoformat(timespec="microseconds")


def _rec(trace: str, thread: str, ago: int, **kw) -> dict:
    base = {
        "trace_id": trace, "thread_id": thread, "ts": _ts(ago), "phase": "done",
        "kind": "ask", "user": "", "question": f"问的是 {thread}",
        "rejected_by": "", "attempts": 1, "rows_returned": 1, "elapsed_ms": 900,
        "cost_cny": 0.01, "source": "src_a", "source_name": "会员中心",
        "model": "qwen-max", "multi_step": False,
        "steps": [{"step": "generate_sql", "status": "ok", "model": "qwen-max"}],
    }
    base.update(kw)
    return base


SEED = [
    _rec("p00000000001", "th-a", 50),
    _rec("p00000000002", "th-b", 45),
    _rec("p00000000003", "th-c", 40, rejected_by="R-02"),
    _rec("p00000000004", "th-d", 35, rejected_by="EXEC"),          # 等运维
    _rec("p00000000005", "th-e", 30, recall_blind=True),           # 等复核
    _rec("p00000000006", "th-f", 25, elapsed_ms=30_000),           # 长任务
    _rec("p00000000007", "th-g", 20, source="src_b", source_name="交易中心"),
]


@pytest.fixture
def client(cfg, audit_store, monkeypatch):
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "postgres"}
    assert audit._resolve(cfg)[0] is not None, "cfg 没有路由到 PostgreSQL"
    for r in SEED:
        auditstore.append_audit(r)
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_tasks_page_comes_back_whole(client):
    got = client.get("/api/tasks?page=1&page_size=3").json()
    assert got["total"] == len(SEED) and got["total_all"] == len(SEED)
    assert len(got["items"]) == 3
    assert got["page"] == 1 and got["page_size"] == 3
    # 统计卡算在筛选之前：这里没筛，两个数自然相等
    assert got["stats"]["done"] + got["stats"]["rejected"] \
        + got["stats"]["needs_operator"] + got["stats"]["waiting_review"] == len(SEED)
    assert {s["value"] for s in got["sources"]} == {"src_a", "src_b"}
    assert got["window"] == {"max_threads": 0, "truncated": False}


def test_paging_does_not_repeat_or_drop(client):
    seen: list[str] = []
    for page in (1, 2, 3, 4):
        seen += [i["thread_id"] for i in
                 client.get(f"/api/tasks?page={page}&page_size=2").json()["items"]]
    assert sorted(seen) == sorted(r["thread_id"] for r in SEED)


@pytest.mark.parametrize("query,want", [
    ("status=rejected", ["th-c"]),
    ("status=needs_operator", ["th-d"]),
    ("status=waiting_review", ["th-e"]),
    ("risk=HIGH", ["th-c"]),
    ("task_kind=long", ["th-f"]),
    ("source=src_b", ["th-g"]),
    ("q=th-a", ["th-a"]),
])
def test_filters_reach_the_database(client, query, want):
    """每个筛选参数都要真的传到 SQL。**统计卡不跟着变** —— 它讲的是系统
    当下的处境，跟着筛选走的话，筛完「已完成」再看「待处理」永远是 0。"""
    got = client.get(f"/api/tasks?{query}").json()
    assert [i["thread_id"] for i in got["items"]] == want
    assert got["total"] == len(want)
    assert got["total_all"] == len(SEED)


def test_bad_filter_value_is_refused(client):
    """认不出来的档一律 400。当成"不筛"处理会让人以为筛过了。"""
    assert client.get("/api/tasks?status=没这一档").status_code == 400


def test_queues_agree_with_the_task_centre(client):
    """复核 / 运维两条队列与任务中心走同一条折算链路。

    2026-09-12 线上实测过一次分叉：任务中心显示 9 条等待运维、运维队列只有
    1 条 —— 那 8 条僵尸线程在"该去处理它们的那一页"上根本看不见。
    """
    tasks = client.get("/api/tasks?status=needs_operator").json()
    ops = client.get("/api/ops").json()
    assert [i["thread_id"] for i in ops["items"]] == \
        [i["thread_id"] for i in tasks["items"]]

    tasks = client.get("/api/tasks?status=waiting_review").json()
    reviews = client.get("/api/reviews").json()
    assert reviews["pending"] == tasks["total"] == 1
