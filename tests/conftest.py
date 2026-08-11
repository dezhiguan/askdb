"""共享夹具。

样例库按会话构建一次，且用裁剪过的数据量 —— 测试要快，
但结构必须和真库完全一致，否则测出来的护栏行为不作数。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import data.seed as seed
from askdb.config import load
from askdb.executor import Executor

ROOT = Path(__file__).resolve().parent.parent

# 裁剪版：保留租户、失败率高低差异与卡住的文档，行数压到千级
SMALL_KBS = [
    (1, 65, "产品中心", 400, 0.02, 0),
    (7, 65, "外部采集库", 300, 0.30, 0),
    (12, 65, "历史归档库", 200, 0.35, 3),
    (40, 66, "合作方文档", 150, 0.03, 1),
]


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory: pytest.TempPathFactory, request) -> Path:
    out = tmp_path_factory.mktemp("db") / "sample.duckdb"
    original = seed.KBS
    seed.KBS = SMALL_KBS
    try:
        seed.build(out=out, quiet=True)
    finally:
        seed.KBS = original
    return out


@pytest.fixture
def cfg(sample_db: Path, tmp_path: Path):
    """指向裁剪样例库的配置。

    审计日志与检查点库按用例隔离到 tmp_path —— 它们是**跨调用累积**的状态，
    共享会让每日配额一类的用例互相干扰（曾因此假失败）。
    """
    c = load(ROOT / "config" / "askdb.yaml")
    c.raw = copy.deepcopy(c.raw)
    c.raw["datasource"]["path"] = str(sample_db)
    c.raw["observability"]["audit_log"] = str(tmp_path / "audit.jsonl")
    c.raw["observability"]["checkpoint_db"] = str(tmp_path / "checkpoints.sqlite")
    return c


@pytest.fixture
def ex(cfg):
    with Executor(cfg) as e:
        yield e
