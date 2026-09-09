"""askdb 自己那几份数据的连接层。

这个模块决定的是"出事之后还查不查得到凭据"，所以用例钉的不是"能连上"，
而是几个**失败方向**：连不上要能与"没有记录"分开、schema 名不能被拼进 SQL、
没配连接串时服务仍要起得来。
"""

from __future__ import annotations

import pytest

from askdb import pgstore
from tests.conftest import _test_store_dsn


@pytest.fixture
def store_env(_no_ambient_store, monkeypatch):
    """给用例一个独立 schema 的凭据库。用完整个 schema 丢掉。"""
    import uuid

    import psycopg

    dsn = _test_store_dsn()
    if not dsn:
        pytest.fail("凭据库用例需要一个可写的 PostgreSQL：设置 ASKDB_TEST_SOURCES_DSN")
    schema = f"askdb_store_t_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setenv(pgstore.DSN_ENV, dsn)
    monkeypatch.setenv(pgstore.SCHEMA_ENV, schema)
    pgstore.reset_pool()
    try:
        yield schema
    finally:
        pgstore.reset_pool()
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


# ------------------------------------------------------------------ 连接串

def test_dsn_falls_back_to_the_other_two_stores(monkeypatch):
    """生产上这三者本来就是同一个库，分开配等于多两把要轮转的凭据。"""
    for var in (pgstore.DSN_ENV, "ASKDB_SOURCES_DSN", "ASKDB_IDENTITY_DSN"):
        monkeypatch.delenv(var, raising=False)
    assert pgstore.raw_dsn() == ""

    monkeypatch.setenv("ASKDB_IDENTITY_DSN", "host=i")
    assert pgstore.raw_dsn() == "host=i"
    monkeypatch.setenv("ASKDB_SOURCES_DSN", "host=s")
    assert pgstore.raw_dsn() == "host=s", "数据源那把优先于名册那把"
    monkeypatch.setenv(pgstore.DSN_ENV, "host=own")
    assert pgstore.raw_dsn() == "host=own", "自己那把优先级最高"


def test_configured_does_not_dial(monkeypatch):
    """健康检查与页面加载都会调它，不能在这里付一次 TCP 往返。"""
    monkeypatch.setenv(pgstore.DSN_ENV, "host=192.0.2.1 connect_timeout=1")
    assert pgstore.configured() is True          # 连不上也照样返回 True


def test_missing_dsn_is_a_named_failure(monkeypatch):
    for var in (pgstore.DSN_ENV, "ASKDB_SOURCES_DSN", "ASKDB_IDENTITY_DSN"):
        monkeypatch.delenv(var, raising=False)
    assert pgstore.configured() is False
    with pytest.raises(pgstore.StoreUnavailable) as e:
        pgstore.dsn()
    assert pgstore.DSN_ENV in str(e.value), "报错要说出该设哪个变量"


def test_password_is_appended_only_when_absent(monkeypatch):
    """口令单独一个变量，是为了让连接串本身可以进日志。"""
    monkeypatch.setenv(pgstore.DSN_ENV, "host=h dbname=d")
    monkeypatch.setenv(pgstore.PASSWORD_ENV, "s3cret")
    assert pgstore.dsn() == "host=h dbname=d password=s3cret"

    monkeypatch.setenv(pgstore.DSN_ENV, "host=h password=inline")
    assert pgstore.dsn() == "host=h password=inline", "连接串里已经有了就不再追加"


# -------------------------------------------------------------------- schema

def test_schema_defaults_to_public(monkeypatch):
    for var in (pgstore.SCHEMA_ENV, "ASKDB_SOURCES_SCHEMA"):
        monkeypatch.delenv(var, raising=False)
    assert pgstore.schema() == "public"


def test_empty_schema_var_falls_back_rather_than_failing(monkeypatch):
    """空串按"没设"处理 —— 部署里把变量置空是常见的关闭方式，
    不该让服务起不来。非法**内容**才拒（见下一条）。"""
    monkeypatch.setenv(pgstore.SCHEMA_ENV, "")
    monkeypatch.delenv("ASKDB_SOURCES_SCHEMA", raising=False)
    assert pgstore.schema() == "public"


@pytest.mark.parametrize("bad", [
    "public; DROP TABLE askdb_audit",
    "askdb-audit",          # 连字符不是合法标识符
    "1abc",                 # 不能数字开头
    "Public",               # 大写：与库里实际建的 schema 对不上
    "a" * 64,               # 超长
])
def test_illegal_schema_name_is_refused(monkeypatch, bad):
    """schema 名是**直接拼进 SQL** 的（SET search_path 不能参数化），
    所以它必须在进 SQL 之前就被判死，而不是指望库去报错。"""
    monkeypatch.setenv(pgstore.SCHEMA_ENV, bad)
    with pytest.raises(pgstore.StoreUnavailable):
        pgstore.schema()


# ---------------------------------------------------------------- 连接与读写

def test_unreachable_store_raises_store_unavailable(monkeypatch):
    """连不上必须是一个**有名字的**异常 —— 调用方据此把「库挂了」
    与「没有记录」分开显示，那正是这轮改造要消灭的静默失败。"""
    monkeypatch.setenv(pgstore.DSN_ENV,
                       "host=192.0.2.1 port=5432 dbname=x connect_timeout=1")
    monkeypatch.delenv(pgstore.SCHEMA_ENV, raising=False)
    pgstore.reset_pool()
    with pytest.raises(pgstore.StoreUnavailable):
        with pgstore.connect():
            pass
    pgstore.reset_pool()


def test_round_trip_through_the_pool(store_env):
    pgstore.execute("CREATE TABLE t (id int, note text)")
    pgstore.execute("INSERT INTO t VALUES (%s, %s)", (1, "甲"))
    assert pgstore.rows("SELECT id, note FROM t") == [(1, "甲")]


def test_reading_a_table_that_does_not_exist_yet_is_empty_not_an_error(store_env):
    """表还没建出来 == 还没有任何记录。连不上才是另一回事（见上一条）。"""
    assert pgstore.rows("SELECT 1 FROM never_created_table") == []


def test_pool_is_rebuilt_after_switching_schema(store_env, monkeypatch):
    """换 schema 必须换池子 —— 沿用旧池会让写入落到上一个 schema 里。"""
    pgstore.execute("CREATE TABLE t (id int)")
    first = pgstore._get_pool()
    monkeypatch.setenv(pgstore.SCHEMA_ENV, "public")
    assert pgstore._get_pool() is not first


def test_reset_pool_is_safe_without_a_pool():
    pgstore.reset_pool()
    pgstore.reset_pool()
