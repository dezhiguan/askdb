"""共享夹具。

样例库按会话构建一次，且用裁剪过的数据量 —— 测试要快，
但结构必须和真库完全一致，否则测出来的护栏行为不作数。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import yaml

import data.seed as seed
from askdb.config import Metric, load, parse_tables
from askdb.executor import Executor

ROOT = Path(__file__).resolve().parent.parent

# 裁剪版：保留租户、失败率高低差异与卡住的文档，行数压到千级
SMALL_KBS = [
    (1, 65, "产品中心", 400, 0.02, 0),
    (7, 65, "外部采集库", 300, 0.30, 0),
    (12, 65, "历史归档库", 200, 0.35, 3),
    (40, 66, "合作方文档", 150, 0.03, 1),
]


def _yaml_of(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory: pytest.TempPathFactory, request) -> Path:
    out = tmp_path_factory.mktemp("db") / "sample.duckdb"
    original, original_316 = seed.KBS, seed.KBS_316
    seed.KBS = SMALL_KBS
    # 真实的 KBS_316 有 1.4 万行，会话级小库不需要它；不置空的话
    # 每个用例都要背着这份数据，且行数断言全部对不上。
    seed.KBS_316 = []
    try:
        seed.build(out=out, quiet=True)
    finally:
        seed.KBS, seed.KBS_316 = original, original_316
    return out


@pytest.fixture
def cfg(sample_db: Path, tmp_path: Path):
    """指向裁剪样例库的配置。

    审计日志与检查点库按用例隔离到 tmp_path —— 它们是**跨调用累积**的状态，
    共享会让每日配额一类的用例互相干扰（曾因此假失败）。
    """
    c = load(ROOT / "config" / "askdb.yaml")
    c.raw = copy.deepcopy(c.raw)
    # 数据源、白名单、租户策略全部在这里钉死，**不继承开发配置**。
    # 开发配置是会被改的（换库、换白名单、换默认租户都合法），
    # 用例跟着它漂就会在别人改配置那天集体假失败 —— 出现过一次，别再来第二次。
    c.raw["datasource"] = {"type": "duckdb", "path": str(sample_db), "read_only": True}
    c.raw["tenant"] = {**c.raw["tenant"], "column": "org_id",
                       "default_ctx": 65, "mode": "predicate"}
    c.tables = parse_tables(_yaml_of(ROOT / "config" / "tables.yaml")["tables"])
    c.metrics = [Metric(**m) for m in _yaml_of(ROOT / "config" / "metrics.yaml")["metrics"]]
    # 回放开关同理钉死：它在开发配置里是会被打开的，而多条用例断言的是
    # "关着时 health/stats 怎么说" —— 跟着开发配置漂就会集体变红。
    c.raw["observability"] = {**c.raw["observability"], "replay_api": False}
    c.raw["observability"]["audit_log"] = str(tmp_path / "audit.jsonl")
    c.raw["observability"]["checkpoint_db"] = str(tmp_path / "checkpoints.sqlite")
    # 登录同理钉死。开发配置 2026-09-05 起 required: true（十个内置账号 + 真实库），
    # 而这里绝大多数用例是匿名发查询的 —— 跟着开发配置漂，一次开关就集体 401。
    # 要验证强制登录本身的用例自行打开（见 tests/test_auth.py）。
    c.raw["auth"] = {**(c.raw.get("auth") or {}), "required": False}
    # 身份库是外部 PostgreSQL，和审计日志同属「跨用例累积的外部状态」，
    # 而且它在开发配置里是打开的 —— 不摘掉，跑一次测试就会往开发库里写角色成员。
    # 需要验证身份功能的用例自行打开（见 tests/test_identity.py）。
    c.raw.pop("identity", None)
    return c


@pytest.fixture
def ex(cfg):
    with Executor(cfg) as e:
        yield e


@pytest.fixture(autouse=True)
def _reset_quota_cache():
    """配额器按配置缓存复用，用例之间会改上限 —— 不清就会串。"""
    from askdb import quota

    quota.reset_cache()
    yield
    quota.reset_cache()


# ---------------------------------------------------------------- 数据源存储

#: 测试用元数据库。**有意与 ASKDB_SOURCES_DSN 分开**：跑一次测试会清表，
#: 指着生产/开发库跑就把真实数据源清掉了。要单独设一个才跑得起来。
TEST_DSN_ENV = "ASKDB_TEST_SOURCES_DSN"


def _test_store_dsn() -> str:
    import os
    return (os.environ.get(TEST_DSN_ENV) or "").strip()


@pytest.fixture
def sources_store(monkeypatch):
    """给每个用例一个独立 schema 的 askdb_sources 表。

    **不 skip。** 没配 TEST_DSN_ENV 就直接 fail —— 数据源注册表自 2026-09-06
    起只有 PG 一种存储，跳过它意味着 17 个用例（准入、口令、隔离、审计归属）
    全都不跑，而报告还是绿的。这一类"绿色的假象"比红灯难查得多。

    用临时 schema 而不是 TRUNCATE 公共表：并行跑用例、或者有人手滑把
    TEST_DSN_ENV 指到了有数据的库上时，临时 schema 都碰不到别人的表。
    """
    import uuid

    from askdb import sources

    dsn = _test_store_dsn()
    if not dsn:
        pytest.fail(
            f"数据源用例需要一个可写的 PostgreSQL：设置 {TEST_DSN_ENV}，"
            f"例如 '{TEST_DSN_ENV}=host=127.0.0.1 dbname=askdb_test user=…'。"
            f"（有意不 skip：跳过等于这批安全用例一条都没跑，而报告是绿的）")

    schema = f"askdb_t_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv(sources.DSN_ENV, dsn)
    monkeypatch.setenv(sources.SCHEMA_ENV, schema)
    sources.reset_pool()
    sources.ensure_schema()
    try:
        yield schema
    finally:
        import psycopg

        sources.reset_pool()
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        sources.reset_pool()
