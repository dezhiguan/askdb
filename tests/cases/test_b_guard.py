"""B 域 · 静态校验 R-01～R-08（40 条）—— 对应 docs/test-cases.md。

全部判定必须基于 AST，不得有任何字符串匹配。每条规则都配一条"变形绕过"
用例 —— 注释、大小写、引号、嵌套，都是真实攻击面。

参数 id 即用例编号，失败时能直接对回设计文档。
"""

from __future__ import annotations

import pytest

from askdb import guard


def _check(sql, cfg, org=65):
    return guard.check(sql, cfg, org_id=org, dialect=cfg.dialect)


# ==========================================================================
# B1 R-01 单语句限制
# ==========================================================================

def test_b01_single_select_passes(cfg):
    assert _check("SELECT id AS x FROM documents", cfg).ok


def test_b02_semicolon_smuggles_second_statement(cfg):
    r = _check("SELECT id AS x FROM documents; DROP TABLE documents", cfg)
    assert not r.ok and r.rejected_by == "R-01"


def test_b03_trailing_semicolon_is_legal(cfg):
    """尾随分号是合法写法，不能误杀。"""
    assert _check("SELECT id AS x FROM documents;", cfg).ok


def test_b04_comment_hides_second_statement(cfg):
    r = _check("SELECT 1 AS x; -- harmless\nDELETE FROM documents", cfg)
    assert not r.ok and r.rejected_by in ("R-01", "R-02")


def test_b05_unparsable_sql(cfg):
    r = _check("SELECT FROM WHERE ???", cfg)
    assert not r.ok and r.rejected_by == "R-01"


# ==========================================================================
# B2 R-02 语句类型白名单
# ==========================================================================

@pytest.mark.parametrize("cid,sql", [
    ("B-06", "DELETE FROM documents"),
    ("B-07", "UPDATE documents SET status = 'x'"),
    ("B-08", "INSERT INTO documents (id) VALUES (1)"),
    ("B-09", "DROP TABLE documents"),
    ("B-10", "ALTER TABLE documents ADD COLUMN c INT"),
])
def test_b06_b10_write_statements_rejected(cid, sql, cfg):
    r = _check(sql, cfg)
    assert not r.ok, f"{cid}: 写操作必须被拒"
    assert r.rejected_by in ("R-01", "R-02"), f"{cid}: 实际 {r.rejected_by}"


def test_b11_with_select_is_legal(cfg):
    sql = ("WITH t AS (SELECT kb_id, COUNT(*) AS n FROM documents GROUP BY kb_id) "
           "SELECT kb_id AS 库, n AS 数量 FROM t")
    assert _check(sql, cfg).ok


def test_b12_writable_cte_rejected(cfg):
    """可写 CTE —— 形式上是 WITH，实质是 DELETE，最易漏的一条。"""
    sql = "WITH x AS (SELECT id FROM documents) DELETE FROM documents"
    r = _check(sql, cfg)
    assert not r.ok and r.rejected_by in ("R-01", "R-02")


# ==========================================================================
# B3 R-03 表白名单
# ==========================================================================

def test_b13_whitelisted_table_passes(cfg):
    assert _check("SELECT id AS x FROM documents", cfg).ok


@pytest.mark.parametrize("cid,sql", [
    ("B-14", "SELECT COUNT(*) AS n FROM chunks"),
    ("B-15", "SELECT id AS x FROM documents WHERE id IN (SELECT id FROM chunks)"),
    ("B-16", "WITH t AS (SELECT id FROM chunks) SELECT id AS x FROM t"),
    ("B-17", "SELECT id AS x FROM documents UNION SELECT id FROM chunks"),
    ("B-18", "SELECT id AS x FROM documents WHERE EXISTS (SELECT 1 FROM chunks)"),
])
def test_b14_b18_non_whitelisted_table_in_any_branch(cid, sql, cfg):
    r = _check(sql, cfg)
    assert not r.ok, f"{cid}: 白名单外的表必须被拦，漏一个分支就是越权路径"
    assert r.rejected_by == "R-03", f"{cid}: 实际 {r.rejected_by}"


def test_b19_cte_alias_shadowing_real_table(cfg):
    """CTE 别名与真实表同名时，不得把别名当成真实表引用。"""
    sql = "WITH documents AS (SELECT 1 AS x) SELECT x AS y FROM documents"
    assert _check(sql, cfg).ok


@pytest.mark.parametrize("cid,sql", [
    ("B-20a", 'SELECT COUNT(*) AS n FROM "CHUNKS"'),
    ("B-20b", "SELECT COUNT(*) AS n FROM Chunks"),
])
def test_b20_case_and_quote_variants(cid, sql, cfg):
    r = _check(sql, cfg)
    assert not r.ok and r.rejected_by == "R-03", f"{cid}: 大小写/引号变形不得绕过"


# ==========================================================================
# B4 R-04 字段真实性
# ==========================================================================

def test_b21_real_column_passes(cfg):
    assert _check("SELECT file_name AS 文件名 FROM documents", cfg).ok


def test_b22_hallucinated_column(cfg):
    r = _check("SELECT no_such_col AS x FROM documents", cfg)
    assert not r.ok and r.rejected_by == "R-04"
    assert "no_such_col" in r.reason


def test_b23_column_belongs_to_another_table(cfg):
    r = _check("SELECT kb_id AS x FROM knowledge_bases", cfg)
    assert not r.ok and r.rejected_by == "R-04"


def test_b24_order_by_select_alias(cfg):
    """回归：ORDER BY 引用的是 SELECT 别名，不是字段，不得误杀。"""
    sql = ("SELECT kb_id AS 库, COUNT(*) AS n FROM documents "
           "GROUP BY kb_id ORDER BY n DESC")
    assert _check(sql, cfg).ok


def test_b25_group_by_ordinal(cfg):
    sql = "SELECT status AS 状态, COUNT(*) AS n FROM documents GROUP BY 1"
    assert _check(sql, cfg).ok


def test_b26_cte_produced_column_used_outside(cfg):
    sql = ("WITH t AS (SELECT id AS x FROM documents) "
           "SELECT x AS y FROM t")
    assert _check(sql, cfg).ok


def test_b27_alias_qualified_column(cfg):
    assert _check("SELECT d.file_name AS 名 FROM documents d", cfg).ok


# ==========================================================================
# B5 R-05 SELECT * 展开
# ==========================================================================

def test_b28_star_expanded(cfg):
    r = _check("SELECT * FROM documents", cfg)
    assert r.ok and "*" not in r.sql
    assert "file_name" in r.sql


def test_b29_qualified_star_expanded(cfg):
    r = _check("SELECT d.* FROM documents d", cfg)
    assert r.ok and "*" not in r.sql


def test_b30_star_over_join(cfg):
    sql = "SELECT * FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id"
    r = _check(sql, cfg)
    assert r.ok and "*" not in r.sql


def test_b31_allow_select_star_config(cfg):
    cfg.raw["guard"]["allow_select_star"] = True
    r = _check("SELECT * FROM documents", cfg)
    assert r.ok and "*" in r.sql, "显式打开时不应展开"


# ==========================================================================
# B6 R-06 跨 schema / 跨库
# ==========================================================================

@pytest.mark.parametrize("cid,sql", [
    ("B-32", "SELECT usename AS u FROM pg_catalog.pg_user"),
    ("B-33", "SELECT x AS y FROM otherdb.public.t"),
    ("B-35", "SELECT column_name AS c FROM information_schema.columns"),
])
def test_b32_b35_cross_schema_rejected(cid, sql, cfg):
    r = _check(sql, cfg)
    assert not r.ok, f"{cid}: 跨 schema/跨库引用必须被拦"
    assert r.rejected_by in ("R-03", "R-06"), f"{cid}: 实际 {r.rejected_by}"


def test_b34_explicit_allowed_schema(cfg):
    """白名单内 schema 的显式限定应放行，否则合法写法被误杀。"""
    r = _check("SELECT id AS x FROM main.documents", cfg)
    assert r.ok, f"被拒：{r.rejected_by} {r.reason}"


# ==========================================================================
# B7 R-07 危险函数
# ==========================================================================

@pytest.mark.parametrize("cid,sql", [
    ("B-36", "SELECT pg_read_file('/etc/passwd') AS x"),
    ("B-37", "SELECT pg_sleep(60) AS x"),
    ("B-38", "SELECT PG_READ_FILE('/etc/passwd') AS x"),
])
def test_b36_b38_dangerous_functions(cid, sql, cfg):
    cfg.raw["guard"]["deny_functions"] = list(cfg.raw["guard"]["deny_functions"]) + [
        "pg_read_file", "pg_sleep"]
    r = _check(sql, cfg)
    assert not r.ok, f"{cid}: 危险函数必须被拦"
    assert r.rejected_by == "R-07", f"{cid}: 实际 {r.rejected_by}"


# ==========================================================================
# B8 R-08 笛卡尔积
# ==========================================================================

def test_b39_join_without_on(cfg):
    r = _check("SELECT d.id AS a FROM documents d JOIN knowledge_bases k", cfg)
    assert not r.ok and r.rejected_by == "R-08"


def test_b40_tautological_on(cfg):
    r = _check("SELECT d.id AS a FROM documents d JOIN knowledge_bases k ON 1=1", cfg)
    assert not r.ok and r.rejected_by == "R-08"
