"""A 域 · 配置加载与校验（16 条）—— 对应 docs/test-cases.md。

配置是 fail-closed 的第一道关。这里漏一条，后面所有护栏都可能建在流沙上。
用例编号即 pytest 的参数 id，失败时能直接对回设计文档。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
import yaml

from askdb.config import load

ROOT = Path(__file__).resolve().parent.parent.parent


def _write_cfg(tmp: Path, sample_db: Path, mutate=None, tables=None, metrics=None) -> Path:
    """按基线配置生成一份可改坏的副本，用于校验用例。"""
    raw = yaml.safe_load((ROOT / "config" / "askdb.yaml").read_text(encoding="utf-8"))
    raw["datasource"]["path"] = str(sample_db)
    raw["observability"]["audit_log"] = str(tmp / "a.jsonl")
    raw["observability"]["checkpoint_db"] = str(tmp / "c.sqlite")

    tf = tmp / "tables.yaml"
    mf = tmp / "metrics.yaml"
    tf.write_text(yaml.safe_dump(
        tables if tables is not None
        else yaml.safe_load((ROOT / "config" / "tables.yaml").read_text(encoding="utf-8")),
        allow_unicode=True), encoding="utf-8")
    mf.write_text(yaml.safe_dump(
        metrics if metrics is not None
        else yaml.safe_load((ROOT / "config" / "metrics.yaml").read_text(encoding="utf-8")),
        allow_unicode=True), encoding="utf-8")
    raw["tables_file"] = "./" + str(tf.relative_to(tmp)) if False else str(tf)
    raw["metrics_file"] = str(mf)

    if mutate:
        mutate(raw)
    # 配置解析用 root = 配置文件的上上级；这里让 tables_file 用绝对路径规避
    d = tmp / "config"
    d.mkdir(exist_ok=True)
    p = d / "askdb.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def _base_tables() -> dict:
    return yaml.safe_load((ROOT / "config" / "tables.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- A-01 / A-02

def test_a01_load_valid_config(cfg):
    assert cfg.tables and cfg.metrics
    assert cfg.db_type in ("duckdb", "postgresql")


def test_a02_missing_config_file():
    with pytest.raises(FileNotFoundError) as e:
        load(ROOT / "config" / "nope-does-not-exist.yaml")
    assert "nope-does-not-exist" in str(e.value)


def test_a03_malformed_yaml(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    p = d / "bad.yaml"
    p.write_text("datasource:\n  type: duckdb\n   path: x\n", encoding="utf-8")
    with pytest.raises(Exception) as e:
        load(p)
    # 报错必须能定位到文件，否则改坏了配置根本不知道改的是哪份
    assert "bad.yaml" in str(e.value) or "yaml" in type(e.value).__name__.lower()


def test_a04_missing_tables_file_key(tmp_path, sample_db):
    p = _write_cfg(tmp_path, sample_db, mutate=lambda r: r.pop("tables_file"))
    with pytest.raises((KeyError, ValueError)):
        load(p)


def test_a05_empty_whitelist(tmp_path, sample_db):
    p = _write_cfg(tmp_path, sample_db, tables={"tables": []})
    with pytest.raises(ValueError) as e:
        load(p)
    assert "白名单" in str(e.value) or "表" in str(e.value)


# ---------------------------------------------------------------- 租户校验

def test_a06_tenant_on_but_no_table_declares(tmp_path, sample_db):
    t = _base_tables()
    for tb in t["tables"]:
        for c in tb.get("columns", {}).values():
            c.pop("tenant", None)
        tb.pop("tenant_filter", None)
        tb.pop("tenant_exempt", None)
    p = _write_cfg(tmp_path, sample_db, tables=t)
    with pytest.raises(ValueError) as e:
        load(p)
    assert "租户" in str(e.value)


def test_a07_single_table_missing_tenancy(tmp_path, sample_db):
    t = _base_tables()
    for tb in t["tables"]:
        if tb["name"] == "documents":
            for c in tb.get("columns", {}).values():
                c.pop("tenant", None)
            tb.pop("tenant_filter", None)
    p = _write_cfg(tmp_path, sample_db, tables=t)
    with pytest.raises(ValueError) as e:
        load(p)
    assert "documents" in str(e.value)


def test_a08_table_tenant_column_conflicts_with_global(tmp_path, sample_db):
    t = _base_tables()
    for tb in t["tables"]:
        if tb["name"] == "documents":
            cols = tb["columns"]
            for name, c in list(cols.items()):
                if c.pop("tenant", None):
                    cols["tenant_id"] = {**c, "tenant": True}
                    cols.pop(name)
                    break
    p = _write_cfg(tmp_path, sample_db, tables=t)
    with pytest.raises(ValueError) as e:
        load(p)
    assert "tenant_id" in str(e.value) and "org_id" in str(e.value)


def test_a09_tenant_filter_without_ctx(tmp_path, sample_db):
    t = _base_tables()
    for tb in t["tables"]:
        if tb["name"] == "documents":
            for c in tb.get("columns", {}).values():
                c.pop("tenant", None)
            tb["tenant_filter"] = "{ref}.org_id = 1"      # 写死租户 = 越权
    p = _write_cfg(tmp_path, sample_db, tables=t)
    with pytest.raises(ValueError) as e:
        load(p)
    assert "{ctx}" in str(e.value) or "ctx" in str(e.value)


def test_a10_tenant_filter_without_ref(tmp_path, sample_db):
    """缺 {ref} 时无法定位到具体表实例，自连接场景会失效。"""
    t = _base_tables()
    for tb in t["tables"]:
        if tb["name"] == "documents":
            for c in tb.get("columns", {}).values():
                c.pop("tenant", None)
            tb["tenant_filter"] = "org_id = {ctx}"
    p = _write_cfg(tmp_path, sample_db, tables=t)
    with pytest.raises(ValueError) as e:
        load(p)
    assert "ref" in str(e.value)


def test_a11_single_tenant_db_disabled(tmp_path, sample_db):
    t = _base_tables()
    for tb in t["tables"]:
        for c in tb.get("columns", {}).values():
            c.pop("tenant", None)
        tb.pop("tenant_filter", None)

    def m(r):
        r["tenant"]["enabled"] = False
        r["tenant"]["mode"] = "predicate"
    c = load(_write_cfg(tmp_path, sample_db, mutate=m, tables=t))
    assert c.tenant_enabled is False


def test_a12_disabled_but_table_still_declares(tmp_path, sample_db):
    def m(r):
        r["tenant"]["enabled"] = False
        r["tenant"]["mode"] = "predicate"
    p = _write_cfg(tmp_path, sample_db, mutate=m)     # 表仍带 tenant: true
    with pytest.raises(ValueError) as e:
        load(p)
    assert "tenant" in str(e.value).lower() or "租户" in str(e.value)


# ---------------------------------------------------------------- 口径与模式

def test_a13_metric_scope_outside_whitelist(tmp_path, sample_db):
    m = {"metrics": [{"name": "X", "aliases": [], "scope": ["chunks"], "expr": "COUNT(*)"}]}
    with pytest.raises(ValueError) as e:
        load(_write_cfg(tmp_path, sample_db, metrics=m))
    assert "chunks" in str(e.value)


def test_a14_metric_without_expr_or_predicate(tmp_path, sample_db):
    m = {"metrics": [{"name": "X", "aliases": [], "scope": ["documents"]}]}
    with pytest.raises(ValueError) as e:
        load(_write_cfg(tmp_path, sample_db, metrics=m))
    assert "X" in str(e.value)


def test_a15_rls_mode_on_duckdb(tmp_path, sample_db):
    p = _write_cfg(tmp_path, sample_db,
                   mutate=lambda r: r["tenant"].__setitem__("mode", "rls"))
    with pytest.raises(ValueError) as e:
        load(p)
    assert "PostgreSQL" in str(e.value)


def test_a16_env_wins_over_dotenv(tmp_path, sample_db, monkeypatch):
    """既有环境变量必须胜出 —— 否则 .env 会悄悄覆盖运维显式设置的值。"""
    monkeypatch.setenv("ASKDB_TEST_TOKEN", "from-env")
    (tmp_path / ".env").write_text("ASKDB_TEST_TOKEN=from-dotenv\n", encoding="utf-8")
    # config.load 的 root 是配置文件的上上级目录
    p = _write_cfg(tmp_path, sample_db)
    load(p)
    assert os.environ["ASKDB_TEST_TOKEN"] == "from-env"
