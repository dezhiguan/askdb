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
    """R-12：DuckDB 没有 statement_timeout，靠看门狗中断。

    **阈值不能取 1ms。** 看门狗是另起一条线程等 timeout 再调 con.interrupt()；
    阈值太小时它可能在 con.execute() 真正开跑**之前**就到点，那次 interrupt
    打在一条空闲连接上等于没打，查询随后跑完，用例报 DID NOT RAISE。
    这条在 2026-09-10 的两轮全量里各偶发了一次，单跑却总是绿的 —— 典型的
    竞态假红，比真 bug 更费排查时间。

    取 150ms + 一条实测 ~2.9s 的查询：到点时 execute 必然已在飞行中，
    余量二十倍。改小任何一边之前，先想清楚上面这段。
    """
    cfg.raw["guard"]["statement_timeout_ms"] = 150
    with Executor(cfg) as e:
        with pytest.raises(DataSourceError) as err:
            e.run("SELECT SUM(range) FROM range(400000000)")
    assert "超时" in str(err.value)


def test_close_is_safe_to_call_twice(cfg):
    e = Executor(cfg)
    e.connect()
    e.close()
    e.close()


def test_query_result_carries_data_time(cfg):
    """§8 准入条件 #7：输出必须附带数据时间。

    不标时间的结果，隔天再看会被当成当前状态 —— 对连生产库的工具，
    这是会直接误导决策的缺失。
    """
    with Executor(cfg) as ex:
        r = ex.run("SELECT 1 AS x")
    assert r.as_of, "查询结果必须带数据时间"
    from datetime import datetime
    datetime.fromisoformat(r.as_of)          # 必须是可解析的 ISO 文本


# ------------------------------------------- pg_stats 取值补全（2026-09-09）

def test_pg_array_literal_is_parsed():
    """没有 COMMENT 的库靠 pg_stats 拿取值；most_common_vals 是 anyarray，
    psycopg 取不到具体类型，只能转 text 再拆。"""
    from askdb.executor import _parse_pg_array
    assert _parse_pg_array("{COMPLETED,FAILED,PENDING}") == ["COMPLETED", "FAILED", "PENDING"]
    assert _parse_pg_array('{"a b",c}') == ["a b", "c"]
    assert _parse_pg_array(None) == [] and _parse_pg_array("abc") == []


def test_only_text_columns_get_enum_values():
    """数值/时间列的高频值是数据不是取值集合，拿去做枚举归一毫无意义。"""
    from askdb.executor import _is_texty
    assert _is_texty("character varying") and _is_texty("text")
    assert not _is_texty("bigint") and not _is_texty("timestamp with time zone")


def test_describe_with_no_tables_asks_nothing(cfg):
    """没有表就不该发查询 —— 空 IN 列表在 PG 上是一次无谓往返。"""
    from askdb.executor import Executor
    with Executor(cfg) as ex:
        assert ex.describe([]) == {}


def test_enum_backfill_is_best_effort(monkeypatch):
    """pg_stats 读不到（无权限、老版本、统计未生成）时必须原样返回。

    取值补全是锦上添花：为它让"加数据源"整个失败，是把可用性赔给了优化项。
    """
    from askdb.executor import _PgBackend

    grouped = {"t": [{"name": "c", "type": "text"}]}

    class Boom:
        def cursor(self):
            raise RuntimeError("pg_stats 不可读")

    backend = _PgBackend.__new__(_PgBackend)
    monkeypatch.setattr(backend, "connect", lambda: Boom(), raising=False)
    assert backend._attach_enums(["t"], grouped) == grouped


def test_enum_backfill_keeps_values_already_parsed_from_comments(monkeypatch):
    """注释里已经解析出取值时，统计不该把它覆盖掉 —— 注释是人写的，更准。"""
    from askdb.executor import _PgBackend

    grouped = {"t": [{"name": "c", "type": "text", "enum": ["A", "B"]}]}

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return None
        def fetchall(self): return [("t", "c", 2, "{X,Y}")]

    class Conn:
        def cursor(self): return Cur()

    backend = _PgBackend.__new__(_PgBackend)
    monkeypatch.setattr(backend, "connect", lambda: Conn(), raising=False)
    out = backend._attach_enums(["t"], grouped)
    assert out["t"][0]["enum"] == ["A", "B"]


# ==========================================================================
# 阻断项 / 警示项（2026-09-10）
#
# 账号姿态那几项从"一项不过就不许接入"降为"接入放行但一直红着"。
# 这组用例钉的是**降级不等于消失**：标记还在、名字还出得来、开关能收回去。
# ==========================================================================

from askdb import executor as _ex          # noqa: E402  —— 与上面的用例分区放


def _posture_fails(monkeypatch, cfg, names=("账号为只读",)):
    """把某几项姿态检查打成失败，模拟拿高权账号接库。"""
    def fake(self):
        return [(n, n not in names, "假装的检查结果")
                for n in ("账号为只读", "语句超时已设置", "连接数上限已设置")]

    monkeypatch.setattr(_ex._DuckBackend, "env_checks", fake)
    with Executor(cfg) as e:
        return e.self_check()


def test_account_posture_failure_does_not_block(cfg, monkeypatch):
    checks = _posture_fails(monkeypatch, cfg)
    assert _ex.blocking_failures(checks) == []
    assert _ex.advisory_failures(checks) == ["账号为只读"]
    # **仍然是 ✕**：允许接入与检查通过是两件事，界面按 ok 渲染
    assert [c["ok"] for c in checks if c["name"] == "账号为只读"] == [False]


def test_timeout_failure_still_blocks(cfg, monkeypatch):
    """语句超时护的是**对方的库**，不是我们的账号姿态 —— 它不在降级之列。"""
    checks = _posture_fails(monkeypatch, cfg, names=("语句超时已设置",))
    assert _ex.blocking_failures(checks) == ["语句超时已设置"]


def test_write_probe_always_blocks(cfg, monkeypatch):
    """唯一不靠声明的那条证据：真发一条写语句，由引擎拒掉。
    它永远阻断，strict 开关也管不着它。"""
    monkeypatch.setattr(_ex._DuckBackend, "fetch",
                        lambda self, sql, cap: ([], [], ""))   # 写没被拒
    with Executor(cfg) as e:
        checks = e.self_check()
    probe = [c for c in checks if c["name"] == "写操作实探"][0]
    assert not probe["ok"] and probe["blocking"]
    assert "写操作实探" in _ex.blocking_failures(checks)


def test_strict_switch_restores_the_old_behaviour(cfg, monkeypatch):
    cfg.raw["datasources"] = {**cfg.raw.get("datasources", {}),
                              "strict_account_check": True}
    checks = _posture_fails(monkeypatch, cfg)
    assert _ex.blocking_failures(checks) == ["账号为只读"]
    assert _ex.advisory_failures(checks) == []
