"""审计/审批/复核三条流水的 PostgreSQL 存储。

用例钉的是几处**取舍**，而不是"能写能读"：
审计写失败要吞（查询已经跑完，不能因为旁路把结果吞掉），审批/复核写失败要抛
（那次写入就是那个操作本身）；迁移要能反复跑；"库挂了"与"没有记录"不能混。
"""

from __future__ import annotations

import uuid

import pytest

from askdb import auditstore, pgstore
from tests.conftest import _test_store_dsn


@pytest.fixture
def store(_no_ambient_store, monkeypatch):
    import psycopg

    dsn = _test_store_dsn()
    if not dsn:
        pytest.fail("凭据库用例需要一个可写的 PostgreSQL：设置 ASKDB_TEST_SOURCES_DSN")
    schema = f"askdb_audit_t_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setenv(pgstore.DSN_ENV, dsn)
    monkeypatch.setenv(pgstore.SCHEMA_ENV, schema)
    pgstore.reset_pool()
    auditstore.reset_ready()
    try:
        yield schema
    finally:
        pgstore.reset_pool()
        auditstore.reset_ready()
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _rec(trace: str, **over):
    return {"trace_id": trace, "ts": "2026-09-09T01:02:03+00:00", "kind": "ask",
            "phase": "", "user": "ops", "role": "DATA_OWNER", "source": "builtin",
            "elapsed_ms": 12, "tok_in": 100, "tok_out": 20, "cost_cny": 0.001, **over}


# ------------------------------------------------------------------ 开关

def test_enabled_needs_both_the_switch_and_the_dsn(cfg, monkeypatch):
    """不做"配了 DSN 就自动启用"：介质是部署决定，不是环境凑出来的。

    **三个回落变量都要自己清干净**，不能只清 ASKDB_STORE_DSN：config.load()
    会把项目根的 .env 读进环境，而开发机的 .env 里有 ASKDB_IDENTITY_DSN ——
    raw_dsn() 回落到它，"没配连接串"那一支就永远走不到。这条用例正因此
    在有 .env 的目录里红、在干净的 worktree 里绿，与 conftest 顶上那条
    _no_ambient_store 的注释说的是同一件事。
    """
    fallbacks = (pgstore.DSN_ENV, "ASKDB_SOURCES_DSN", "ASKDB_IDENTITY_DSN")
    for var in fallbacks:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv(pgstore.DSN_ENV, "host=h")
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "file"}
    assert auditstore.enabled(cfg) is False

    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "postgres"}
    assert auditstore.enabled(cfg) is True

    for var in fallbacks:
        monkeypatch.delenv(var, raising=False)
    assert auditstore.enabled(cfg) is False, "选了 postgres 但没连接串，不算启用"


# -------------------------------------------------------------- 审计流水

def test_audit_round_trip_keeps_write_order(store):
    auditstore.ensure_schema()
    for i in range(3):
        auditstore.append_audit(_rec(f"t{i}"))
    assert [r["trace_id"] for r in auditstore.read_audit()] == ["t0", "t1", "t2"]


def test_started_records_are_filtered_out_by_default(store):
    """发起记录没有结果也没有成本，进统计就是把一次调用数成两次。"""
    auditstore.append_audit(_rec("a", phase="started"))
    auditstore.append_audit(_rec("a"))
    assert [r["phase"] for r in auditstore.read_audit()] == [""]
    assert len(auditstore.read_audit(include_started=True)) == 2


def test_audit_write_failure_is_swallowed(store, monkeypatch, caplog):
    """查询已经跑完了，不能因为凭据库抖动把用户的结果吞掉 —— 但要留痕。"""
    def boom(*a, **k):
        raise RuntimeError("库抖了")

    monkeypatch.setattr(pgstore, "execute", boom)
    with caplog.at_level("WARNING"):
        auditstore.append_audit(_rec("x"))          # 不抛
    assert any("审计写入失败" in m for m in caplog.messages), "静默变少必须有痕迹"


def test_record_without_a_timestamp_still_lands(store):
    """一条没有时间的审计等于没有审计 —— 宁可标成入库时刻，也不写 NULL。"""
    auditstore.append_audit({"trace_id": "no-ts", "kind": "ask"})
    assert [r["trace_id"] for r in auditstore.read_audit()] == ["no-ts"]


def test_unparsable_numbers_do_not_break_the_write(store):
    """模型偶尔回一个字符串数字，审计不能因此整条丢掉。"""
    auditstore.append_audit(_rec("odd", elapsed_ms="慢", tok_in=None, cost_cny="贵"))
    assert [r["trace_id"] for r in auditstore.read_audit()] == ["odd"]


# ---------------------------------------------------- 审批 / 复核（写失败要抛）

def test_approval_and_review_round_trip(store):
    auditstore.append_approval({"id": "ap1", "ts": "2026-09-09T00:00:00+00:00",
                                "status": "PENDING", "trace_id": "t1"})
    auditstore.append_review({"id": "rv1", "ts": "2026-09-09T00:00:00+00:00",
                              "status": "OPEN", "trace_id": "t1"})
    assert [r["id"] for r in auditstore.read_approvals()] == ["ap1"]
    assert [r["id"] for r in auditstore.read_reviews()] == ["rv1"]


def test_approval_write_failure_is_raised_not_swallowed(store, monkeypatch):
    """与审计相反：静默失败会让人以为批过了，而流水里没有。"""
    def boom(*a, **k):
        raise RuntimeError("库抖了")

    monkeypatch.setattr(pgstore, "execute", boom)
    with pytest.raises(RuntimeError):
        auditstore.append_approval({"id": "ap2", "status": "PENDING"})


# ------------------------------------------------------------------ 迁移

def test_import_is_idempotent(store):
    """迁移当天写入还在继续，这条命令必须能反复跑。"""
    recs = [_rec("m1"), _rec("m2")]
    first = auditstore.import_records(auditstore.AUDIT, recs)
    assert first == {"total": 2, "imported": 2, "skipped": 0}

    again = auditstore.import_records(auditstore.AUDIT, recs)
    assert again == {"total": 2, "imported": 0, "skipped": 2}
    assert len(auditstore.read_audit()) == 2, "重复导入不得把记录翻倍"


def test_import_only_brings_in_what_is_new(store):
    auditstore.import_records(auditstore.AUDIT, [_rec("m1")])
    out = auditstore.import_records(auditstore.AUDIT, [_rec("m1"), _rec("m3")])
    assert out == {"total": 2, "imported": 1, "skipped": 1}


def test_import_approvals_is_idempotent(store):
    recs = [{"id": "ap9", "ts": "2026-09-09T00:00:00+00:00", "status": "PENDING"}]
    assert auditstore.import_records(auditstore.APPROVALS, recs)["imported"] == 1
    assert auditstore.import_records(auditstore.APPROVALS, recs)["skipped"] == 1


def test_counts_reconciles_all_three_streams(store):
    """迁移前后对账用 —— 三条流水各自数各自的。"""
    auditstore.append_audit(_rec("c1"))
    auditstore.append_approval({"id": "ap1", "status": "PENDING"})
    assert auditstore.counts() == {auditstore.AUDIT: 1, auditstore.APPROVALS: 1,
                                   auditstore.REVIEWS: 0}


def test_ensure_schema_is_idempotent(store):
    auditstore.ensure_schema()
    auditstore.ensure_schema()
    auditstore.reset_ready()
    auditstore.ensure_schema()          # 忘掉记忆后重跑，仍不该炸
    assert auditstore.counts()[auditstore.AUDIT] == 0
