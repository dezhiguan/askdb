"""审计 / 审批 / 复核三份凭据的 PostgreSQL 存储。

为什么从文件搬进库（2026-09-09）
--------------------------------
原来三份都是共享 hostPath 上的 JSONL，靠 O_APPEND 单次 write 保证不撕行。
那套写法本身没错，但它成立的前提是"所有副本落在同一台机器的同一块本地盘"，
而集群里**没有任何调度约束在守这个前提**：加一个节点、副本被调度过去，
两个 Pod 就各写各的目录，审计、任务中心、配额一起分裂，还不报错。

除此之外文件方案有三处硬上限：
  · 只增不减，没有轮转与保留策略；
  · 每次 /api/audit、/api/tasks、/api/audit/stats 都把整份文件读进内存
    逐行解析 —— 磁盘撑爆之前先 OOM；

**换介质本身治不好最后那条。** 2026-09-09 首版把 read_audit 写成了
``SELECT record FROM askdb_audit ORDER BY id``：无 LIMIT、无时间窗，
再经 pgstore.rows 的 fetchall 全量缓冲，等于把同一个 OOM 从文件搬到了库上
（而且峰值更高——文件那版至少是逐行流式解析的）。同日补上下推：筛选条件
由 _where 翻成 SQL，分页 page_audit、计数 count_audit、下拉取值 audit_facets
各自只带回自己那点数据，必须整遍的场景走 iter_audit 的服务端游标。
  · 节点没了凭据就没了，而审计恰恰是出事后唯一的凭据。

**存储换了，语义一条不改**：仍然是 append-only 的事件流，状态仍然靠回放
推导（approvals / reviews 的 state()），读出来的仍然是原样那条 dict。
所以上层的统计、分页、任务聚合全部不动 —— 这一步只换介质，不换口径。

整条记录原样存进 record jsonb，另外把常用字段抽成列建索引：
抽出来的列是给检索与聚合用的，**取值一律以 record 为准**，避免出现
"列上说 A、原文说 B"这种对不上的凭据。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from . import pgstore
from .config import Config

log = logging.getLogger("askdb.auditstore")

AUDIT, APPROVALS, REVIEWS = "audit", "approvals", "reviews"

_DDL = """
CREATE TABLE IF NOT EXISTS askdb_audit (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL,
    trace_id    text NOT NULL,
    thread_id   text NOT NULL DEFAULT '',
    phase       text NOT NULL DEFAULT '',
    kind        text NOT NULL DEFAULT '',
    username    text NOT NULL DEFAULT '',
    role        text NOT NULL DEFAULT '',
    source      text NOT NULL DEFAULT '',
    rejected_by text NOT NULL DEFAULT '',
    model       text NOT NULL DEFAULT '',
    elapsed_ms  integer,
    tok_in      integer,
    tok_out     integer,
    cost_cny    numeric(14,6),
    record      jsonb NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS askdb_audit_ts_idx ON askdb_audit (ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS askdb_audit_trace_idx ON askdb_audit (trace_id);
CREATE INDEX IF NOT EXISTS askdb_audit_thread_idx ON askdb_audit (thread_id)
    WHERE thread_id <> '';
CREATE INDEX IF NOT EXISTS askdb_audit_user_idx ON askdb_audit (username)
    WHERE username <> '';

CREATE TABLE IF NOT EXISTS askdb_approvals (
    id          bigserial PRIMARY KEY,
    approval_id text NOT NULL,
    trace_id    text NOT NULL DEFAULT '',
    ts          timestamptz,
    record      jsonb NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS askdb_approvals_aid_idx ON askdb_approvals (approval_id);

CREATE TABLE IF NOT EXISTS askdb_reviews (
    id          bigserial PRIMARY KEY,
    review_id   text NOT NULL,
    trace_id    text NOT NULL DEFAULT '',
    ts          timestamptz,
    record      jsonb NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS askdb_reviews_rid_idx ON askdb_reviews (review_id);
"""

_ready: set[tuple[str, str]] = set()


def enabled(cfg: Config) -> bool:
    """这台实例的凭据是不是存在库里。

    两个条件都要成立：配置里选了 postgres，且环境变量给了连接串。
    **不做"配了 DSN 就自动启用"**：本机开发通常两者都在，但那时想要的
    往往还是文件（好 grep、好删）。介质是部署决定，不是环境凑出来的。
    """
    obs = cfg.raw.get("observability") or {}
    return str(obs.get("store", "file")).strip().lower() in ("postgres", "postgresql", "pg") \
        and pgstore.configured()


def ensure_schema() -> None:
    """建表建索引，幂等。每个进程按 (连接串, schema) 只跑一次。"""
    key = (pgstore.raw_dsn(), pgstore.schema())
    if key in _ready:
        return
    with pgstore.connect() as con:
        con.execute(_DDL)
    _ready.add(key)


def reset_ready() -> None:
    """忘掉"已建表"的记忆。换库的测试要用；生产用不到。"""
    _ready.clear()


def _ts_of(rec: dict[str, Any]) -> datetime:
    """记录自己的时间戳。解析不出来就用当下 —— 一条**没有时间的审计等于
    没有审计**，宁可标成此刻入库时间，也不要写一个 NULL 进去让它从所有
    按时间的查询里消失。"""
    raw = str(rec.get("ts") or "")
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return datetime.now().astimezone()


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def append_audit(rec: dict[str, Any]) -> None:
    """落一条审计。

    写失败**不抛**：查询已经跑完了，不能因为凭据库抖动把用户的结果吞掉——
    这条取舍与换库之前的文件写入完全一致（那边吞的是 OSError）。
    但要留一行 warning，否则"审计静默变少"没有任何痕迹。
    """
    from psycopg.types.json import Jsonb

    try:
        ensure_schema()
        pgstore.execute(
            "INSERT INTO askdb_audit (ts, trace_id, thread_id, phase, kind, username,"
            " role, source, rejected_by, model, elapsed_ms, tok_in, tok_out, cost_cny,"
            " record) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                _ts_of(rec),
                str(rec.get("trace_id") or ""),
                str(rec.get("thread_id") or ""),
                str(rec.get("phase") or ""),
                str(rec.get("kind") or ""),
                str(rec.get("user") or ""),
                str(rec.get("role") or ""),
                str(rec.get("source") or ""),
                str(rec.get("rejected_by") or ""),
                str(rec.get("model") or ""),
                _int(rec.get("elapsed_ms")),
                _int(rec.get("tok_in")),
                _int(rec.get("tok_out")),
                _num(rec.get("cost_cny")),
                Jsonb(json.loads(json.dumps(rec, ensure_ascii=False, default=str))),
            ),
        )
    except Exception as e:                             # noqa: BLE001 - 见 docstring
        log.warning("审计写入失败（记录已丢失）：%s", e)


#: 「现场还在检查点里」的两个收尾码。与 audit._OPEN_CODES 同一份取值 ——
#: 那边是判定的出处，这里只是把它翻成 SQL。改一处必须改两处，
#: tests/test_audit_pushdown.py 会在两者分叉时失败。
_OPEN_CODES_SQL = ("INTERRUPTED", "RESUME_BLOCKED")

#: 记录归属哪条线程。与 audit._thread_of 是同一条口径的两种写法。
_THREAD_EXPR = "COALESCE(NULLIF(thread_id, ''), trace_id)"


def _where(f: Any) -> tuple[str, list[Any]]:
    """_clauses 拼成一句 WHERE。取值口径见 _clauses。"""
    clauses, args = _clauses(f)
    return " AND ".join(clauses), args


def _clauses(f: Any) -> tuple[list[str], list[Any]]:
    """把 AuditFilter 翻成一组 WHERE 子句。

    **契约是「超集」，不是「相等」**：_where 允许多带回来一些，不允许漏。
    判定的出处始终是 audit.matches —— 抽出来的那些列是写入时从 record 折算的，
    折算口径与 Python 侧有两处对不齐（kind 为 null 时列上是空串；ts 解析不出来
    时列上填的是入库时刻），要求两边逐条相等就得把这些畸形记录的处理也复制一遍，
    那正是"列上说 A、原文说 B"的来源。

    所以分工是：SQL 负责把量降下来（这是性能），matches 负责下最终判断
    （这是口径）。read_audit / iter_audit 拿到结果后**再过一遍 matches**，
    因此聚合类调用与文件后端逐条一致。

    例外是 count_audit / page_audit / audit_facets：它们只信 SQL —— 分页要求
    「第 N 页有多少条」在库里就定死，事后再筛会让页与页之间错位。代价是
    写坏的记录在这三处与文件后端可能差一条，这是知情的取舍，不是疏忽。

    这里每一条都落在建了索引的列上（ts / trace_id / username），
    所以下推之后审计页不再是全表扫。

    trace_id <> '' 那条对应 Python 侧「没有 trace_id 的记录不算数」：
    列是 NOT NULL，但写入时取的是 str(rec.get("trace_id") or "")，空串进得来。
    """
    where: list[str] = ["trace_id <> ''"]
    args: list[Any] = []
    if not f.include_started:
        where.append("phase <> 'started'")
    if f.since is not None:
        where.append("ts >= %s")
        args.append(f.since)
    if f.trace_id is not None:
        where.append("trace_id = %s")
        args.append(f.trace_id)
    if f.username is not None:
        where.append("username = %s")
        args.append(f.username)
    if f.kind is not None:
        if f.kind == "ask":
            # 老记录没有 kind 字段，入库时落成空串，而 Python 侧按 "ask"
            # 兜底（rec.get("kind", "ask")）。不认这个空串，筛「问答」就会
            # 把所有历史记录漏掉 —— 而且是静默漏掉。
            where.append("kind IN ('ask', '')")
        else:
            where.append("kind = %s")
            args.append(f.kind)
    if f.source is not None:
        where.append("source = %s")
        args.append(f.source)
    if f.thread_ids is not None:
        # 与 audit._thread_of 对应：没有 thread_id 的老记录按 trace_id 自成一线程。
        # 空元组要真的筛掉全部，别让 ANY(空数组) 之外的分支把它当"不筛"。
        where.append(f"{_THREAD_EXPR} = ANY(%s)")
        args.append(list(f.thread_ids))
    if f.status is not None:
        if f.status == "ok":
            where.append("rejected_by = ''")
        elif f.status == "interrupted":
            where.append("rejected_by = ANY(%s)")
            args.append(list(_OPEN_CODES_SQL))
        elif f.status == "rejected":
            # 整条包起来：现在只靠 AND 的结合律才碰巧对，将来这串里插进
            # 任何一个 OR，"已拦截"这一档就会悄悄放行全部记录。
            where.append("(rejected_by <> '' AND NOT (rejected_by = ANY(%s)))")
            args.append(list(_OPEN_CODES_SQL))
        else:
            # 认不出来的档不能当"不筛"放过去 —— 那会把全部记录当成命中，
            # 页面上看起来像是筛选没生效，实际是边界失效。
            where.append("false")
    return where, args


def read_audit(f: Any = None, *, limit: int | None = None,
               include_started: bool = False) -> list[dict[str, Any]]:
    """读出过筛的审计记录，**保持写入顺序**（与文件版的"文件顺序"等价）。

    limit 取**最新的 N 条**：SQL 里按 id DESC 取，返回前再翻回来，
    所以调用方拿到的顺序与不带 limit 时一致（旧的在前）。
    """
    from .audit import AuditFilter

    ensure_schema()
    f = f if f is not None else AuditFilter(include_started=include_started)
    where, args = _where(f)
    if limit is None:
        sql = f"SELECT record FROM askdb_audit WHERE {where} ORDER BY id"
    else:
        sql = (f"SELECT record FROM askdb_audit WHERE {where}"
               f" ORDER BY id DESC LIMIT {int(limit)}")
    from .audit import matches

    out = [rec for (rec,) in pgstore.rows(sql, tuple(args))
           if isinstance(rec, dict) and matches(rec, f)]
    return out[::-1] if limit is not None else out


def iter_audit(f: Any = None) -> Any:
    """流式读出过筛的记录，按写入顺序。**结果不整份落进内存。**

    走服务端游标（pgstore.iter_rows）—— 生成器活着就占着一条池子连接，
    调用方必须消费完或及时 break。
    """
    from .audit import AuditFilter

    ensure_schema()
    f = f if f is not None else AuditFilter()
    where, args = _where(f)
    from .audit import matches

    sql = f"SELECT record FROM askdb_audit WHERE {where} ORDER BY id"
    for (rec,) in pgstore.iter_rows(sql, tuple(args)):
        if isinstance(rec, dict) and matches(rec, f):
            yield rec


def count_audit(f: Any = None) -> int:
    """过筛记录有多少条 —— 分页的 total 走这里，不靠把记录读回来数。"""
    from .audit import AuditFilter

    ensure_schema()
    f = f if f is not None else AuditFilter()
    where, args = _where(f)
    got = pgstore.rows(f"SELECT count(*) FROM askdb_audit WHERE {where}", tuple(args))
    return int(got[0][0]) if got else 0


def page_audit(f: Any = None, *, offset: int = 0, limit: int = 10) -> list[dict[str, Any]]:
    """取一页，**新的在前**（与审计页的展示顺序一致）。

    这一条是整轮下推的落点：页面要 10 条，就只有 10 条离开数据库。
    ts DESC, id DESC 那个索引正好吃这个排序。
    """
    from .audit import AuditFilter

    ensure_schema()
    f = f if f is not None else AuditFilter()
    where, args = _where(f)
    sql = (f"SELECT record FROM askdb_audit WHERE {where}"
           f" ORDER BY id DESC OFFSET {int(offset)} LIMIT {int(limit)}")
    return [rec for (rec,) in pgstore.rows(sql, tuple(args))
            if isinstance(rec, dict) and rec.get("trace_id")]


def recent_thread_ids(f: Any = None, *, limit: int = 2000) -> list[str]:
    """最近有过动静的 N 条线程 id，新的在前。

    任务中心要的是"最近这些线程"，而它此前的做法是把全部记录读回来、
    在内存里按 thread_id 聚合再排序 —— 线程只有几千条，记录却有几十万条。
    这一步先在库里把线程定下来，再只取这些线程的记录。

    排序按线程上**最后一条**记录（max(id)），不是第一条：任务中心那一页
    问的是"最近发生了什么"，一条老线程今天被续跑过就该排在前面。
    """
    from .audit import AuditFilter

    ensure_schema()
    f = f if f is not None else AuditFilter(include_started=True)
    where, args = _where(f)
    rows_ = pgstore.rows(
        f"SELECT t FROM (SELECT {_THREAD_EXPR} AS t, max(id) AS mx"
        f" FROM askdb_audit WHERE {where} GROUP BY 1"
        f" ORDER BY mx DESC LIMIT {int(limit)}) x ORDER BY mx DESC", tuple(args))
    return [t for (t,) in rows_ if t]


def audit_facets(f: Any = None) -> dict[str, list[Any]]:
    """筛选条上那两个下拉的取值：**可见范围内真出现过的**数据源与发起人。

    在 SQL 里 DISTINCT，而不是把记录读回来去重 —— 这两份名单的基数是几十，
    却曾经要求把几十万条记录读进内存才能算出来。

    数据源名取该 id **最近一条**记录里的写法（DISTINCT ON + id DESC），
    与文件版"从新往旧扫、第一次见到的那个名字"一致：源改过名之后，
    下拉里该显示新名字。
    """
    from .audit import AuditFilter

    ensure_schema()
    f = f if f is not None else AuditFilter()
    where, args = _where(f)
    # 两层排序各有职责，别合并：内层 DISTINCT ON 必须按 source 排（PostgreSQL
    # 的语法要求），外层再按 id DESC 排 —— 下拉的顺序是"最近用过的在前"，
    # 而不是按 id 字母序。文件后端也是这个顺序，两边必须一致。
    src_rows = pgstore.rows(
        f"SELECT source, name FROM ("
        f" SELECT DISTINCT ON (source) source, record->>'source_name' AS name, id"
        f" FROM askdb_audit WHERE {where} ORDER BY source, id DESC) t"
        f" ORDER BY id DESC", tuple(args))
    sources = [{"id": sid, "name": name or sid or "（未记录数据源）"}
               for sid, name in src_rows]
    user_rows = pgstore.rows(
        f"SELECT username FROM ("
        f" SELECT DISTINCT ON (username) username, id FROM askdb_audit"
        f" WHERE {where} ORDER BY username, id DESC) t"
        f" ORDER BY id DESC", tuple(args))
    return {"sources": sources, "users": [u for (u,) in user_rows]}


def append_approval(rec: dict[str, Any]) -> None:
    _append_event("askdb_approvals", "approval_id", rec)


def read_approvals() -> list[dict[str, Any]]:
    return _read_events("askdb_approvals")


def append_review(rec: dict[str, Any]) -> None:
    _append_event("askdb_reviews", "review_id", rec)


def read_reviews() -> list[dict[str, Any]]:
    return _read_events("askdb_reviews")


def _append_event(table: str, id_col: str, rec: dict[str, Any]) -> None:
    """审批与复核的事件写入。

    **这两条与审计相反：写失败要抛。** 审计是旁路（丢一条是损失，不是错误），
    而审批/复核的写入就是那个操作本身 —— 静默失败会让人以为批过了、复核过了，
    而流水里没有。调用方据此回 5xx，用户重试一次即可。
    """
    from psycopg.types.json import Jsonb

    ensure_schema()
    pgstore.execute(
        f"INSERT INTO {table} ({id_col}, trace_id, ts, record) VALUES (%s,%s,%s,%s)",
        (
            str(rec.get("id") or ""),
            str(rec.get("trace_id") or ""),
            _ts_of(rec) if rec.get("ts") else None,
            Jsonb(json.loads(json.dumps(rec, ensure_ascii=False, default=str))),
        ),
    )


def _read_events(table: str) -> list[dict[str, Any]]:
    ensure_schema()
    out = []
    for (rec,) in pgstore.rows(f"SELECT record FROM {table} ORDER BY id"):
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


# ==========================================================================
# 迁移：把既有 JSONL 灌进库
# ==========================================================================

def import_records(stream: str, records: list[dict[str, Any]]) -> dict[str, int]:
    """把一批记录导入对应的表，**已经在库里的跳过**。

    幂等靠"自然键"判重，不靠唯一索引：审计的自然键是
    (trace_id, ts, phase)，审批/复核是 (id, ts, status)。索引方案会在
    正常写入路径上多一处可能抛错的约束，而那条路径必须尽量不抛。

    重复跑一次这个命令是常态（迁移当天写入还在继续），所以它必须能反复跑。
    """
    ensure_schema()
    # 判重比的是**记录原文里的 ts 字符串**，不是入库后那个 timestamptz。
    # 两个原因：一是时区文本不同而瞬间相同（库里按会话时区回显 +08:00，
    # 原文可能写着 +00:00）；二是少数记录压根没有 ts，解析时会回落到"此刻"，
    # 每跑一次就得到一个新值 —— 那样它们每次迁移都会重复一条。
    if stream == AUDIT:
        seen = {(t, ts, p) for t, ts, p in pgstore.rows(
            "SELECT trace_id, coalesce(record->>'ts',''), phase FROM askdb_audit")}
        fresh = [r for r in records
                 if (str(r.get("trace_id") or ""), str(r.get("ts") or ""),
                     str(r.get("phase") or "")) not in seen]
        for rec in fresh:
            append_audit(rec)
        return {"total": len(records), "imported": len(fresh),
                "skipped": len(records) - len(fresh)}

    table, id_col = (("askdb_approvals", "approval_id") if stream == APPROVALS
                     else ("askdb_reviews", "review_id"))
    seen = {(i, ts, st) for i, ts, st in pgstore.rows(
        f"SELECT {id_col}, coalesce(record->>'ts',''),"
        f" coalesce(record->>'status','') FROM {table}")}
    fresh = [r for r in records
             if (str(r.get("id") or ""), str(r.get("ts") or ""),
                 str(r.get("status") or "")) not in seen]
    for rec in fresh:
        _append_event(table, id_col, rec)
    return {"total": len(records), "imported": len(fresh),
            "skipped": len(records) - len(fresh)}


def counts() -> dict[str, int]:
    """三张表各有多少行 —— 迁移前后对账用。"""
    ensure_schema()
    out = {}
    for stream, table in ((AUDIT, "askdb_audit"), (APPROVALS, "askdb_approvals"),
                          (REVIEWS, "askdb_reviews")):
        rows = pgstore.rows(f"SELECT count(*) FROM {table}")
        out[stream] = int(rows[0][0]) if rows else 0
    return out
