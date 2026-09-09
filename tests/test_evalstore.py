"""评测成绩的 PostgreSQL 存储。

迁库之前这些成绩是 .json 文件加一个 .prev.json 备份：只留得下一轮，
"上一轮"靠复制文件得到（复制可能失败在半路），而"跑于何时"只能拿文件
mtime 近似 —— 复制一次就漂一次。用例钉的就是这几件事现在不再是那样。
"""

from __future__ import annotations

import uuid

import pytest

from askdb import evalstore, pgstore
from tests.conftest import _test_store_dsn


@pytest.fixture
def store(_no_ambient_store, monkeypatch):
    import psycopg

    dsn = _test_store_dsn()
    if not dsn:
        pytest.fail("凭据库用例需要一个可写的 PostgreSQL：设置 ASKDB_TEST_SOURCES_DSN")
    schema = f"askdb_eval_t_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setenv(pgstore.DSN_ENV, dsn)
    monkeypatch.setenv(pgstore.SCHEMA_ENV, schema)
    pgstore.reset_pool()
    evalstore._ready.clear()
    try:
        yield schema
    finally:
        pgstore.reset_pool()
        evalstore._ready.clear()
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _report(score: float, ds: str = "sample.duckdb"):
    return {"overall": score, "provenance": {"datasource": ds}}


def test_enabled_follows_the_audit_switch(cfg, monkeypatch):
    """成绩与凭据分家存放没有意义 —— 同一个开关。"""
    monkeypatch.setenv(pgstore.DSN_ENV, "host=h")
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "postgres"}
    assert evalstore.enabled(cfg) is True
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "file"}
    assert evalstore.enabled(cfg) is False


def test_latest_returns_the_newest_run(store):
    evalstore.save("golden", _report(0.71))
    evalstore.save("golden", _report(0.88))
    assert evalstore.latest("golden")["overall"] == 0.88


def test_previous_run_is_a_page_back_not_a_backup_file(store):
    """back=1 就是上一轮。不再依赖 .prev.json 那次复制。"""
    evalstore.save("golden", _report(0.71))
    evalstore.save("golden", _report(0.88))
    assert evalstore.latest("golden", back=1)["overall"] == 0.71


def test_more_than_two_runs_are_all_kept(store):
    """文件版只留得下一轮；这里翻多少页都在。"""
    for s in (0.5, 0.6, 0.7, 0.8):
        evalstore.save("golden", _report(s))
    assert [evalstore.latest("golden", back=i)["overall"] for i in range(4)] == \
        [0.8, 0.7, 0.6, 0.5]


def test_latest_of_an_unknown_name_is_none_not_an_error(store):
    assert evalstore.latest("从来没跑过") is None


def test_runs_are_listed_newest_first_with_a_real_timestamp(store):
    """ran_at 取入库时刻，不是文件 mtime。"""
    evalstore.save("a", _report(0.5))
    evalstore.save("b", _report(0.6))
    listed = evalstore.runs()
    assert [r["name"] for r in listed] == ["b", "a"]
    assert listed[0]["ran_at"], "每一轮都要说得出跑于何时"


def test_runs_can_be_filtered_by_name(store):
    evalstore.save("a", _report(0.5))
    evalstore.save("b", _report(0.6))
    assert [r["name"] for r in evalstore.runs("a")] == ["a"]


def test_datasource_is_pulled_out_of_the_report(store):
    """成绩离开数据源没有意义 —— 同一套题在别的库上的分数不可比。"""
    evalstore.save("golden", _report(0.9, ds="careermate"))
    got = pgstore.rows("SELECT datasource FROM askdb_eval_runs")
    assert got == [("careermate",)]


def test_report_without_provenance_still_saves(store):
    """老报告没有 provenance 段，不能因此存不进去。"""
    evalstore.save("legacy", {"overall": 0.4})
    assert evalstore.latest("legacy")["overall"] == 0.4
