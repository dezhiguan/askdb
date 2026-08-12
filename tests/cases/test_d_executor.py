"""D 域 · 执行层 R-11/R-12/R-13 与连接自检（22 条）。"""

from __future__ import annotations

import copy
from datetime import datetime

import pytest

from askdb import guard
from askdb.executor import DataSourceError, Executor


def _run(ex, cfg, sql, org=65):
    g = guard.check(sql, cfg, org_id=org, dialect=cfg.dialect)
    assert g.ok, f"用例前置失败：{g.rejected_by} {g.reason}"
    return ex.run(g.sql)


def test_d01_duckdb_query(ex, cfg):
    r = _run(ex, cfg, "SELECT id AS x FROM documents")
    assert r.row_count > 0 and r.columns


@pytest.mark.skip(reason="需 PostgreSQL 隔离环境，不在默认套件中执行")
def test_d02_postgres_query():
    ...


def test_d03_missing_database_file(cfg, tmp_path):
    cfg.raw["datasource"]["path"] = str(tmp_path / "nope.duckdb")
    with pytest.raises(DataSourceError) as e:
        with Executor(cfg) as x:
            x.connect()
    assert "seed" in (e.value.hint or "").lower() or "样例库" in str(e.value)


def test_d04_unreachable_datasource(cfg):
    cfg.raw["datasource"] = {"type": "postgresql",
                             "dsn": "host=127.0.0.1 port=1 dbname=x user=y"}
    with pytest.raises(DataSourceError) as e:
        with Executor(cfg) as x:
            x.connect()
    assert "password" not in str(e.value).lower()


def test_d05_wrong_password_not_leaked(cfg):
    cfg.raw["datasource"] = {
        "type": "postgresql",
        "dsn": "host=127.0.0.1 port=1 dbname=x user=y password=SUPERSECRET"}
    with pytest.raises(DataSourceError) as e:
        with Executor(cfg) as x:
            x.connect()
    assert "SUPERSECRET" not in str(e.value), "口令绝不能出现在报错里"


def test_d06_scan_threshold_blocks(ex, cfg):
    cfg.raw["guard"]["max_scan_rows"] = 1
    g = guard.check("SELECT id AS x FROM documents", cfg, org_id=65, dialect=cfg.dialect)
    p = ex.explain(g.sql)
    assert not p.ok and "阈值" in p.reason


def test_d07_explain_row_formats(ex, cfg):
    """回归：DuckDB 两种行数格式都要能解析出估算值。"""
    g = guard.check("SELECT id AS x FROM documents", cfg, org_id=65, dialect=cfg.dialect)
    p = ex.explain(g.sql)
    assert p.est_rows is not None and p.est_rows > 0


def test_d08_explain_without_cardinality(ex, cfg):
    g = guard.check("SELECT 1 AS x", cfg, org_id=65, dialect=cfg.dialect)
    p = ex.explain(g.sql)
    assert p.ok, "无基数估计不得误判为超限"


def test_d09_explain_engine_error(ex, cfg):
    p = ex.explain("SELECT * FROM no_such_table_at_all")
    assert p.ok is False or p.est_rows is None, "引擎报错须归类为干跑失败，不得崩"


@pytest.mark.skip(reason="需 PostgreSQL：statement_timeout 由数据库侧生效")
def test_d10_statement_timeout_pg():
    ...


def test_d11_watchdog_interrupt(ex, cfg):
    """DuckDB 无 statement_timeout，靠看门狗中断。"""
    cfg.raw["guard"]["statement_timeout_ms"] = 1
    with pytest.raises(DataSourceError) as e:
        ex.run("SELECT COUNT(*) FROM range(200000000)")
    assert "超时" in str(e.value)


def test_d12_row_truncation(ex, cfg):
    cfg.raw["guard"]["max_rows"] = 5
    r = ex.run("SELECT id FROM documents LIMIT 100")
    assert r.row_count == 5 and r.truncated


def test_d13_exactly_at_limit_not_marked_truncated(ex, cfg):
    cfg.raw["guard"]["max_rows"] = 5
    r = ex.run("SELECT id FROM documents LIMIT 5")
    assert r.row_count == 5 and not r.truncated, "恰好等于上限不得误标截断"


def test_d14_empty_result(ex, cfg):
    r = _run(ex, cfg, "SELECT id AS x FROM documents WHERE status = 'NOPE'")
    assert r.row_count == 0 and not r.truncated


def test_d15_write_probe_must_be_rejected(ex):
    checks = ex.self_check()
    probe = [c for c in checks if "写操作" in c["name"]]
    assert probe and probe[0]["ok"], "写探针未通过 = 连接不是只读"


@pytest.mark.parametrize("cid,name", [
    ("D-16", "账号为只读"), ("D-17", "语句超时"), ("D-18", "连接数上限"),
])
def test_d16_d18_env_checks_present(cid, name, ex):
    checks = ex.self_check()
    hit = [c for c in checks if name in c["name"]]
    assert hit, f"{cid}: 自检缺少「{name}」项"
    assert hit[0]["ok"], f"{cid}: {hit[0]['detail']}"


def test_d19_grant_vs_whitelist(ex, cfg):
    cfg.tables["ghost_table"] = copy.deepcopy(cfg.tables["documents"])
    cfg.tables["ghost_table"].name = "ghost_table"
    checks = ex.self_check()
    grants = [c for c in checks if "授权表" in c["name"]]
    assert grants and not grants[0]["ok"], "白名单里有库中不存在的表时自检必须失败"
    assert "ghost_table" in grants[0]["detail"]


@pytest.mark.skip(reason="需 PostgreSQL：超级用户与 BYPASSRLS 判定")
def test_d20_not_superuser_pg():
    ...


def test_d21_decimal_rendering(ex, cfg):
    """回归：高标度 numeric 用 str() 会变成 0E-20，人认不出那是 0。"""
    from askdb.graph import jsonable
    from decimal import Decimal
    assert jsonable(Decimal("0E-20")) == "0"
    assert jsonable(Decimal("1.500")) == "1.5"


def test_d22_as_of_present_and_parsable(ex, cfg):
    r = _run(ex, cfg, "SELECT id AS x FROM documents")
    assert r.as_of, "结果必须带数据时间"
    datetime.fromisoformat(r.as_of)
