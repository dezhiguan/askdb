"""样例库生成 —— 结构、可复现性与关键数据分布。"""

from __future__ import annotations

import duckdb
import pytest

import data.seed as seed
from tests.conftest import SMALL_KBS


@pytest.fixture
def small(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "KBS", SMALL_KBS)
    return seed.build(out=tmp_path / "s.duckdb", quiet=True)


def test_creates_expected_tables(small):
    con = duckdb.connect(str(small), read_only=True)
    names = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"documents", "knowledge_bases", "orgs", "model_usage"} <= names
    con.close()


def test_row_counts_match_spec(small):
    con = duckdb.connect(str(small), read_only=True)
    total = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    expected = sum(n for _, _, _, n, _, s in SMALL_KBS) + sum(s for *_, s in SMALL_KBS)
    assert total == expected
    con.close()


def test_stuck_documents_exist_for_metric(small):
    """「卡住的文档」口径要有数据可命中，否则该口径无法被验证。"""
    con = duckdb.connect(str(small), read_only=True)
    n = con.execute(
        "SELECT COUNT(*) FROM documents WHERE status='PROCESSING' "
        "AND updated_at < now() - INTERVAL 1 HOUR").fetchone()[0]
    assert n == sum(s for *_, s in SMALL_KBS)
    con.close()


def test_failure_rate_varies_across_kbs(small):
    """失败率必须有高低差异，多步规划那类场景才立得住。"""
    con = duckdb.connect(str(small), read_only=True)
    rates = con.execute("""
        SELECT kb_id, COUNT(*) FILTER (WHERE status='FAILED') * 1.0 / COUNT(*)
        FROM documents GROUP BY kb_id
    """).fetchall()
    vals = sorted(r[1] for r in rates)
    assert vals[-1] - vals[0] > 0.2
    con.close()


def test_multi_tenant_data_present(small):
    con = duckdb.connect(str(small), read_only=True)
    orgs = {r[0] for r in con.execute("SELECT DISTINCT org_id FROM documents").fetchall()}
    assert len(orgs) >= 2
    con.close()


def test_is_reproducible(tmp_path, monkeypatch):
    """固定种子 —— 任何人生成的数据必须完全一致，否则评测结果不可复现。"""
    monkeypatch.setattr(seed, "KBS", SMALL_KBS)
    a = seed.build(out=tmp_path / "a.duckdb", quiet=True)
    b = seed.build(out=tmp_path / "b.duckdb", quiet=True)
    q = "SELECT file_name, status, error_code FROM documents ORDER BY id LIMIT 200"
    ca, cb = duckdb.connect(str(a), read_only=True), duckdb.connect(str(b), read_only=True)
    assert ca.execute(q).fetchall() == cb.execute(q).fetchall()
    ca.close(); cb.close()


def test_rebuild_overwrites_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "KBS", SMALL_KBS)
    out = tmp_path / "x.duckdb"
    seed.build(out=out, quiet=True)
    seed.build(out=out, quiet=True)
    con = duckdb.connect(str(out), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM orgs").fetchone()[0] == len(seed.ORGS)
    con.close()


def test_prints_summary_when_not_quiet(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(seed, "KBS", SMALL_KBS)
    seed.build(out=tmp_path / "p.duckdb", quiet=False)
    assert "样例库已生成" in capsys.readouterr().out
