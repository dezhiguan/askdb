"""审计流水这一页，两条后端必须给出同一页。

这一页 2026-09-09 就把大部分维度下推到 SQL 了，2026-09-15 补上最后一维
（关键词）—— 在那之前，输入框里一有字，这一页就从"取十条"退回"把整份流水
拉回来在 Python 里扫一遍"，生产实测 0.19s → 1.15s，且随记录数线性增长。
而搜索恰恰是这一页最常用的动作。

与 test_tasks_pushdown 同一条思路：同一批记录同时进库与进文件，逐字段比对。
**遮蔽（with_text=False）那一档要一起比** —— 问题原文与发起人同属"内容"，
只抹显示、仍允许按它搜，等于留了一个预言机。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from askdb import audit, auditstore


def _ts(minutes_ago: int) -> str:
    return (datetime.now().astimezone()
            - timedelta(minutes=minutes_ago)).isoformat(timespec="microseconds")


def _rec(trace: str, ago: int, **kw) -> dict:
    base = {
        "trace_id": trace, "thread_id": f"th-{trace[-2:]}", "ts": _ts(ago),
        "phase": "done", "kind": "ask", "user": "alice", "rejected_by": "",
        "question": "广州有多少个 Java 岗位", "source": "src_a",
        "source_name": "会员中心", "elapsed_ms": 900, "cost_cny": 0.01,
    }
    base.update(kw)
    return base


CORPUS = [
    _rec("a00000000001", 5),
    _rec("a00000000002", 10, question="深圳的薪资中位数"),
    _rec("a00000000003", 15, user="bob", question="广州的岗位都要什么"),
    _rec("a00000000004", 20, user="", question=""),
    _rec("a00000000005", 25, rejected_by="R-02"),
    _rec("a00000000006", 30, rejected_by="INTERRUPTED"),
    _rec("a00000000007", 35, kind="sql", source="src_b", source_name="交易中心"),
    _rec("a00000000008", 40, source="", source_name=""),
    _rec("a00000000009", 45, phase="started"),          # 发起占位：这一页不列
    _rec("a0000000000a", 60 * 40),                      # 一天多以前
    # LIKE 元字符：不转义的话搜它等于搜"任意字符"，什么都能搜到
    _rec("a0000000000b", 50, question="命中率 100% 的那次"),
]


@pytest.fixture
def twin(cfg, audit_store):
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "postgres"}
    assert audit._resolve(cfg)[0] is not None, "cfg 没有路由到 PostgreSQL"
    for r in CORPUS:
        auditstore.append_audit(r)
    path = Path(cfg.audit_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in CORPUS) + "\n",
                    encoding="utf-8")
    return cfg, path


def _both(twin, **kw):
    cfg, path = twin
    return audit.list_audits(cfg, **kw), audit.list_audits(path, **kw)


def _same(pg: dict, fs: dict, what: str) -> None:
    for key in ("total", "total_all", "text_visible", "sources", "users"):
        assert pg[key] == fs[key], f"{what}：{key} 对不上（{pg[key]} != {fs[key]}）"
    assert [i["trace_id"] for i in pg["items"]] == [i["trace_id"] for i in fs["items"]], \
        f"{what}：这一页是哪几条对不上"
    for a, b in zip(pg["items"], fs["items"]):
        assert a == b, f"{what}：记录 {a['trace_id']} 的字段对不上"


@pytest.mark.parametrize("kw", [
    {},
    {"kind": "ask"},
    {"kind": "sql"},
    {"status": "ok"},
    {"status": "rejected"},
    {"status": "interrupted"},
    {"source": "src_a"},
    {"source": ""},
    {"user": "alice"},
    {"user": ""},
    {"since": "today"},
    {"since": "7d"},
    {"q": "广州"},
    {"q": "薪资"},
    {"q": "a00000000003"},
    {"q": "alice"},
    {"q": "100%"},
    {"q": "没有这个词"},
    {"q": "广州", "status": "ok", "source": "src_a"},
])
def test_same_page_from_both_backends(twin, kw):
    pg, fs = _both(twin, page=1, page_size=20, **kw)
    _same(pg, fs, str(kw))


def test_like_metacharacters_do_not_match_everything(twin):
    """搜 % 只应该搜到那条真的写着 % 的记录。"""
    pg, fs = _both(twin, page=1, page_size=20, q="%")
    _same(pg, fs, "q=%")
    assert [i["trace_id"] for i in pg["items"]] == ["a0000000000b"]


def test_masked_identity_cannot_search_by_content(twin):
    """看不到原文的身份，**也不能按原文搜** —— 只抹显示、仍允许按它搜，
    等于留了一个预言机（"搜广州能搜出 12 条"本身就把内容说出来了）。"""
    pg, fs = _both(twin, page=1, page_size=20, q="广州", with_text=False)
    _same(pg, fs, "遮蔽后搜内容")
    assert pg["total"] == 0 and pg["text_visible"] is False
    assert pg["users"] == []
    # trace_id 仍然搜得到：它不是内容
    pg2, fs2 = _both(twin, page=1, page_size=20, q="a00000000003", with_text=False)
    _same(pg2, fs2, "遮蔽后搜 trace_id")
    assert pg2["total"] == 1
    assert pg2["items"][0]["question"] is None


@pytest.mark.parametrize("page", [1, 2, 3])
def test_paging_matches(twin, page):
    pg, fs = _both(twin, page=page, page_size=3)
    _same(pg, fs, f"第 {page} 页")


def test_scope_to_one_user(twin):
    """只看自己的那些角色：范围排在关键词与分页之前，total 不能泄露别人有多少条。"""
    pg, fs = _both(twin, page=1, page_size=20, only_user="bob")
    _same(pg, fs, "only_user=bob")
    assert pg["total"] == 1 and pg["total_all"] == 1


def test_scope_and_user_filter_cannot_widen(twin):
    """可见范围是硬边界，手上的筛选退不出它。"""
    pg, fs = _both(twin, page=1, page_size=20, only_user="bob", user="alice")
    _same(pg, fs, "范围 bob + 筛 alice")
    assert pg["total"] == 0
