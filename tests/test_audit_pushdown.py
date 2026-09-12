"""下推之后，SQL 与 Python 必须对同一批记录给出同一个答案。

2026-09-09 把审计读取从"全量读回来再筛"改成"能下推的下推到 SQL"。
这么改立刻引入了一个新的失效方式：同一个语义写了两遍 —— auditstore._where
生成 WHERE 子句，audit.matches 判一条 dict。两者一旦分叉，表现不是报错，
而是**页面上少了几条记录且没有任何提示**，正是这个仓库反复踩的那类 bug。

这份用例守两件事：

  1. **超集契约**：_where 可以多带回来，不能漏。判定的出处是 matches，
     所以库后端拿到结果后还会再过一遍它 —— 前提是 SQL 没把该留的筛掉。
  2. **折算细节**：kind 为空/缺失、rejected_by 的三档、trace_id 为空、
     started 记录 —— 这四处是两边最容易对不齐的地方，各钉一条。

用例不连库：SQL 那半边用一个极小的 WHERE 求值器在 Python 里跑。
连库的版本要 PostgreSQL，而这几条折算恰恰在没有库的机器上也必须成立。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from askdb import auditstore
from askdb.audit import AuditFilter, matches

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)


def _rec(**kw):
    """一条审计记录。默认是"正常收尾的问答"，各用例只改自己关心的那一维。"""
    base = {
        "trace_id": "abc123456789", "ts": T0.isoformat(), "phase": "done",
        "kind": "ask", "user": "amy", "source": "s1", "rejected_by": "",
        "thread_id": "th-1",
    }
    base.update(kw)
    return base


def _cols(rec):
    """把记录折算成入库时那几列 —— 与 auditstore.append_audit 逐字同一套写法。

    这里必须照抄而不是调用它：append_audit 要连库。抄一份的代价是它变了
    这里不会自动跟着变，所以下面 test_column_projection_matches_writer
    直接读源码钉住这几行。
    """
    from askdb.auditstore import _ts_of

    return {
        "trace_id": str(rec.get("trace_id") or ""),
        "thread_id": str(rec.get("thread_id") or ""),
        "phase": str(rec.get("phase") or ""),
        "kind": str(rec.get("kind") or ""),
        "username": str(rec.get("user") or ""),
        "source": str(rec.get("source") or ""),
        "rejected_by": str(rec.get("rejected_by") or ""),
        "ts": _ts_of(rec),
    }


def _sql_keeps(rec, f) -> bool:
    """在 Python 里求值 _clauses 生成的那些子句 —— 用例不连库的替身。

    只支持 _clauses 实际会生成的那几种子句形状；出现没见过的形状就失败，
    免得将来加了新条件而这份用例悄悄放行。

    走 _clauses 而不是把 _where 的字符串再切开：带括号的子句里本来就有
    " AND "，切字符串会把它劈成两半 —— 那是解析器的问题，不是被测代码的问题，
    而这种假失败最会把人引到错误的地方去改。
    """
    clauses, args = auditstore._clauses(f)
    cols = _cols(rec)
    it = iter(args)
    for c in clauses:
        if c == "trace_id <> ''":
            ok = cols["trace_id"] != ""
        elif c == "phase <> 'started'":
            ok = cols["phase"] != "started"
        elif c == "ts >= %s":
            ok = cols["ts"] >= next(it)
        elif c == "trace_id = %s":
            ok = cols["trace_id"] == next(it)
        elif c == "username = %s":
            ok = cols["username"] == next(it)
        elif c == "kind IN ('ask', '')":
            ok = cols["kind"] in ("ask", "")
        elif c == "kind = %s":
            ok = cols["kind"] == next(it)
        elif c == "source = %s":
            ok = cols["source"] == next(it)
        elif c == "rejected_by = ''":
            ok = cols["rejected_by"] == ""
        elif c == "rejected_by = ANY(%s)":
            ok = cols["rejected_by"] in next(it)
        elif c == "(rejected_by <> '' AND NOT (rejected_by = ANY(%s)))":
            codes = next(it)
            ok = cols["rejected_by"] != "" and cols["rejected_by"] not in codes
        elif c.startswith("COALESCE(NULLIF(thread_id, ''), trace_id) = ANY("):
            tid = cols["thread_id"] or cols["trace_id"]
            ok = tid in next(it)
        elif c == "false":
            ok = False
        else:                                        # pragma: no cover
            pytest.fail(f"_clauses 生成了这份用例不认识的子句：{c!r}")
        if not ok:
            return False
    return True


#: 覆盖四处折算的记录集。每一条都是真会出现的形状，不是编出来凑数的。
CORPUS = [
    _rec(),
    _rec(kind=""),                                   # 老记录：入库空串，Python 兜底成 ask
    _rec(kind=None),                                 # 字段在但为 null
    _rec(kind="sql"),
    _rec(rejected_by="R-02"),
    _rec(rejected_by="INTERRUPTED"),
    _rec(rejected_by="RESUME_BLOCKED"),
    _rec(rejected_by="EXEC"),
    _rec(phase="started"),
    _rec(trace_id=""),                               # 没有 trace_id 的不算数
    _rec(user=""),                                   # 匿名发起：空串是合法一档
    _rec(source=""),                                 # 未记录数据源
    _rec(ts=(T0 - timedelta(days=3)).isoformat()),
    _rec(ts="不是时间"),                              # 解析不出来的时间
    _rec(thread_id=""),                              # 线程退回 trace_id
]

FILTERS = [
    AuditFilter(),
    AuditFilter(include_started=True),
    AuditFilter(since=T0 - timedelta(days=1)),
    AuditFilter(trace_id="abc123456789"),
    AuditFilter(username="amy"),
    AuditFilter(username=""),
    AuditFilter(kind="ask"),
    AuditFilter(kind="sql"),
    AuditFilter(source="s1"),
    AuditFilter(source=""),
    AuditFilter(status="ok"),
    AuditFilter(status="rejected"),
    AuditFilter(status="interrupted"),
    AuditFilter(thread_ids=("th-1",)),
    AuditFilter(thread_ids=()),
    AuditFilter(kind="ask", status="ok", username="amy"),
]


@pytest.mark.parametrize("f", FILTERS, ids=lambda f: str(f))
def test_sql_is_a_superset_of_matches(f):
    """_where 允许多带，不允许漏。

    漏一条的后果是页面上静默少一条记录 —— 而多带回来的那些会被
    read_audit / iter_audit 里那一遍 matches 挡掉，只是白跑一趟。
    """
    for rec in CORPUS:
        if matches(rec, f):
            assert _sql_keeps(rec, f), (
                f"SQL 把 matches 认可的记录筛掉了：filter={f} rec={rec}")


def test_old_records_without_kind_still_count_as_ask():
    """**没有 kind 字段**的老记录要能被「问答」筛中，两边都得中。

    这是唯一一处 Python 有兜底默认值（rec.get("kind","ask")）而列上没有的维度。
    入库时它落成空串，所以 SQL 必须认 kind IN ('ask','') —— 不认的话，
    筛「问答」会把所有历史记录静默漏掉。
    """
    old_rec = {k: v for k, v in _rec().items() if k != "kind"}
    f = AuditFilter(kind="ask")
    assert matches(old_rec, f)
    assert _sql_keeps(old_rec, f)


def test_empty_kind_counts_as_ask_on_both_sides():
    """kind 空串与"字段缺失"归同一档，两边一致。

    列上折算完只剩空串一档（写入是 str(rec.get("kind") or "")），SQL 再怎么写
    也分不出"键不在"与"键是空串"。所以 matches 也不去分 —— 分不出的差别不该
    在判定侧制造出来，否则审计列表（信 SQL）与统计（信 matches）会把同一条
    记录归成两档，而这种不一致在页面上只表现为"两个数对不上"。
    """
    f = AuditFilter(kind="ask")
    for rec in (_rec(kind=""), _rec(kind=None),
                {k: v for k, v in _rec().items() if k != "kind"}):
        assert matches(rec, f)
        assert _sql_keeps(rec, f)


def test_status_three_buckets_partition_the_corpus():
    """ok / rejected / interrupted 三档不重不漏 —— 每条记录恰好落一档。"""
    live = [r for r in CORPUS if matches(r, AuditFilter())]
    hit = {s: [r for r in live if matches(r, AuditFilter(status=s))]
           for s in ("ok", "rejected", "interrupted")}
    assert sum(len(v) for v in hit.values()) == len(live)
    assert hit["interrupted"] and hit["rejected"] and hit["ok"]


def test_unknown_status_matches_nothing_rather_than_everything():
    """认不出来的档要筛掉全部，不能当"不筛"放过去。

    放过去的表现是"筛选没生效"，而它实际是一道边界失效 —— 这两件事在
    页面上看起来一模一样，所以必须在这里钉死。
    """
    f = AuditFilter(status="不存在的档")
    assert not any(matches(r, f) for r in CORPUS)
    assert not any(_sql_keeps(r, f) for r in CORPUS)


def test_empty_thread_ids_means_none_not_all():
    """空元组是"一条线程都不要"，不是"不筛"。"""
    f = AuditFilter(thread_ids=())
    assert not any(matches(r, f) for r in CORPUS)
    assert not any(_sql_keeps(r, f) for r in CORPUS)


def test_open_codes_agree_between_the_two_modules():
    """两处收尾码常量必须同值。分叉了就是「中断」这一档在两个后端各筛各的。"""
    from askdb.audit import _OPEN_CODES

    assert set(_OPEN_CODES) == set(auditstore._OPEN_CODES_SQL)


def test_column_projection_matches_writer():
    """本文件 _cols 抄的那几行，必须与 append_audit 真正写进列里的一致。

    抄一份是为了不连库，代价是会漂。这里直接读源码钉住：append_audit 里
    每一列的取值表达式都还在，改了就得回来同步这份替身。
    """
    import inspect

    src = inspect.getsource(auditstore.append_audit)
    for expr in ('rec.get("trace_id")', 'rec.get("thread_id")', 'rec.get("phase")',
                 'rec.get("kind")', 'rec.get("user")', 'rec.get("source")',
                 'rec.get("rejected_by")', '_ts_of(rec)'):
        assert expr in src, f"append_audit 不再这样取 {expr}，_cols 需要同步"


# ---------------------------------------------------------------------------
# 写入路由 —— 2026-09-09 线上事故的回归守卫
#
# 那次改造把审计的**读**全部切到了 PostgreSQL，写却漏了 graph.py：五处
# 调用传的都是 cfg.audit_log（Path），于是 write_audit 一进门就走了文件分支，
# auditstore 那条路永远进不去。
#
# 症状极难定位：查询成功、页面出结果、日志无报错，只是审计中心、任务中心、
# 执行追踪三个页面同时看不到任何新记录 —— 看起来像"延迟很大"，实际是
# 写到 A、读的是 B。线上因此丢了整整一个部署周期的审计。
#
# 下面两条一条钉行为、一条钉调用点，缺一个都挡不住它重演。
# ---------------------------------------------------------------------------

def test_write_audit_routes_to_store_when_given_a_config(monkeypatch, tmp_path):
    """给 Config 就必须写库；给 Path 才写文件。**这是路由本身的契约。**"""
    from pathlib import Path

    from askdb import auditstore as store_mod
    from askdb import trace

    landed: list[dict] = []
    monkeypatch.setattr(store_mod, "enabled", lambda _cfg: True)
    monkeypatch.setattr(store_mod, "append_audit", lambda rec: landed.append(rec))

    class _Cfg:                       # 只需要 audit_log 这一个属性
        audit_log = tmp_path / "should-not-be-written.jsonl"

    trace.write_audit(_Cfg(), {"trace_id": "t1", "ts": "2026-09-09T00:00:00+00:00"})
    assert landed == [{"trace_id": "t1", "ts": "2026-09-09T00:00:00+00:00"}]
    assert not _Cfg.audit_log.exists(), "给了 Config 却把记录写进了文件"

    # 反向：Path 一律走文件，不碰库
    landed.clear()
    fpath = Path(tmp_path / "file.jsonl")
    trace.write_audit(fpath, {"trace_id": "t2", "ts": "2026-09-09T00:00:01+00:00"})
    assert landed == [], "给了 Path 却写进了库"
    assert "t2" in fpath.read_text(encoding="utf-8")


def test_graph_hands_write_audit_the_config_not_the_path():
    """graph.py 的每一处调用都必须传 cfg 本身。

    传 cfg.audit_log 不会报错、不会少写一条 —— 只会全部落到文件里，
    而读侧在库里什么也看不到。正因为没有任何失败信号，才需要在这里钉住。
    """
    import inspect

    from askdb import graph

    # 2026-09-12：固定管道删除后，写审计的调用点搬去了 agent / agentgraph。
    # 判据因此扫**所有写审计的模块**，钉的是"没有人再把 Path 传进去" ——
    # 而不是"某一个文件里必须有几处"（那个数字会随重构漂）。
    from askdb import agent, agentgraph

    src = "\n".join(inspect.getsource(m) for m in (graph, agent, agentgraph))
    assert "write_audit(cfg.audit_log" not in src, (
        "graph.py 又把 Path 传给了 write_audit —— 审计会写进文件而读侧读库，"
        "两边分裂且没有任何报错")
    assert src.count("write_audit(cfg,") >= 3, "write_audit 的调用点少了，确认是否漏改"
