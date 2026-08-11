"""执行层测试：自检、干跑、行数上限、超时、错误提示。"""

from __future__ import annotations

import pytest

from askdb.executor import DataSourceError, Executor


def test_self_check_all_pass(ex):
    checks = ex.self_check()
    names = [c["name"] for c in checks]
    assert "写操作实探" in names
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


def test_self_check_write_probe_actually_probes(ex):
    """最后一项必须是真发起写、真被拒，而不是读配置。"""
    probe = [c for c in ex.self_check() if c["name"] == "写操作实探"][0]
    assert probe["ok"] and "拒绝" in probe["detail"]


def test_self_check_reports_missing_whitelisted_table(cfg, sample_db, monkeypatch):
    cfg.tables["ghost_table"] = cfg.tables["orgs"]
    with Executor(cfg) as e:
        checks = e.self_check()
    bad = [c for c in checks if c["name"] == "授权表集合"][0]
    assert not bad["ok"] and "ghost_table" in bad["detail"]


def test_missing_database_gives_actionable_hint(cfg, tmp_path):
    cfg.raw["datasource"]["path"] = str(tmp_path / "nope.duckdb")
    with pytest.raises(DataSourceError) as e:
        Executor(cfg).connect()
    assert "data.seed" in e.value.hint


def test_self_check_short_circuits_when_db_missing(cfg, tmp_path):
    cfg.raw["datasource"]["path"] = str(tmp_path / "nope.duckdb")
    checks = Executor(cfg).self_check()
    assert len(checks) == 1 and not checks[0]["ok"]


def test_unsupported_datasource_type(cfg):
    cfg.raw["datasource"]["type"] = "oracle"
    with pytest.raises(DataSourceError, match="暂不支持"):
        Executor(cfg).connect()


def test_connect_is_idempotent(ex):
    assert ex.connect() is ex.connect()


def test_explain_returns_estimate(ex):
    r = ex.explain("SELECT file_name FROM documents WHERE org_id = 65 LIMIT 10")
    assert r.ok and r.est_rows is not None and r.est_rows > 0


def test_explain_blocks_when_over_threshold(cfg):
    cfg.raw["guard"]["max_scan_rows"] = 1
    with Executor(cfg) as e:
        r = e.explain("SELECT file_type FROM documents WHERE org_id = 65 LIMIT 100")
    assert not r.ok and "超过阈值" in r.reason


def test_explain_handles_invalid_sql(ex):
    r = ex.explain("SELECT nope_col FROM documents")
    assert not r.ok and "执行计划" in r.reason


def test_run_returns_rows_and_columns(ex):
    r = ex.run("SELECT file_name, status FROM documents WHERE org_id = 65 LIMIT 5")
    assert r.columns == ["file_name", "status"]
    assert 0 < r.row_count <= 5 and not r.truncated
    assert r.elapsed_ms >= 0


def test_run_truncates_at_row_cap(cfg):
    cfg.raw["guard"]["max_rows"] = 3
    with Executor(cfg) as e:
        r = e.run("SELECT id FROM documents WHERE org_id = 65")
    assert r.row_count == 3 and r.truncated


def test_run_raises_on_bad_sql(ex):
    with pytest.raises(Exception):
        ex.run("SELECT nope FROM documents")


def test_timeout_interrupts_long_query(cfg):
    """R-12：DuckDB 没有 statement_timeout，靠看门狗中断。"""
    cfg.raw["guard"]["statement_timeout_ms"] = 1
    with Executor(cfg) as e:
        with pytest.raises(DataSourceError) as err:
            e.run("SELECT COUNT(*) FROM range(400000000)")
    assert "超时" in str(err.value)


def test_close_is_safe_to_call_twice(cfg):
    e = Executor(cfg)
    e.connect()
    e.close()
    e.close()
