"""配置加载与启动期校验。

校验的意义在于：配置错误必须在启动时炸，
而不是拖到生成 SQL 时才以"查不出数据"的形式出现。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from askdb.config import Metric, load

ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path: Path, askdb: str, tables: str, metrics: str) -> Path:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "askdb.yaml").write_text(textwrap.dedent(askdb), encoding="utf-8")
    (tmp_path / "config" / "tables.yaml").write_text(textwrap.dedent(tables), encoding="utf-8")
    (tmp_path / "config" / "metrics.yaml").write_text(textwrap.dedent(metrics), encoding="utf-8")
    return tmp_path / "config" / "askdb.yaml"


BASE_MAIN = """\
    datasource: {type: duckdb, path: ./data/x.duckdb, read_only: true}
    tenant: {column: org_id, default_ctx: 65, mode: predicate, on_unresolved: reject}
    guard: {max_rows: 10, max_scan_rows: 100, statement_timeout_ms: 100, max_retry: 1,
            allow_select_star: false, deny_functions: [read_csv]}
    planner: {enabled: false, max_steps: 3, max_carry_rows: 50, cost_cap_tokens: 100}
    schema_rag: {mode: keyword, top_k: 1, max_k: 2, token_budget: 1500}
    llm: {provider: x, base_url: "http://x", model: m, temperature: 0, api_key_env: NOPE_KEY}
    observability: {audit_log: ./data/audit.jsonl}
    tables_file: ./config/tables.yaml
    metrics_file: ./config/metrics.yaml
"""
BASE_TABLES = """\
    tables:
      - name: documents
        desc: 文档
        aliases: [文档]
        columns:
          id: {type: BIGINT, desc: ID}
          org_id: {type: BIGINT, desc: 组织, tenant: true}
          status: {type: VARCHAR, desc: 状态, enum: [OK, BAD]}
"""


def test_loads_real_project_config():
    cfg = load(ROOT / "config" / "askdb.yaml")
    assert "documents" in cfg.tables
    assert cfg.tenant_column == "org_id"
    assert cfg.tenant_tables()
    assert cfg.max_rows > 0 and cfg.max_retry >= 0
    assert cfg.deny_functions
    assert cfg.audit_log.name.endswith(".jsonl")


def test_column_metadata_is_parsed():
    cfg = load(ROOT / "config" / "askdb.yaml")
    status = cfg.tables["documents"].columns["status"]
    assert "COMPLETED" in status.enum
    assert cfg.tables["documents"].tenant_column == "org_id"
    assert cfg.tables["orgs"].tenant_column is None


def test_api_key_absent_returns_none(monkeypatch):
    cfg = load(ROOT / "config" / "askdb.yaml")
    monkeypatch.delenv(cfg.llm["api_key_env"], raising=False)
    assert cfg.api_key() is None


def test_api_key_present(monkeypatch):
    cfg = load(ROOT / "config" / "askdb.yaml")
    monkeypatch.setenv(cfg.llm["api_key_env"], "sk-test")
    assert cfg.api_key() == "sk-test"


def test_rejects_when_no_tenant_column(tmp_path):
    tables = BASE_TABLES.replace(", tenant: true", "")
    p = _write(tmp_path, BASE_MAIN, tables, "metrics: []")
    with pytest.raises(ValueError, match="租户列"):
        load(p)


def test_rejects_tenant_column_mismatch(tmp_path):
    main = BASE_MAIN.replace("column: org_id", "column: tenant_id")
    p = _write(tmp_path, main, BASE_TABLES, "metrics: []")
    with pytest.raises(ValueError, match="不一致"):
        load(p)


def test_rejects_metric_scope_outside_allowlist(tmp_path):
    metrics = "metrics:\n  - {name: X, aliases: [], scope: [ghost], expr: 'COUNT(*)'}\n"
    p = _write(tmp_path, BASE_MAIN, BASE_TABLES, metrics)
    with pytest.raises(ValueError, match="不在白名单"):
        load(p)


def test_rejects_metric_without_definition(tmp_path):
    metrics = "metrics:\n  - {name: X, aliases: [], scope: [documents]}\n"
    p = _write(tmp_path, BASE_MAIN, BASE_TABLES, metrics)
    with pytest.raises(ValueError, match="既没有"):
        load(p)


def test_rejects_rls_mode_on_non_postgres(tmp_path):
    main = BASE_MAIN.replace("mode: predicate", "mode: rls")
    p = _write(tmp_path, main, BASE_TABLES, "metrics: []")
    with pytest.raises(ValueError, match="PostgreSQL"):
        load(p)


def test_metric_matches_by_name_and_alias():
    m = Metric(name="卡住的文档", aliases=["卡住", "堆积"], scope=["documents"], predicate="x")
    assert m.matches("有哪些卡住的文档")
    assert m.matches("最近堆积得厉害吗")
    assert not m.matches("这个月的成本是多少")
