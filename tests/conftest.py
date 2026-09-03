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
