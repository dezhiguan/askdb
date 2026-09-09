"""下推的每一句 SQL，真打到 PostgreSQL 上跑一遍。

与 test_audit_pushdown.py 的分工，别混为一谈：

  · 那边验的是**口径一致** —— _clauses 与 matches 对同一条记录给同一个答案。
    它在 Python 里模拟 WHERE，因此不连库也能跑，四处易错的折算都钉在那里。
  · 这边验的是**SQL 本身跑不跑得通**。嵌套 DISTINCT ON、ANY()、COALESCE、
    命名游标 —— 这几句写错的话，上面那份用例一条都不会红，因为它压根没执行
    过 SQL。而它们只在生产上才有机会出错。

所以两份都要，缺一边都会留下一整类看不见的失效。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from askdb import auditstore, pgstore
from askdb.audit import AuditFilter, matches

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)


def _rec(i: int, **kw):
    d = {
        "trace_id": f"tr{i:010d}", "thread_id": f"th-{i % 3}", "phase": "done",
        "kind": "ask", "user": "amy" if i % 2 else "", "source": f"s{i % 2}",
        "source_name": f"源{i % 2}", "rejected_by": "",
        "ts": (NOW - timedelta(hours=i)).isoformat(), "question": f"问题{i}",
    }
    d.update(kw)
    return d


#: 十五条覆盖各档的记录。写入顺序即 id 顺序，下面多处断言依赖这一点。
CORPUS = ([_rec(i) for i in range(10)]
          + [_rec(100, kind=""), _rec(101, rejected_by="R-02"),
             _rec(102, rejected_by="INTERRUPTED"), _rec(103, phase="started"),
             _rec(104, thread_id="")])


@pytest.fixture
def seeded(audit_store):
    for r in CORPUS:
        auditstore.append_audit(r)
    return audit_store


FILTERS = [
    AuditFilter(),
    AuditFilter(include_started=True),
    AuditFilter(kind="ask"),
    AuditFilter(kind="sql"),
    AuditFilter(status="ok"),
    AuditFilter(status="rejected"),
    AuditFilter(status="interrupted"),
    AuditFilter(username="amy"),
    AuditFilter(username=""),
    AuditFilter(source="s1"),
    AuditFilter(since=NOW - timedelta(hours=5)),
    AuditFilter(trace_id="tr0000000003"),
    AuditFilter(thread_ids=("th-1",)),
    AuditFilter(thread_ids=()),
]


@pytest.mark.parametrize("f", FILTERS, ids=lambda f: str(f))
def test_read_and_count_agree_with_matches(seeded, f):
    """库读回来的条数，必须等于 matches 在同一批记录上认可的条数。

    read_audit 与 count_audit 分开断言：前者会再过一遍 matches（因此天然对齐），
    后者**只信 SQL**（分页要求总数在库里就定死）。两个数不一样，就说明
    _clauses 与 matches 之间出现了 matches 更严的差异 —— 那正是分页会错位的信号。
    """
    want = sum(1 for r in CORPUS if matches(r, f))
    assert len(auditstore.read_audit(f)) == want
    assert auditstore.count_audit(f) == want


def test_pagination_reassembles_into_the_full_list_newest_first(seeded):
    """一页页取回来拼起来 == 全量倒序。不重、不漏、不错位。"""
    newest = [r["trace_id"] for r in auditstore.read_audit(AuditFilter())][::-1]
    paged: list[str] = []
    for off in range(0, len(newest) + 4, 4):
        paged += [r["trace_id"] for r in
                  auditstore.page_audit(AuditFilter(), offset=off, limit=4)]
    assert paged == newest


def test_facets_dedupe_in_sql_and_order_by_recency(seeded):
    """下拉取值在 SQL 里去重，且"最近用过的在前"。

    顺序不是讲究：文件后端就是这个顺序，两边对不上的话，同一个站点换个
    存储后端下拉里的排序就变了，而没有任何地方说得清为什么。
    """
    fc = auditstore.audit_facets(AuditFilter())
    assert [s["id"] for s in fc["sources"]] == ["s0", "s1"]
    assert {s["name"] for s in fc["sources"]} == {"源0", "源1"}
    # 最后写入的是 tr104（user=""），所以匿名那一档排在前面
    assert fc["users"][0] == ""
    assert set(fc["users"]) == {"", "amy"}


def test_recent_thread_ids_dedupes_and_honours_limit(seeded):
    """线程在库里定下来，按 max(id) 倒序，limit 真生效。"""
    want = {(r.get("thread_id") or r["trace_id"]) for r in CORPUS}
    tids = auditstore.recent_thread_ids(AuditFilter(include_started=True), limit=50)
    assert set(tids) == want
    # 最后一条记录 tr104 没有 thread_id，按 trace_id 自成一线程，且最新
    assert tids[0] == "tr0000000104"
    assert len(auditstore.recent_thread_ids(
        AuditFilter(include_started=True), limit=2)) == 2


def test_server_side_cursor_streams_the_same_rows(seeded):
    """iter_audit（服务端游标）与 read_audit 给出同一批、同一顺序的记录。"""
    assert ([r["trace_id"] for r in auditstore.iter_audit(AuditFilter())]
            == [r["trace_id"] for r in auditstore.read_audit(AuditFilter())])


def test_closing_the_cursor_early_returns_the_connection(seeded):
    """只取一条就关掉游标，连接必须回到池子里还能接着用。

    这条钉的是一个会拖垮整个服务的失效：池子只有 6 条连接，命名游标没关
    就一直占着。表现不是报错，是**并发上来之后所有库操作一起超时**。
    """
    gen = auditstore.iter_audit(AuditFilter())
    assert next(gen)["trace_id"]
    gen.close()
    # 连着来几次，把池子的连接数用一轮 —— 泄漏的话这里会卡住/超时
    for _ in range(8):
        assert auditstore.count_audit(AuditFilter()) == 14


def test_limit_takes_the_newest_but_returns_write_order(seeded):
    """limit=N 取最新 N 条，返回时仍按写入顺序（旧的在前）。

    两件事都要：取新是因为调用方没有一处想看最早那几条；顺序不变是因为
    不带 limit 时就是这个顺序，同一个函数不该因为多传一个参数换掉排序。
    """
    allrecs = [r["trace_id"] for r in auditstore.read_audit(AuditFilter())]
    assert ([r["trace_id"] for r in auditstore.read_audit(AuditFilter(), limit=3)]
            == allrecs[-3:])


def test_get_audit_by_trace_id_hits_one_row(seeded):
    """按 trace_id 下推只回一条 —— 这条路原来是把全表读回来逐条比。"""
    got = auditstore.read_audit(AuditFilter(trace_id="tr0000000007"), limit=1)
    assert [r["trace_id"] for r in got] == ["tr0000000007"]


def test_iter_rows_is_a_real_server_side_cursor(audit_store):
    """pgstore.iter_rows 分批取，且不整份物化。

    itersize 设成 2、灌 5 行，仍要完整拿到 5 行 —— 拿不全说明 DECLARE/FETCH
    这条路没走通（命名游标要在事务里 DECLARE，而池子开的是 autocommit）。
    """
    for i in range(5):
        auditstore.append_audit(_rec(200 + i))
    got = list(pgstore.iter_rows(
        "SELECT trace_id FROM askdb_audit ORDER BY id", batch_size=2))
    assert [t for (t,) in got] == [f"tr{200 + i:010d}" for i in range(5)]


def test_missing_table_reads_as_empty_not_an_error(audit_store):
    """表不存在按"还没有记录"处理 —— rows() 与 iter_rows() 同一条口径。"""
    with pgstore.connect() as con:
        con.execute("DROP TABLE IF EXISTS askdb_audit")
    assert pgstore.rows("SELECT 1 FROM askdb_audit") == []
    assert list(pgstore.iter_rows("SELECT 1 FROM askdb_audit")) == []
