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


def test_explicit_empty_kind_is_not_ask_and_sql_only_oversamples():
    """kind 显式为空串**不是**问答 —— 而 SQL 分不出它与"字段缺失"，会多带回来。

    这正是"超集契约"存在的理由：列是写入时折算出来的，丢掉了"这个键在不在"
    这一维。多带回来的那条由 read_audit / iter_audit 里的 matches 挡掉，
    页面上看不到它。这里把这个差异钉住，免得哪天有人看到 SQL 多筛出一条
    就去改 _where，反而把真正的老记录漏掉。
    """
    rec = _rec(kind="")
    f = AuditFilter(kind="ask")
    assert not matches(rec, f)          # 判定的出处
    assert _sql_keeps(rec, f)           # SQL 多带 —— 允许，且必须允许


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
