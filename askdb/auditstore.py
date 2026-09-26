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

AUDIT, APPROVALS, REVIEWS, OPS = "audit", "approvals", "reviews", "ops"

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

CREATE TABLE IF NOT EXISTS askdb_ops (
    id          bigserial PRIMARY KEY,
    ops_id      text NOT NULL,
    trace_id    text NOT NULL DEFAULT '',
    ts          timestamptz,
    record      jsonb NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS askdb_ops_oid_idx ON askdb_ops (ops_id);
"""

#: 2026-09-15 补的「折算列」。
#:
#: 任务中心与统计此前把整条 record jsonb 拉回 Python 才能折算状态、风险、
#: 长短任务与成本 —— 一次请求几千条、每条几十 KB，而屏幕上只有十行。
#: 要把筛选、计数、分页真正下推到库里，这几样判据必须是**列**：
#: jsonb 里的字段虽然也能在 SQL 里取（record->>'x'），但那会把每一行的
#: jsonb 都解压一遍，等于换了个地方付同样的钱。
#:
#: 与既有那几列同一条纪律：**抽出来的列只用于检索与聚合，取值一律以 record
#: 为准**。所以这些列全部由 columns_of() 从记录本身算出来，入库与回填共用
#: 同一个函数；folded SQL 只读列，不再碰 record。
#:
#: derived_v 是回填进度的哨兵：为空表示这一行还是老格式，_backfill 按它找活干。
_ALTER = """
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS question        text;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS source_name     text;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS recall_blind    boolean;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS mask_degraded   boolean;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS attempts        integer;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS converged_early text;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS explain_rows    bigint;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS rows_returned   integer;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS multi_step      boolean;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS model_calls     integer;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS model_failed    integer;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS has_steps       boolean;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS model_agg       jsonb;
ALTER TABLE askdb_audit ADD COLUMN IF NOT EXISTS derived_v       integer;

-- 任务中心按「线程」聚合，这个表达式就是分组键（与 audit._thread_of 同一口径）。
CREATE INDEX IF NOT EXISTS askdb_audit_thread_expr_idx
    ON askdb_audit ((COALESCE(NULLIF(thread_id, ''), trace_id)), id);
-- 回填要反复找"还没折算的那些"。回填跑完这个索引就是空的，代价趋近于零。
CREATE INDEX IF NOT EXISTS askdb_audit_underived_idx
    ON askdb_audit (id) WHERE derived_v IS NULL;

CREATE TABLE IF NOT EXISTS askdb_migrations (
    name     text PRIMARY KEY,
    done_at  timestamptz NOT NULL DEFAULT now()
);
"""

#: 关键词检索走这个索引。**建不出来不算失败**：pg_trgm 要 CREATE EXTENSION
#: 权限，而生产上 askdb 连的是别人的库、未必给得到（pgvector 当初就是让
#: 部署方先手工建的）。没有它 question 上是一次窄列顺扫 —— 比把几千条
#: jsonb 拉回 Python 仍然快一个量级，所以不值得为它把服务拦住。
#:
#: 另外：**这个索引只对三个字以上的关键词有用**。trigram 顾名思义按三字切分，
#: 更短的词用不上索引，照样顺扫。中文两字词很常见，所以别指望它包治百病。
_TRGM_INDEX = ("CREATE INDEX IF NOT EXISTS askdb_audit_question_trgm_idx"
               " ON askdb_audit USING gin (question {ns}.gin_trgm_ops)")


def _ensure_trgm(con: Any) -> None:
    """尽力建出关键词索引，建不出来就记一行 info 走人。

    **操作符类必须按 schema 限定**：pg_trgm 可能早就装在 public 里，而本连接的
    search_path 只有业务 schema —— 那时 CREATE EXTENSION IF NOT EXISTS 什么都
    不做（扩展确实已存在），紧接着的 gin_trgm_ops 却找不到，整句失败。
    本机实测踩到过一次，表现是索引悄悄没建、而日志只有一行"未建"。
    """
    try:
        con.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as e:                             # noqa: BLE001 - 见 docstring
        log.info("pg_trgm 扩展装不上（关键词检索改走顺扫）：%s", e)
    try:
        got = con.execute(
            "SELECT n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace"
            " WHERE e.extname = 'pg_trgm'").fetchone()
        if not got:
            log.info("没有 pg_trgm，关键词检索走顺扫")
            return
        con.execute(_TRGM_INDEX.format(ns=str(got[0])))
    except Exception as e:                             # noqa: BLE001
        log.info("pg_trgm 索引未建（关键词检索改走顺扫）：%s", e)


_ready: set[tuple[str, str]] = set()
_derived_ready: set[tuple[str, str]] = set()


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
    """建表建索引、补列、回填折算列，幂等。每个进程按 (连接串, schema) 只跑一次。"""
    key = (pgstore.raw_dsn(), pgstore.schema())
    if key in _ready:
        return
    with pgstore.connect() as con:
        con.execute(_DDL)
        con.execute(_ALTER)
        _ensure_trgm(con)
    _ready.add(key)


def ensure_derived() -> None:
    """折算列**确实可用**之后才返回。读任务中心、统计、关键词之前必须调。

    与 ensure_schema 分开是有意的：**写入不该等回填**。append_audit 自己就把
    折算列写全了，它一行历史数据都不需要；而回填在几十万行的表上可能跑上
    几分钟，让写入排在它后面，等于那几分钟里每一次问答的收尾都卡住。

    读则必须等：折算 SQL 只读列不读 record，列是空的行会被算成"没有任何风险
    痕迹的已完成任务" —— 静默算错，不是报错。
    """
    key = (pgstore.raw_dsn(), pgstore.schema())
    if key in _derived_ready:
        return
    ensure_schema()
    _backfill()
    _derived_ready.add(key)


#: 回填一批多少行。批小一点，单条 UPDATE 的事务就短，不会把一张在写的表
#: 锁着不放；批大一点往返次数少。两千是按"一批一秒以内跑完"定的。
_BACKFILL_BATCH = 2000

#: 回填在库上的记号。**记在库里而不是进程内存里**：四个副本各跑一次回填
#: 是纯浪费，而且第二个副本会在第一个还没跑完时看到半成品。
_BACKFILL_NAME = "audit_derived_cols_v1"

#: 回填用的建议锁键（任取的常数）。同一时刻只让一个副本干活，其余的等它。
_BACKFILL_LOCK = 0x4153_4B44


#: 等别的副本把回填跑完，最多等多久。等不到**报错而不是照常渲染** ——
#: 没回填完就去折算，给出的是一份看起来正常的错数据。
_BACKFILL_WAIT_S = 120


def _backfill() -> None:
    """把老记录的折算列补上 —— **读之前必须完成**。

    为什么不肯让它异步或者可选：折算 SQL 只读列不读 record（那正是这轮改造
    的全部收益）。列是空的行会被折算成"没有风险痕迹的已完成任务"，也就是
    **静默算错**，而不是报错。这个仓库反复踩的就是这一类。

    所以取舍是：宁可第一次打开这几页慢几秒，也不要给出一份看起来正常的错
    数据。跑失败一律抛出去 —— 让页面报"凭据库不可用"，而不是照常渲染。

    多副本靠建议锁挑一个干活，**其余的轮询等，不阻塞在锁上**：阻塞式
    pg_advisory_lock 会把一条池子连接一直占着（池子只有 6 条），而且等多久
    完全取决于表有多大；轮询则是每两秒问一次"记号写上了没"，等过头就抛
    StoreUnavailable，页面上是一句能看懂的话，而不是一个转不完的圈。
    """
    import os
    import time

    if str(os.environ.get("ASKDB_STORE_SKIP_BACKFILL") or "").strip() in ("1", "true"):
        log.warning("按 ASKDB_STORE_SKIP_BACKFILL 跳过折算列回填 —— "
                    "老记录在任务中心与统计上会被算错，只应在迁移演练时用")
        return
    deadline = time.monotonic() + _BACKFILL_WAIT_S
    while True:
        if _backfilled():
            return
        if _try_backfill():
            return
        if time.monotonic() >= deadline:
            raise pgstore.StoreUnavailable(
                f"折算列回填还没跑完（等了 {_BACKFILL_WAIT_S} 秒）。"
                f"另一个副本正在补历史记录，稍后重试；"
                f"进度看 SELECT count(*) FROM askdb_audit WHERE derived_v IS NULL")
        time.sleep(2)


def _backfilled() -> bool:
    """回填跑完了没有。记号记在库里 —— 四个副本各跑一次是纯浪费，
    而且第二个副本会在第一个还没跑完时看到半成品。"""
    return bool(pgstore.rows("SELECT 1 FROM askdb_migrations WHERE name = %s",
                             (_BACKFILL_NAME,)))


def _try_backfill() -> bool:
    """抢到建议锁就把活干完，返回 True；没抢到返回 False（让调用方去等）。"""
    with pgstore.connect() as con:
        got = con.execute("SELECT pg_try_advisory_lock(%s)",
                          (_BACKFILL_LOCK,)).fetchone()
        if not (got and got[0]):
            return False
        try:
            if con.execute("SELECT 1 FROM askdb_migrations WHERE name = %s",
                           (_BACKFILL_NAME,)).fetchone():
                return True                            # 等锁期间别人已经跑完了
            total = 0
            names = list(columns_of({}))
            update = ("UPDATE askdb_audit SET "
                      + ", ".join(f"{n} = %s" for n in names) + " WHERE id = %s")
            while True:
                batch = con.execute(
                    "SELECT id, record FROM askdb_audit WHERE derived_v IS NULL"
                    f" ORDER BY id LIMIT {_BACKFILL_BATCH}").fetchall()
                if not batch:
                    break
                # executemany 走管线，一批只有一次往返。逐条 execute 在几十万行
                # 的表上就是几十万次往返 —— 回填本身会变成那次部署的停机时间。
                with con.cursor() as cur:
                    cur.executemany(update, [
                        tuple(columns_of(rec if isinstance(rec, dict) else {})[n]
                              for n in names) + (rid,)
                        for rid, rec in batch])
                total += len(batch)
                log.info("折算列回填中：已处理 %d 行", total)
            con.execute("INSERT INTO askdb_migrations (name) VALUES (%s)"
                        " ON CONFLICT DO NOTHING", (_BACKFILL_NAME,))
            if total:
                log.warning("折算列回填完成：%d 行", total)
            return True
        finally:
            con.execute("SELECT pg_advisory_unlock(%s)", (_BACKFILL_LOCK,))


def reset_ready() -> None:
    """忘掉"已建表"与"已回填"的记忆。换库的测试要用；生产用不到。"""
    _ready.clear()
    _derived_ready.clear()


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


def _strict_int(v: Any) -> int | None:
    """只认真正的整数。**bool 不算** —— Python 里 True 是 int 的子类，
    而 audit._risk 用 isinstance(x, int) 判这两个字段，这里必须给出同一个答案。
    字符串形态的数字也不认：那一维在记录里本来就是数字，认了反而把
    "写坏的记录"悄悄折算成一个像样的值。"""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def columns_of(rec: dict[str, Any]) -> dict[str, Any]:
    """把一条记录折算成入库时那些**列** —— 检索、聚合、分页全靠它们。

    **入库与回填共用这一个函数**，所以老记录补出来的列与新写入的逐字一致。
    取值一律以 record 为准（与模块开头那条纪律一致）：这里只做类型收口，
    不做任何语义判断 —— 判断留在 SQL 的折算表达式与 audit.py 的纯函数里，
    两边由 tests/test_tasks_pushdown.py 对着跑。
    """
    from psycopg.types.json import Jsonb

    from .audit import model_contrib, step_counters

    model_calls, model_failed, has_steps = step_counters(rec)
    return {
        "question": str(rec.get("question") or ""),
        "source_name": str(rec.get("source_name") or ""),
        "recall_blind": bool(rec.get("recall_blind")),
        "mask_degraded": bool(rec.get("mask_degraded")),
        "attempts": _int(rec.get("attempts")),
        "converged_early": str(rec.get("converged_early") or ""),
        "explain_rows": _strict_int(rec.get("explain_rows")),
        "rows_returned": _strict_int(rec.get("rows_returned")),
        "multi_step": bool(rec.get("multi_step")),
        "model_calls": model_calls,
        "model_failed": model_failed,
        "has_steps": has_steps,
        "model_agg": Jsonb(model_contrib(rec)),
        "derived_v": 1,
    }


def append_audit(rec: dict[str, Any]) -> None:
    """落一条审计。

    写失败**不抛**：查询已经跑完了，不能因为凭据库抖动把用户的结果吞掉——
    这条取舍与换库之前的文件写入完全一致（那边吞的是 OSError）。
    但要留一行 warning，否则"审计静默变少"没有任何痕迹。
    """
    from psycopg.types.json import Jsonb

    try:
        ensure_schema()
        base = {
            "ts": _ts_of(rec),
            "trace_id": str(rec.get("trace_id") or ""),
            "thread_id": str(rec.get("thread_id") or ""),
            "phase": str(rec.get("phase") or ""),
            "kind": str(rec.get("kind") or ""),
            "username": str(rec.get("user") or ""),
            "role": str(rec.get("role") or ""),
            "source": str(rec.get("source") or ""),
            "rejected_by": str(rec.get("rejected_by") or ""),
            "model": str(rec.get("model") or ""),
            "elapsed_ms": _int(rec.get("elapsed_ms")),
            "tok_in": _int(rec.get("tok_in")),
            "tok_out": _int(rec.get("tok_out")),
            "cost_cny": _num(rec.get("cost_cny")),
            "record": Jsonb(json.loads(json.dumps(rec, ensure_ascii=False, default=str))),
        }
        cols = {**base, **columns_of(rec)}
        names = list(cols)
        pgstore.execute(
            f"INSERT INTO askdb_audit ({', '.join(names)})"
            f" VALUES ({', '.join(['%s'] * len(names))})",
            tuple(cols[n] for n in names))
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

    **契约是「超集」，不是「相等」**：_clauses 允许多带回来一些，不允许漏。
    判定的出处始终是 audit.matches。

    绝大多数维度上两边是逐条相等的 —— kind 那一处本来对不齐（列上分不出
    "键不在"与"键是空串"），2026-09-09 的修法是让 matches 也不去分，
    而不是让 SQL 去猜。**分不出的差别不该在判定侧制造出来。**

    留下的差异只剩一处：ts 解析不出来的记录，列上填的是入库时刻，SQL 因此
    可能把它算进时间窗而 matches 不算。这是写坏的记录才会有的形状，
    为它把畸形处理复制到 SQL 里，换来的是"列上说 A、原文说 B"，不划算。

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
    if f.q:
        # 关键词下推。**大小写靠 ILIKE，不是靠把列转小写** —— 后者用不上
        # trgm 索引，而这一维恰恰是最需要索引的那个。
        #
        # 与 audit._q_hit 的差别只有一处，是知情的：Python 比的是
        # str.lower() 的子串，ILIKE 比的是数据库 collation 下的大小写折叠。
        # 命中面只在"某些 Unicode 字母的花式大小写"上有极小出入，而这一维
        # 的实际用法是 trace_id（纯 ASCII）与中文问题原文（无大小写）。
        like = f"%{_like_escape(f.q)}%"
        cols = ["trace_id"]
        if f.q_text:
            # 问题原文与发起人同属"内容"，跟着 q_text 一起放开或一起收 ——
            # 只抹显示、仍允许按它搜，等于留了一个预言机。
            cols += ["COALESCE(question, '')", "username"]
        where.append("(" + " OR ".join(f"{c} ILIKE %s ESCAPE '!'" for c in cols) + ")")
        args.extend([like] * len(cols))
    return where, args


def _ensure_for(f: Any) -> None:
    """按这次筛选**真正会用到的列**决定要不要等回填。

    只有关键词那一维打在 question 列上（回填补的正是它）；其余维度全落在
    2026-09-09 就有的老列上，没必要为它们排在一次几分钟的回填后面。
    """
    if getattr(f, "q", None):
        ensure_derived()
    else:
        ensure_schema()


def _like_escape(text: str) -> str:
    """把 LIKE 的三个元字符转义掉。**必须做**：搜一个 100% 会被读成
    "任意字符"，于是搜什么都能搜到 —— 一个静默给出错结果的筛选框。
    转义符用 ! 而不是反斜杠，免得再被字符串字面量吃掉一层。"""
    out = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return out


def read_audit(f: Any = None, *, limit: int | None = None,
               include_started: bool = False) -> list[dict[str, Any]]:
    """读出过筛的审计记录，**保持写入顺序**（与文件版的"文件顺序"等价）。

    limit 取**最新的 N 条**：SQL 里按 id DESC 取，返回前再翻回来，
    所以调用方拿到的顺序与不带 limit 时一致（旧的在前）。
    """
    from .audit import AuditFilter

    f = f if f is not None else AuditFilter(include_started=include_started)
    _ensure_for(f)
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

    f = f if f is not None else AuditFilter()
    _ensure_for(f)
    where, args = _where(f)
    from .audit import matches

    sql = f"SELECT record FROM askdb_audit WHERE {where} ORDER BY id"
    for (rec,) in pgstore.iter_rows(sql, tuple(args)):
        if isinstance(rec, dict) and matches(rec, f):
            yield rec


def count_audit(f: Any = None) -> int:
    """过筛记录有多少条 —— 分页的 total 走这里，不靠把记录读回来数。"""
    from .audit import AuditFilter

    f = f if f is not None else AuditFilter()
    _ensure_for(f)
    where, args = _where(f)
    got = pgstore.rows(f"SELECT count(*) FROM askdb_audit WHERE {where}", tuple(args))
    return int(got[0][0]) if got else 0


def page_audit(f: Any = None, *, offset: int = 0, limit: int = 10) -> list[dict[str, Any]]:
    """取一页，**新的在前**（与审计页的展示顺序一致）。

    这一条是整轮下推的落点：页面要 10 条，就只有 10 条离开数据库。
    ts DESC, id DESC 那个索引正好吃这个排序。
    """
    from .audit import AuditFilter

    f = f if f is not None else AuditFilter()
    _ensure_for(f)
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

    f = f if f is not None else AuditFilter()
    _ensure_for(f)
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


# ===========================================================================
# 统计：**聚合在库里做**（2026-09-15）
#
# /api/audit/stats 此前把时间窗内的每一条 record 整条拉回 Python，只为了累加
# 七八个计数器。生产实测 days=30 要 1.1 秒、days=1 只要 0.23 秒 —— 完美线性，
# 也就是说这一页的代价与"这个月发生过多少事"成正比，而它显示的永远是同样
# 那十几个数字。审计页与执行追踪页的首屏就卡在它身上。
#
# 现在每个维度各一条聚合 SQL，全部落在列上，jsonb 只在「按模型」那一维被
# 碰一次（model_agg 是入库时算好的小对象，不是整条 record）。
# ===========================================================================


def stats_totals(f: Any) -> tuple[Any, ...]:
    """窗口内的几个总数：调用、拦截、有节点链、成本、token、模型调用与失败。"""
    ensure_derived()
    where, args = _where(f)
    got = pgstore.rows(
        f"""SELECT count(*), count(*) FILTER (WHERE rejected_by <> ''),
                   count(*) FILTER (WHERE has_steps),
                   COALESCE(sum(cost_cny), 0), COALESCE(sum(tok_in), 0),
                   COALESCE(sum(tok_out), 0), COALESCE(sum(model_calls), 0),
                   COALESCE(sum(model_failed), 0)
              FROM askdb_audit WHERE {where}""", tuple(args))
    return got[0] if got else (0, 0, 0, 0, 0, 0, 0, 0)


def elapsed_at(f: Any, offset: int) -> int | None:
    """按耗时升序排在第 offset 位的那条（0 起算）。

    分位数**故意不用 percentile_cont / percentile_disc**：audit._percentile 用的是
    最近秩法 round((n-1)*q)，而 Python 的 round 是银行家舍入、PostgreSQL 的是
    四舍五入 —— 样本数为偶数时 P50 会差出一个位置。名次在 Python 里算好、
    这里只按名次取值，两边就不可能分叉。

    耗时缺失按 0 计，与 Python 侧 int(r.get("elapsed_ms") or 0) 一致：
    那也是"这次调用有多慢"的一个真实答案（没记下来），不是缺席。
    """
    ensure_derived()
    where, args = _where(f)
    got = pgstore.rows(
        f"SELECT COALESCE(elapsed_ms, 0) FROM askdb_audit WHERE {where}"
        f" ORDER BY 1 OFFSET %s LIMIT 1", tuple(args + [offset]))
    return int(got[0][0]) if got else None


def stats_daily(f: Any, tz_offset: Any) -> list[tuple[Any, ...]]:
    """按日分桶的调用数与成本。**日界按声明时区算**，不跟着容器时钟走。

    tz_offset 是一个 timedelta：生产容器的时钟是 UTC，不折算回来的话
    「今日查询」要到北京时间早上八点才翻页。
    """
    ensure_derived()
    where, args = _where(f)
    return pgstore.rows(
        f"""SELECT (ts AT TIME ZONE %s)::date AS d, count(*),
                   COALESCE(sum(cost_cny), 0)
              FROM askdb_audit WHERE {where} GROUP BY d ORDER BY d""",
        tuple([tz_offset] + args))


def stats_by_kind(f: Any) -> list[tuple[Any, ...]]:
    """按调用类型。空串折成 ask —— 老记录没有 kind 字段，入库落的是空串，
    而 Python 侧兜底成 ask（rec.get("kind", "ask")）。不折的话，改造前后
    同一批记录会分到两个不同的桶里。"""
    ensure_derived()
    where, args = _where(f)
    return pgstore.rows(
        f"SELECT COALESCE(NULLIF(kind, ''), 'ask'), count(*) FROM askdb_audit"
        f" WHERE {where} GROUP BY 1", tuple(args))


def stats_by_rule(f: Any) -> list[tuple[Any, ...]]:
    """按拦截规则，多的在前。"""
    ensure_derived()
    where, args = _where(f)
    return pgstore.rows(
        f"SELECT rejected_by, count(*) FROM askdb_audit WHERE {where}"
        f" AND rejected_by <> '' GROUP BY 1 ORDER BY 2 DESC, 1", tuple(args))


def stats_by_model(f: Any) -> list[tuple[Any, ...]]:
    """按模型的调用次数与成本，贵的在前。

    合并的是入库时算好的 model_agg（audit.model_contrib 的产物），不是现场
    去 steps 里数 —— 归因规则只有那一份，这里只做加法。金额用 numeric 累加，
    因此「各行之和 = 总额」这条对账关系是精确成立的。

    where 里的列名不加表别名：jsonb_each 那侧只有 key / value 两列，不会撞名。
    """
    ensure_derived()
    where, args = _where(f)
    return pgstore.rows(
        f"""SELECT m.key, sum((m.value->>'calls')::int),
                   sum((m.value->>'cost_cny')::numeric)
              FROM askdb_audit a, LATERAL jsonb_each(a.model_agg) AS m
             WHERE {where} AND a.model_agg IS NOT NULL
             GROUP BY 1 ORDER BY 3 DESC, 1""", tuple(args))


# ===========================================================================
# 任务中心：**线程分页在库里做**（2026-09-15）
#
# 在这之前这一页是这样的：SQL 只负责挑出"最近 2000 条线程"，然后把这些线程
# 的**全部记录整条**拉回 Python，在内存里聚合成两千多个任务、算状态、算风险、
# 排序，最后切出十条发给浏览器。生产实测一次请求 1.65 秒，且与页码无关 ——
# 翻到第 3 页照样把两千条重算一遍。一条记录几十 KB（span 全量输入输出都在
# record 里），所以真正的开销不是"条数多"，而是每次都要把几十 MB 的 jsonb
# 解压、传输、反序列化一遍。
#
# 现在：聚合、折算、筛选、计数、分面、切页全部在一条 SQL 里完成，出库的只有
# 当前这一页的十个线程 id + 十条 record。**这是真分页**，翻页的代价与总量无关。
#
# 代价是一处知情的重复：状态 / 风险 / 长短任务这三档折算，audit.py 里有一份
# 纯函数（文件后端、单元测试、任务详情都用它），这里有一份等价的 SQL 表达式。
# 与 _clauses / matches 那一对是同一种安排，也由同一种手段守住 ——
# tests/test_tasks_pushdown.py 把同一批记录喂给两边，逐条比对三个折算值。
# **改任何一边都要改另一边**，否则表现不是报错，而是页面上的状态悄悄不对。
# ===========================================================================


def _lit(value: str) -> str:
    """把一个状态常量写成 SQL 字面量。**取值来自 audit 的常量，不在这里重打一遍**
    —— 状态码拼错在 SQL 里不会报错，只会让某一档永远筛不出东西。"""
    if "'" in value or "\\" in value:                  # pragma: no cover - 常量里不会有
        raise ValueError(f"状态常量不该含引号：{value!r}")
    return f"'{value}'"


def _pairs(mapping: dict[str, str]) -> list[list[str]]:
    """{键: 值} 拆成两个平行数组，好用 unnest(%s::text[], %s::text[]) 接进 SQL。

    三份结论（审批 / 复核 / 运维）各自存在别的表里，审计不认识它们、也不该
    认识（见 server._task_context）。传数组进来是让"不认识"这件事保持成立
    的同时，仍然能在库里一次折算完 —— 而不是把三张表 JOIN 进审计的查询里。
    """
    keys = list(mapping)
    return [keys, [str(mapping[k] or "") for k in keys]]


def _fold_sql(fold: Any) -> tuple[str, list[Any]]:
    """三档折算的 SQL 表达式（风险、长短任务、状态），以及它们的参数。

    与 audit._risk / audit.task_kind / audit.stage 逐分支对应，顺序也一样 ——
    对照着读才看得出有没有漏一档。
    """
    from .audit import (CANCELED, DONE, INTERRUPTED, LONG_TASK, NEEDS_OPERATOR, REJECTED,
                        REVIEW_RETURNED, RUNNING, SHORT_TASK, WAITING_APPROVAL,
                        WAITING_INPUT, WAITING_REVIEW, _INPUT_CODES,
                        _OPEN_CODES, _OPS_CODES, _RISK_HIGH, _RISK_MEDIUM)

    args: list[Any] = []

    risk = f"""CASE
        WHEN t.rejected_by = ANY(%s) THEN 'HIGH'
        WHEN t.rejected_by = ANY(%s) THEN 'MEDIUM'
        WHEN t.rejected_by <> '' THEN 'LOW'
        WHEN t.explain_rows IS NOT NULL AND %s > 0 AND t.explain_rows >= %s THEN 'MEDIUM'
        WHEN t.rows_returned IS NOT NULL AND %s > 0 AND t.rows_returned >= %s THEN 'MEDIUM'
        WHEN t.multi_step THEN 'MEDIUM'
        ELSE 'LOW' END"""
    # 阈值折半用 Python 的整除算好再传，别在 SQL 里写 / 2 —— 那是浮点除法，
    # 与 audit._risk 的 max_scan_rows // 2 在奇数上差一。
    args += [sorted(_RISK_HIGH), sorted(_RISK_MEDIUM),
             fold.max_scan_rows, fold.max_scan_rows // 2,
             fold.max_rows, fold.max_rows]

    kind = f"""CASE
        WHEN %s <= 0 THEN {_lit(SHORT_TASK)}
        WHEN t.phase = 'started' THEN
            CASE WHEN EXTRACT(EPOCH FROM (%s::timestamptz - t.ts)) * 1000 >= %s
                 THEN {_lit(LONG_TASK)} ELSE {_lit(SHORT_TASK)} END
        WHEN t.elapsed_ms IS NOT NULL AND t.elapsed_ms > 0 THEN
            CASE WHEN t.elapsed_ms >= %s THEN {_lit(LONG_TASK)} ELSE {_lit(SHORT_TASK)} END
        ELSE {_lit(SHORT_TASK)} END"""
    args += [fold.async_after_ms, fold.now, fold.async_after_ms, fold.async_after_ms]

    stage = f"""CASE
        WHEN t.phase = 'started' THEN
            CASE WHEN t.stale THEN {_lit(INTERRUPTED)} ELSE {_lit(RUNNING)} END
        WHEN t.rejected_by = 'CANCELED' THEN {_lit(CANCELED)}
        WHEN t.rejected_by = ANY(%s) THEN {_lit(INTERRUPTED)}
        WHEN t.rejected_by = '' THEN
            CASE WHEN t.review_status = 'RETURNED' THEN {_lit(REVIEW_RETURNED)}
                 WHEN t.review_status = 'ACCEPTED' THEN {_lit(DONE)}
                 WHEN t.needs_review THEN {_lit(WAITING_REVIEW)}
                 ELSE {_lit(DONE)} END
        WHEN t.rejected_by = ANY(%s) THEN
            CASE WHEN t.ops_status <> '' THEN {_lit(REJECTED)} ELSE {_lit(NEEDS_OPERATOR)} END
        WHEN t.rejected_by = ANY(%s) THEN {_lit(WAITING_INPUT)}
        WHEN t.approval_status IN ('REQUESTED', 'APPROVED') THEN {_lit(WAITING_APPROVAL)}
        ELSE {_lit(REJECTED)} END"""
    args += [sorted(_OPEN_CODES), sorted(_OPS_CODES), sorted(_INPUT_CODES)]

    # 陈旧线程由服务端按检查点核实之后回传（override）—— 检查点在另一个库里，
    # SQL 看不见它。见 audit.tasks_page 的说明。
    sql = (f"SELECT t.*, {risk} AS risk, {kind} AS task_kind,"
           f" CASE WHEN t.override_status <> '' THEN t.override_status"
           f" ELSE {stage} END AS status FROM thr t")
    return sql, args


def _thread_cte(f: Any, fold: Any, owner: str | None) -> tuple[str, list[Any]]:
    """线程聚合的 CTE：一条线程一行，首末两条记录的判据都在这一行上。

    分四层，每层只做一件事：
      scoped  —— 过筛之后的记录（只取会用到的窄列，**绝不碰 record**）
      live    —— 去掉已被收尾记录取代的发起占位（与 audit.tasks 里 done_traces
                 那一步同口径：收尾一到，发起就该退场，否则"最后一条"会是
                 那个占位，线程永远显示成运行中）
      grouped —— 按线程分组，拿首末两条的 id 与尝试次数
      thr     —— 把首末两条摊平成一行，并接上三份外部结论
    """
    where, args = _clauses(f)
    if owner is not None:
        # 先把范围收到"这个人参与过的线程"上（走 username 索引），归属最终
        # 仍按线程**首条**判 —— 与 audit.tasks 一致：续跑会写新 trace，
        # 按最后一条判会让"谁续跑谁就成了主人"。
        where = where + [f"{_THREAD_EXPR} IN (SELECT {_THREAD_EXPR}"
                         f" FROM askdb_audit WHERE username = %s)"]
        args = args + [owner]

    cte = f"""WITH scoped AS (
    SELECT id, ts, trace_id, thread_id, phase, source, source_name, username,
           rejected_by, elapsed_ms, question, recall_blind, mask_degraded,
           attempts, converged_early, explain_rows, rows_returned, multi_step,
           {_THREAD_EXPR} AS th
      FROM askdb_audit WHERE {' AND '.join(where)}
), live AS (
    SELECT s.* FROM scoped s
     WHERE s.phase <> 'started'
        OR NOT EXISTS (SELECT 1 FROM scoped d
                        WHERE d.trace_id = s.trace_id AND d.phase <> 'started')
), grouped AS (
    SELECT th, count(*) AS attempts_on_thread, min(id) AS first_id,
           COALESCE(max(id) FILTER (WHERE rejected_by = 'CANCELED'),
                    max(id)) AS last_id
      FROM live GROUP BY th
), thr AS (
    SELECT g.th AS thread_id, g.attempts_on_thread, g.last_id, g.first_id,
           l.ts, l.trace_id, l.phase, l.source, l.source_name, l.username,
           l.rejected_by, l.elapsed_ms, l.explain_rows, l.rows_returned,
           COALESCE(l.multi_step, false) AS multi_step,
           COALESCE(NULLIF(fr.question, ''), NULLIF(l.question, ''), '') AS question,
           fr.username AS owner, fr.ts AS first_ts,
           COALESCE(ap.st, '') AS approval_status,
           COALESCE(rv.st, '') AS review_status,
           COALESCE(op.st, '') AS ops_status,
           COALESCE(ov.st, '') AS override_status,
           (COALESCE(l.recall_blind, false) OR COALESCE(l.mask_degraded, false)
            OR COALESCE(l.attempts, 1) >= 3
            OR COALESCE(l.converged_early, '') <> '') AS needs_review,
           (l.phase = 'started' AND %s > 0
            AND EXTRACT(EPOCH FROM (%s::timestamptz - l.ts)) > %s) AS stale
      FROM grouped g
      JOIN live l ON l.id = g.last_id
      JOIN live fr ON fr.id = g.first_id
      LEFT JOIN unnest(%s::text[], %s::text[]) AS ap(k, st) ON ap.k = l.trace_id
      LEFT JOIN unnest(%s::text[], %s::text[]) AS rv(k, st) ON rv.k = l.trace_id
      LEFT JOIN unnest(%s::text[], %s::text[]) AS op(k, st) ON op.k = l.trace_id
      LEFT JOIN unnest(%s::text[], %s::text[]) AS ov(k, st) ON ov.k = g.th
     {'WHERE fr.username = %s' if owner is not None else ''}
), folded AS ("""
    args += [fold.stale_after_s, fold.now, fold.stale_after_s]
    args += _pairs(fold.approval) + _pairs(fold.review) + _pairs(fold.ops) \
        + _pairs(fold.override)
    if owner is not None:
        args.append(owner)
    fold_sql, fold_args = _fold_sql(fold)
    return cte + fold_sql + ")", args + fold_args


#: 一次请求最多核实多少条陈旧线程。核实要逐条开检查点库，而陈旧线程是故障态
#: （生产上个位数）—— 真涨到几百条，说明进程在批量被杀，那是另一件事，
#: 不该让任务中心跟着卡住。超出的部分会留在「运行中」，并记一行 warning。
STALE_PROBE_CAP = 500


def stale_threads(f: Any, fold: Any, *, owner: str | None = None) -> list[tuple[Any, ...]]:
    """只落了发起记录、而且已经很久没动静的线程 —— (线程 id, trace id, 运维结论)。

    **必须在分页之前单独取一次**：这些线程要按检查点核实成"可续跑"还是
    "执行期故障"，而检查点在另一个库里，SQL 看不见。核完的结论由调用方
    当作 override 传回来参与折算，这样计数、筛选、分页看到的都是核实后的状态。
    """
    ensure_derived()
    cte, args = _thread_cte(f, fold, owner)
    sql = (cte + " SELECT thread_id, trace_id, ops_status FROM folded"
                 " WHERE stale ORDER BY ts DESC LIMIT %s")
    return pgstore.rows(sql, tuple(args + [STALE_PROBE_CAP + 1]))


def thread_counts(f: Any, fold: Any, *, owner: str | None = None,
                  day: tuple[Any, Any] | None = None) -> list[tuple[Any, ...]]:
    """按状态分档的条数，外加"今天收尾的有多少" —— 四张统计卡的全部来源。

    **算在筛选之前**（与 audit.paginate_tasks 同一条口径）：这几个数讲的是
    系统当下的处境，跟着手上的筛选变的话，筛完「已完成」再看「待处理」
    永远是 0。所以这一支不带任何筛选条件，只带可见范围。
    """
    ensure_derived()
    cte, args = _thread_cte(f, fold, owner)
    lo, hi = day if day else (fold.now, fold.now)
    sql = (cte + " SELECT status, count(*), count(*) FILTER (WHERE ts >= %s AND ts < %s)"
                 " FROM folded GROUP BY status")
    return pgstore.rows(sql, tuple(args + [lo, hi]))


def thread_facets(f: Any, fold: Any, *, owner: str | None = None) -> dict[str, list[Any]]:
    """筛选条上两个下拉的取值：可见范围内真出现过的数据源与发起人。

    同样算在筛选之前 —— 跟着筛选收窄的话，选中一个源之后下拉里就只剩这一个，
    人就退不回去了（/api/audit 的 sources 是同一条口径）。

    顺序是"最近用过的在前"：内层 DISTINCT ON 按取值排（PostgreSQL 的语法
    要求），外层再按时间排。两层各有职责，别合并。
    """
    ensure_derived()
    cte, args = _thread_cte(f, fold, owner)
    src = pgstore.rows(cte + """ SELECT source, label FROM (
        SELECT DISTINCT ON (source) source,
               COALESCE(NULLIF(source_name, ''), NULLIF(source, ''), '（未记录数据源）') AS label,
               ts, last_id FROM folded ORDER BY source, ts DESC, last_id DESC) x
        ORDER BY ts DESC, last_id DESC""", tuple(args))
    usr = pgstore.rows(cte + """ SELECT username FROM (
        SELECT DISTINCT ON (username) username, ts, last_id FROM folded
        ORDER BY username, ts DESC, last_id DESC) x
        ORDER BY ts DESC, last_id DESC""", tuple(args))
    return {"sources": [{"value": sid, "label": label} for sid, label in src],
            "users": [{"value": u, "label": u or "匿名"} for (u,) in usr]}


#: thread_page 返回的列，顺序即 SQL 里的顺序。调用方按名字取，不数位置。
THREAD_COLS = ("total", "thread_id", "last_id", "first_id", "attempts_on_thread",
               "owner", "question", "status", "risk", "task_kind", "stale",
               "approval_status", "review_status", "ops_status", "resumable_hint")


def thread_page(f: Any, fold: Any, *, owner: str | None = None,
                page: int = 1, page_size: int = 10,
                status: str | None = None, source: str | None = None,
                risk: str | None = None, user: str | None = None,
                task_kind: str | None = None, q: str | None = None,
                since: tuple[Any, Any] | None = None) -> tuple[int, list[dict[str, Any]]]:
    """筛完之后的第 N 页线程 —— **筛选、计数、排序、切页全在库里**。

    None 一律是"这一维不筛"；空串是合法取值（未记录数据源 / 匿名发起），
    与 audit.FILTER_ANY 那条约定对应（调用方负责把 "all" 翻成 None）。

    总条数由 count(*) OVER () 随页一起带回来，省一次往返 —— 它算在 WHERE
    之后、LIMIT 之前，正是分页要的那个数。

    排序是 (ts DESC, last_id DESC)。**第二个键不是装饰**：同毫秒的两条线程
    若只按 ts 排，PostgreSQL 给出的顺序在两次查询之间可以不同，翻页时就会
    有记录重复出现、也有记录一次都不出现。
    """
    from .audit import INTERRUPTED, RUNNING

    ensure_derived()
    cte, args = _thread_cte(f, fold, owner)
    where: list[str] = []
    for col, val in (("status", status), ("source", source), ("risk", risk),
                     ("username", user), ("task_kind", task_kind)):
        if val is not None:
            where.append(f"{col} = %s")
            args.append(val)
    if since is not None:
        lo, hi = since
        if lo is not None:
            where.append("ts >= %s")
            args.append(lo)
        if hi is not None:
            where.append("ts < %s")
            args.append(hi)
    if q:
        # 与 audit.paginate_tasks 的关键词同一套命中面：问题、线程 id、trace id。
        # **发起人不在里面**（审计中心那一页才搜发起人）—— 两页本来就不同，
        # 别顺手统一，那会悄悄改掉一页的行为。
        like = f"%{_like_escape(q)}%"
        where.append("(question ILIKE %s ESCAPE '!' OR thread_id ILIKE %s ESCAPE '!'"
                     " OR trace_id ILIKE %s ESCAPE '!')")
        args += [like, like, like]

    # resumable 只给**候选**：审计上"正在跑"与"进程被杀"分不开，现场在不在
    # 检查点里只有检查点知道。服务端随后逐条核实（见 audit.tasks_page）。
    hint = (f"(status IN ({_lit(INTERRUPTED)}, {_lit(RUNNING)}))")
    sql = (cte + f" SELECT count(*) OVER () AS total, thread_id, last_id, first_id,"
                 f" attempts_on_thread, owner, question, status, risk,"
                 f" task_kind, stale, approval_status, review_status, ops_status,"
                 f" {hint} FROM folded"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY ts DESC, last_id DESC OFFSET %s LIMIT %s")
    args += [(page - 1) * page_size, page_size]
    got = pgstore.rows(sql, tuple(args))
    rows_ = [dict(zip(THREAD_COLS, r)) for r in got]
    return (int(got[0][0]) if got else 0), rows_


def records_by_id(ids: list[int]) -> dict[int, dict[str, Any]]:
    """按主键取整条记录 —— **只给当前这一页那十条**。

    分页查询本身一列 record 都不选：把它放进那条 SQL 的投影里，PostgreSQL
    很可能在 LIMIT 之前就把每一行的 jsonb 解压一遍，那就等于什么都没省。
    多一次往返换"离开数据库的 jsonb 恰好是十条"，这笔买卖很划算。
    """
    if not ids:
        return {}
    got = pgstore.rows("SELECT id, record FROM askdb_audit WHERE id = ANY(%s)", (ids,))
    return {int(i): rec for i, rec in got if isinstance(rec, dict)}


def append_approval(rec: dict[str, Any]) -> None:
    _append_event("askdb_approvals", "approval_id", rec)


def read_approvals() -> list[dict[str, Any]]:
    return _read_events("askdb_approvals")


def append_review(rec: dict[str, Any]) -> None:
    _append_event("askdb_reviews", "review_id", rec)


def read_reviews() -> list[dict[str, Any]]:
    return _read_events("askdb_reviews")


def append_ops(rec: dict[str, Any]) -> None:
    _append_event("askdb_ops", "ops_id", rec)


def read_ops() -> list[dict[str, Any]]:
    return _read_events("askdb_ops")


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

    table, id_col = {
        APPROVALS: ("askdb_approvals", "approval_id"),
        REVIEWS: ("askdb_reviews", "review_id"),
        OPS: ("askdb_ops", "ops_id"),
    }[stream]
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
    """四张表各有多少行 —— 迁移前后对账用。"""
    ensure_schema()
    out = {}
    for stream, table in ((AUDIT, "askdb_audit"), (APPROVALS, "askdb_approvals"),
                          (REVIEWS, "askdb_reviews"), (OPS, "askdb_ops")):
        rows = pgstore.rows(f"SELECT count(*) FROM {table}")
        out[stream] = int(rows[0][0]) if rows else 0
    return out
