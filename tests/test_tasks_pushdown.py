"""任务中心分页下推之后，**两条后端必须给出同一页**。

2026-09-15 把任务中心从"把最近两千条线程全部读进 Python 再筛再切页"改成
"聚合、折算、筛选、计数、分页全在 SQL 里"。这么改引入了一个新的失效方式：
状态 / 风险 / 长短任务这三档折算，audit.py 里有一份纯函数、auditstore 里有
一份等价的 SQL 表达式。两者一旦分叉，表现不是报错，而是**页面上的状态悄悄
不对**、或者筛选筛出一条写着别的状态的记录 —— 正是这个仓库反复踩的那类。

守法与 test_audit_pushdown 同一条思路，只是这里没法在 Python 里模拟一整套
CTE，所以改成把同一批记录**同时写进 PostgreSQL 与 JSONL**，让库后端与文件
后端各读一份，逐字段比对整页结果（items / total / total_all / stats /
sources / users）。文件后端那条路一行没动，因此它就是改造前的行为。

CORPUS 里每一条都对着一个真实的分支，不是编出来凑数的：状态九档、风险三档、
长短任务两档、复核痕迹四种、三份外部结论各自的已决与未决、发起占位被收尾
记录取代、老记录没有 thread_id、一条线程上两次执行。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from askdb import audit, auditstore

#: 阈值取一组好算的：扫描超 5000 算 MEDIUM（折半是 2500），结果满 200 行算
#: MEDIUM，超过 10 秒算长任务，超过 900 秒没收尾算陈旧。
FOLD_ARGS = dict(max_rows=200, max_scan_rows=5000, async_after_ms=10_000,
                 stale_after_s=900)


def _ts(seconds_ago: int) -> str:
    return (datetime.now().astimezone()
            - timedelta(seconds=seconds_ago)).isoformat(timespec="microseconds")


def _rec(trace: str, thread: str, ago: int, **kw) -> dict:
    """一条审计。默认是"正常收尾的小查询"，各条只改自己关心的那一维。"""
    base = {
        "trace_id": trace, "thread_id": thread, "ts": _ts(ago), "phase": "done",
        "kind": "ask", "org_id": 65, "role": "PRODUCT", "user": "alice",
        "question": f"这条问的是 {trace}", "rejected_by": "", "attempts": 1,
        "rows_returned": 3, "elapsed_ms": 1200, "cost_cny": 0.01,
        "step_count": 2, "multi_step": False, "source": "src_a",
        "source_name": "会员中心", "model": "qwen-max",
        "steps": [{"step": "generate_sql", "status": "ok", "model": "qwen-max",
                   "cost_cny": 0.01}],
    }
    base.update(kw)
    return base


#: 每条线程的最后一条记录决定状态，第一条决定归属与标题。
CORPUS: list[dict] = [
    # ---- 已完成 / 待复核的四种痕迹 ----
    _rec("d0000000000a", "th-done", 3600),
    _rec("d0000000000b", "th-blind", 3540, recall_blind=True),
    _rec("d0000000000c", "th-mask", 3480, mask_degraded=True),
    _rec("d0000000000d", "th-retry", 3420, attempts=3),
    _rec("d0000000000e", "th-converge", 3360, converged_early="token 触顶"),
    # 复核已决：一条采信、一条打回
    _rec("d0000000000f", "th-accepted", 3300, recall_blind=True),
    _rec("d00000000010", "th-returned", 3240, recall_blind=True),
    # ---- 拦截的各档 ----
    _rec("d00000000011", "th-high", 3180, rejected_by="R-02"),
    _rec("d00000000012", "th-quota", 3120, rejected_by="QUOTA"),
    _rec("d00000000013", "th-lowrule", 3060, rejected_by="R-05"),
    # R-11 的三种下场：未决审批 / 已批未用 / 没开单
    _rec("d00000000014", "th-ask-approve", 3000, rejected_by="R-11"),
    _rec("d00000000015", "th-approved", 2940, rejected_by="R-11"),
    _rec("d00000000016", "th-plain-reject", 2880, rejected_by="R-11"),
    # 运维：一条待处置、一条已处置
    _rec("d00000000017", "th-ops", 2820, rejected_by="EXEC"),
    _rec("d00000000018", "th-ops-done", 2760, rejected_by="DATASOURCE"),
    # 等补充
    _rec("d00000000019", "th-input", 2700, rejected_by="CLARIFY"),
    # 断点还在
    _rec("d0000000001a", "th-open", 2640, rejected_by="INTERRUPTED"),
    # ---- 风险与长短任务 ----
    _rec("d0000000001b", "th-scan", 2580, explain_rows=4000),
    _rec("d0000000001c", "th-rows", 2520, rows_returned=200),
    _rec("d0000000001d", "th-multi", 2460, multi_step=True),
    _rec("d0000000001e", "th-long", 2400, elapsed_ms=30_000),
    _rec("d0000000001f", "th-noelapsed", 2340, elapsed_ms=None),
    # ---- 另一个数据源、另一个发起人、匿名 ----
    _rec("d00000000020", "th-other-src", 2280, source="src_b", source_name="交易中心"),
    _rec("d00000000021", "th-bob", 2220, user="bob"),
    _rec("d00000000022", "th-anon", 2160, user=""),
    _rec("d00000000023", "th-nosrc", 2100, source="", source_name=""),
    # ---- 线程形态 ----
    # 发起占位与收尾记录同一个 trace：收尾一到，占位就该退场
    _rec("d00000000024", "th-placeholder", 2100, phase="started", elapsed_ms=None),
    _rec("d00000000024", "th-placeholder", 2040),
    # 一条线程上两次执行：归属看第一条（alice），状态看最后一条（bob 续的）
    _rec("d00000000025", "th-two", 1980, question="第一次问的"),
    _rec("d00000000026", "th-two", 1920, user="bob", question="续跑时问的",
         rejected_by="INTERRUPTED"),
    # 老记录没有 thread_id：一次调用自成一条线程
    _rec("d00000000027", "", 1860),
    # ---- 还在跑 / 已经陈旧 ----
    _rec("d00000000028", "th-running", 60, phase="started", elapsed_ms=None),
    _rec("d00000000029", "th-stale", 5400, phase="started", elapsed_ms=None),
    # 缓存命中：不进「按模型」那一维
    _rec("d0000000002a", "th-cached", 1800, cached=True, model="cache", steps=[]),
]

#: 三份外部结论，键是那条线程**最后一条**记录的 trace_id。
FOLD = audit.TaskFold(
    approval={"d00000000014": "REQUESTED", "d00000000015": "APPROVED"},
    review={"d0000000000f": "ACCEPTED", "d00000000010": "RETURNED"},
    ops={"d00000000018": "RESOLVED"},
    **FOLD_ARGS)


@pytest.fixture
def twin(cfg, audit_store):
    """同一批记录同时进库与进文件。返回 (库入口, 文件入口)。

    **两个入口读的是同一批记录**，所以任何差异都只可能来自读法本身 ——
    这正是这份用例要盯的东西。
    """
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "postgres"}
    # 这一句是这份用例的**前提**：cfg 真的路由到库。少了它，两边都在读文件，
    # 全部断言照样绿 —— 而 SQL 一行都没执行过。
    assert audit._resolve(cfg)[0] is not None, "cfg 没有路由到 PostgreSQL"
    for r in CORPUS:
        auditstore.append_audit(r)
    path = Path(cfg.audit_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in CORPUS) + "\n",
                    encoding="utf-8")
    return cfg, path


def _both(twin, **kw):
    """两条后端各跑一次同样的请求。

    resolve_stale 固定回 None（"查不到检查点"）—— 检查点库不在这份用例的
    射程内，而两边吃到的是同一个回调，折算结果仍然必须一致。
    """
    cfg, path = twin
    call = dict(fold=FOLD, resolve_stale=lambda _tid: None, **kw)
    return (audit.tasks_page(cfg, **call), audit.tasks_page(path, **call))


def _same(pg: dict, fs: dict, what: str) -> None:
    assert pg["stats"] == fs["stats"], f"{what}：统计卡对不上"
    assert pg["total"] == fs["total"], f"{what}：筛完的条数对不上"
    assert pg["total_all"] == fs["total_all"], f"{what}：筛之前的条数对不上"
    assert pg["sources"] == fs["sources"], f"{what}：数据源下拉对不上"
    assert pg["users"] == fs["users"], f"{what}：发起人下拉对不上"
    assert [i["thread_id"] for i in pg["items"]] == [i["thread_id"] for i in fs["items"]], \
        f"{what}：这一页是哪几条线程对不上"
    for a, b in zip(pg["items"], fs["items"]):
        assert a == b, f"{what}：线程 {a['thread_id']} 的字段对不上"


def test_default_page_is_identical(twin):
    """不带任何筛选的第一页 —— 状态、风险、长短任务、统计卡、两个下拉全比一遍。"""
    pg, fs = _both(twin, page=1, page_size=50)
    _same(pg, fs, "默认页")
    # 折算真的跑出了各档，不是两边一起空着
    assert len({i["status"] for i in pg["items"]}) >= 7
    assert {i["risk"] for i in pg["items"]} == {"HIGH", "MEDIUM", "LOW"}
    assert {i["task_kind"] for i in pg["items"]} == {"long", "short"}


@pytest.mark.parametrize("status", audit.TASK_STATUSES)
def test_status_filter_is_identical(twin, status):
    """九档状态逐档比。**这是折算分叉最先暴露的地方**。"""
    pg, fs = _both(twin, page=1, page_size=50, status=status)
    _same(pg, fs, f"status={status}")


@pytest.mark.parametrize("risk", audit.RISK_LEVELS)
def test_risk_filter_is_identical(twin, risk):
    pg, fs = _both(twin, page=1, page_size=50, risk=risk)
    _same(pg, fs, f"risk={risk}")


@pytest.mark.parametrize("kind", audit.TASK_KINDS)
def test_task_kind_filter_is_identical(twin, kind):
    pg, fs = _both(twin, page=1, page_size=50, task_kind_filter=kind)
    _same(pg, fs, f"task_kind={kind}")


@pytest.mark.parametrize("since", audit.SINCE_CHOICES)
def test_since_filter_is_identical(twin, since):
    pg, fs = _both(twin, page=1, page_size=50, since=since)
    _same(pg, fs, f"since={since}")


@pytest.mark.parametrize("source", ["src_a", "src_b", ""])
def test_source_filter_is_identical(twin, source):
    """空串是**一档**（未记录数据源），不是"不筛"。"""
    pg, fs = _both(twin, page=1, page_size=50, source=source)
    _same(pg, fs, f"source={source!r}")
    assert pg["total"] > 0


@pytest.mark.parametrize("user", ["alice", "bob", ""])
def test_user_filter_is_identical(twin, user):
    pg, fs = _both(twin, page=1, page_size=50, user=user)
    _same(pg, fs, f"user={user!r}")
    assert pg["total"] > 0


@pytest.mark.parametrize("q", ["第一次", "th-two", "d00000000021", "不会命中的词"])
def test_keyword_filter_is_identical(twin, q):
    """关键词命中面：问题原文、线程 id、trace id。发起人**不在**里面。"""
    pg, fs = _both(twin, page=1, page_size=50, q=q)
    _same(pg, fs, f"q={q!r}")


def test_keyword_does_not_match_the_user(twin):
    """搜 alice 搜不到东西 —— 任务中心这一页的命中面里没有发起人。
    审计中心那一页才搜发起人，两页本来就不同，别顺手统一。"""
    pg, fs = _both(twin, page=1, page_size=50, q="alice")
    _same(pg, fs, "q=alice")
    assert pg["total"] == 0


@pytest.mark.parametrize("page", [1, 2, 3, 4])
def test_paging_is_identical(twin, page):
    """逐页比。页与页之间不能重、不能漏 —— 排序键不确定时最先出这个毛病。"""
    pg, fs = _both(twin, page=page, page_size=5)
    _same(pg, fs, f"第 {page} 页")


def test_pages_cover_everything_exactly_once(twin):
    """把所有页拼起来，应当恰好等于一次取完 —— 不重不漏。"""
    seen: list[str] = []
    for page in range(1, 12):
        pg, _fs = _both(twin, page=page, page_size=4)
        seen += [i["thread_id"] for i in pg["items"]]
    whole, _ = _both(twin, page=1, page_size=100)
    assert seen == [i["thread_id"] for i in whole["items"]]
    assert len(seen) == len(set(seen)), "同一条线程在两页上都出现了"


def test_owner_scope_is_identical(twin):
    """只看某个人发起的 —— 归属按线程**第一条**判，续跑的人不会变成主人。"""
    pg, fs = _both(twin, only_user="alice", page=1, page_size=50)
    _same(pg, fs, "only_user=alice")
    assert all(i["owner"] == "alice" for i in pg["items"])
    # th-two 的最后一条是 bob 写的，但它仍算 alice 的
    assert "th-two" in [i["thread_id"] for i in pg["items"]]


def test_placeholder_retires_when_the_run_finishes(twin):
    """同一个 trace 的发起占位与收尾记录只算一次执行，且状态看收尾那条。"""
    pg, _fs = _both(twin, page=1, page_size=50)
    row = next(i for i in pg["items"] if i["thread_id"] == "th-placeholder")
    assert row["attempts_on_thread"] == 1
    assert row["status"] == audit.DONE


def test_stale_override_reaches_counts_and_filters(twin):
    """陈旧线程按检查点核实之后的状态，**计数与筛选看到的必须是核实后的那个**。

    核不过 → 执行期故障 → 等运维。放到分页之后再改的话，会出现
    「等待运维」筛不出这几条、而计数把它们记在「可续跑」名下。
    """
    pg, fs = _both(twin, page=1, page_size=50, status=audit.NEEDS_OPERATOR)
    _same(pg, fs, "陈旧线程核实后")
    assert "th-stale" in [i["thread_id"] for i in pg["items"]]
    assert pg["stats"]["needs_operator"] >= 2      # th-ops 与 th-stale


def test_resolved_stale_thread_does_not_return_to_the_queue(twin):
    """运维已处置过的陈旧线程不再回到待处置队列 —— 否则队列永远清不空。"""
    cfg, path = twin
    fold = replace(FOLD, ops={**FOLD.ops, "d00000000029": "WONTFIX"})
    call = dict(fold=fold, resolve_stale=lambda _tid: None, page=1, page_size=50)
    pg = audit.tasks_page(cfg, **call)
    fs = audit.tasks_page(path, **call)
    _same(pg, fs, "陈旧但已处置")
    row = next(i for i in pg["items"] if i["thread_id"] == "th-stale")
    assert row["status"] == audit.REJECTED


def test_resumable_stale_thread_stays_interruptible(twin):
    """核得过检查点的陈旧线程仍是「可续跑」，不该被打成故障。"""
    cfg, path = twin
    call = dict(fold=FOLD, resolve_stale=lambda _tid: True, page=1, page_size=50)
    pg = audit.tasks_page(cfg, **call)
    fs = audit.tasks_page(path, **call)
    _same(pg, fs, "陈旧但现场还在")
    row = next(i for i in pg["items"] if i["thread_id"] == "th-stale")
    assert row["status"] == audit.INTERRUPTED and row["resumable"] is True
