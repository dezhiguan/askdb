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


def read_audit(*, include_started: bool = False) -> list[dict[str, Any]]:
    """读出全部审计记录，**保持写入顺序**（与文件版的"文件顺序"等价）。

    默认滤掉发起记录（phase=started），与 audit.read_records 同一条口径：
    它没有结果、没有成本，进了统计就是把每次调用数成两次。
    """
    ensure_schema()
    where = "" if include_started else " WHERE phase <> 'started'"
    out = []
    for (rec,) in pgstore.rows(f"SELECT record FROM askdb_audit{where} ORDER BY id"):
        if isinstance(rec, dict) and rec.get("trace_id"):
            out.append(rec)
    return out


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
