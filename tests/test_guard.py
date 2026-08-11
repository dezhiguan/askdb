"""护栏测试 —— 本项目最重要的一组用例。

除了逐条规则的正例反例，重点覆盖**绕过手法**：
注释切断关键词、大小写混淆、多语句夹带、子查询/CTE 里藏表。
这些是字符串匹配挡不住、AST 判定才挡得住的场景。
"""

from __future__ import annotations

import pytest
import sqlglot

from askdb import guard

ORG = 65


def chk(sql: str, cfg, org: int = ORG):
    return guard.check(sql, cfg, org_id=org)


# --------------------------------------------------------------------- R-01

def test_r01_rejects_stacked_statements(cfg):
    r = chk("SELECT id FROM documents; UPDATE orgs SET name='x'", cfg)
    assert not r.ok and r.rejected_by == "R-01"


def test_r01_rejects_unparsable(cfg):
    r = chk("SELECT FROM WHERE ((", cfg)
    assert not r.ok and r.rejected_by == "R-01"


def test_r01_trailing_semicolon_is_fine(cfg):
    assert chk("SELECT id FROM documents;", cfg).ok


# --------------------------------------------------------------------- R-02

@pytest.mark.parametrize("sql", [
    "DELETE FROM documents WHERE status='FAILED'",
    "UPDATE documents SET status='COMPLETED'",
    "INSERT INTO documents (id) VALUES (1)",
    "CREATE TABLE t AS SELECT 1",
    "DROP TABLE documents",
])
def test_r02_rejects_non_select(sql, cfg):
    r = chk(sql, cfg)
    assert not r.ok and r.rejected_by == "R-02"


def test_r02_accepts_with_select(cfg):
    r = chk("WITH w AS (SELECT kb_id FROM documents) SELECT kb_id FROM w", cfg)
    assert r.ok


def test_r02_comment_split_keyword_is_still_caught(cfg):
    """DEL/**/ETE 骗得过正则黑名单，骗不过解析器。"""
    r = chk("DEL/**/ETE FROM documents WHERE 1=1", cfg)
    assert not r.ok
    assert r.rejected_by in ("R-01", "R-02")


def test_r02_case_mixing_is_still_caught(cfg):
    r = chk("dElEtE FROM documents WHERE 1=1", cfg)
    assert not r.ok and r.rejected_by == "R-02"


# --------------------------------------------------------------------- R-03

def test_r03_rejects_unknown_table(cfg):
    r = chk("SELECT a FROM secret_table", cfg)
    assert not r.ok and r.rejected_by == "R-03" and "secret_table" in r.reason


def test_r03_catches_table_hidden_in_subquery(cfg):
    r = chk("SELECT id FROM documents WHERE kb_id IN (SELECT x FROM shadow_tbl)", cfg)
    assert not r.ok and r.rejected_by == "R-03"


def test_r03_catches_table_hidden_in_cte(cfg):
    r = chk("WITH w AS (SELECT a FROM hidden_tbl) SELECT a FROM w", cfg)
    assert not r.ok and r.rejected_by == "R-03"


def test_r03_catches_table_hidden_in_join(cfg):
    r = chk("SELECT d.id FROM documents d JOIN nope n ON n.id = d.kb_id", cfg)
    assert not r.ok and r.rejected_by == "R-03"


def test_r03_cte_alias_is_not_treated_as_table(cfg):
    r = chk("WITH w AS (SELECT kb_id FROM documents) "
            "SELECT d.id FROM documents d JOIN w ON w.kb_id = d.kb_id", cfg)
    assert r.ok


# --------------------------------------------------------------------- R-04

def test_r04_rejects_qualified_hallucinated_column(cfg):
    r = chk("SELECT documents.member_level FROM documents", cfg)
    assert not r.ok and r.rejected_by == "R-04"


def test_r04_rejects_bare_hallucinated_column_single_table(cfg):
    r = chk("SELECT member_level FROM documents", cfg)
    assert not r.ok and r.rejected_by == "R-04"


def test_r04_rejects_via_alias(cfg):
    r = chk("SELECT d.nope_col FROM documents d", cfg)
    assert not r.ok and r.rejected_by == "R-04"


def test_r04_accepts_real_columns(cfg):
    assert chk("SELECT d.file_name, d.status FROM documents d", cfg).ok


def test_r04_skips_ambiguous_multi_table_scope(cfg):
    """多表作用域下的裸字段留给 P1，此处不应误杀。"""
    r = chk("SELECT file_name FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id", cfg)
    assert r.ok


# --------------------------------------------------------------------- R-05

def test_r05_expands_bare_star(cfg):
    """设计要求 R-05 是「改写」而非「阻断」（§4.1 阻断=否）。"""
    r = chk("SELECT * FROM orgs", cfg)
    assert r.ok and "R-05" in r.rules_fired
    assert "*" not in r.sql
    for col in cfg.tables["orgs"].columns:
        assert col in r.sql


def test_r05_expands_qualified_star_with_alias(cfg):
    r = chk("SELECT o.* FROM orgs o", cfg)
    assert r.ok and "o.id" in r.sql and "o.name" in r.sql


def test_r05_expands_every_table_in_multi_table_scope(cfg):
    r = chk("SELECT * FROM documents d JOIN orgs o ON o.id = d.org_id", cfg)
    assert r.ok
    assert "d.file_name" in r.sql and "o.name" in r.sql


def test_r05_keeps_other_select_items(cfg):
    r = chk("SELECT o.*, 1 AS x FROM orgs o", cfg)
    assert r.ok and "AS x" in r.sql


def test_r05_rejects_when_expansion_is_unsafe(cfg):
    """列来自 CTE 时无法静态确定，只能拒绝 —— 不能瞎展开。"""
    r = chk("WITH w AS (SELECT id FROM orgs) SELECT * FROM w", cfg)
    assert not r.ok and r.rejected_by == "R-05"


def test_r05_can_be_disabled(cfg):
    cfg.raw["guard"]["allow_select_star"] = True
    r = chk("SELECT * FROM documents", cfg)
    assert r.ok and "R-05" not in r.rules_fired and "*" in r.sql


# --------------------------------------------------------------------- R-07

def test_r07_rejects_denied_function(cfg):
    r = chk("SELECT id FROM documents WHERE file_name = read_csv('/etc/passwd')", cfg)
    assert not r.ok and r.rejected_by == "R-07"


def test_r07_normalizes_underscores(cfg):
    """read_csv 与 sqlglot 解析出的 ReadCSV 要能对上。"""
    assert guard._normalize("read_csv") == guard._normalize("ReadCSV")


# --------------------------------------------------------------------- R-09

def test_r09_injects_limit_when_missing(cfg):
    r = chk("SELECT id FROM documents", cfg)
    assert r.ok and "R-09" in r.rules_fired
    assert f"LIMIT {cfg.max_rows}" in r.sql.upper().replace("\n", " ")


def test_r09_caps_oversized_limit(cfg):
    r = chk("SELECT id FROM documents LIMIT 999999", cfg)
    assert r.ok and "R-09" in r.rules_fired
    assert "999999" not in r.sql


def test_r09_keeps_small_limit(cfg):
    r = chk("SELECT id FROM documents LIMIT 5", cfg)
    assert r.ok and "R-09" not in r.rules_fired and "LIMIT 5" in r.sql


def test_r09_rewrites_non_literal_limit(cfg):
    r = chk("SELECT id FROM documents LIMIT (SELECT 3)", cfg)
    assert r.ok and "R-09" in r.rules_fired


# --------------------------------------------------------------------- R-10

def _where_of(sql: str) -> str:
    return sql.replace("\n", " ").upper()


def test_r10_injects_tenant_predicate(cfg):
    r = chk("SELECT id FROM documents", cfg)
    assert r.ok and "R-10" in r.rules_fired
    assert "DOCUMENTS.ORG_ID = 65" in _where_of(r.sql)


def test_r10_uses_alias_when_present(cfg):
    r = chk("SELECT d.id FROM documents d", cfg)
    assert "D.ORG_ID = 65" in _where_of(r.sql)


def test_r10_injects_for_every_joined_tenant_table(cfg):
    r = chk("SELECT d.id, k.name FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id", cfg)
    s = _where_of(r.sql)
    assert "D.ORG_ID = 65" in s and "K.ORG_ID = 65" in s


def test_r10_injects_into_cte_inner_and_outer(cfg):
    """CTE 内层与外层必须各自独立注入 —— 漏任一层即构成越权路径。"""
    r = chk("WITH w AS (SELECT kb_id FROM documents GROUP BY kb_id) "
            "SELECT d.file_type FROM documents d JOIN w ON w.kb_id = d.kb_id", cfg)
    assert r.ok
    assert _where_of(r.sql).count("ORG_ID = 65") == 2


def test_r10_injects_into_subquery(cfg):
    r = chk("SELECT id FROM documents WHERE kb_id IN "
            "(SELECT id FROM knowledge_bases)", cfg)
    assert r.ok
    assert _where_of(r.sql).count("ORG_ID = 65") == 2


def test_r10_respects_org_id_argument(cfg):
    r = chk("SELECT id FROM documents", cfg, org=99)
    assert "DOCUMENTS.ORG_ID = 99" in _where_of(r.sql)


def test_r10_model_supplied_org_id_cannot_replace_injection(cfg):
    """模型自己写了别的 org_id 也没用，系统那条照样追加。"""
    r = chk("SELECT id FROM documents WHERE org_id = 999", cfg)
    s = _where_of(r.sql)
    assert "ORG_ID = 999" in s and "DOCUMENTS.ORG_ID = 65" in s


def test_r10_uses_tenant_filter_for_indirect_ownership(cfg):
    """真实库里很多表没有租户列，靠外键间接归属 —— tenant_filter 覆盖这种情况。"""
    r = chk("SELECT id, name FROM orgs", cfg)
    assert r.ok and "R-10" in r.rules_fired
    assert "ORGS.ID = 65" in _where_of(r.sql)


def test_r10_tenant_filter_supports_subquery(cfg):
    """间接归属常常要子查询（documents.kb_id → knowledge_bases.org_id）。"""
    cfg.tables["documents"].columns["org_id"].tenant = False
    cfg.tables["documents"].tenant_filter = (
        "{ref}.kb_id IN (SELECT id FROM knowledge_bases WHERE org_id = {ctx})")
    r = chk("SELECT d.file_name FROM documents d", cfg)
    assert r.ok and "R-10" in r.rules_fired
    s = _where_of(r.sql)
    assert "D.KB_ID IN" in s and "ORG_ID = 65" in s


def test_r10_rejects_table_without_any_tenancy(cfg):
    """漏配一张表就是一条越权路径 —— 失败要朝安全的方向失败。"""
    t = cfg.tables["orgs"]
    t.tenant_filter = None
    r = chk("SELECT id, name FROM orgs", cfg)
    assert not r.ok and r.rejected_by == "R-10"
    assert "无法确定租户归属" in r.reason


def test_r10_exempt_table_is_skipped(cfg):
    """全局字典表可以豁免，但必须是显式声明。"""
    t = cfg.tables["orgs"]
    t.tenant_filter = None
    t.tenant_exempt = True
    r = chk("SELECT id, name FROM orgs", cfg)
    assert r.ok and "R-10" not in r.rules_fired


def test_r10_rejects_unparsable_tenant_filter(cfg):
    cfg.tables["orgs"].tenant_filter = "{ref}.id === {ctx} BROKEN ((("
    r = chk("SELECT id FROM orgs", cfg)
    assert not r.ok and r.rejected_by == "R-10"


# --------------------------------------------------------------------- 综合

def test_rewritten_sql_stays_parsable(cfg):
    r = chk("SELECT d.file_name FROM documents d WHERE d.status = 'FAILED'", cfg)
    assert r.ok
    assert len(sqlglot.parse(r.sql, dialect="duckdb")) == 1


def test_rewrites_are_reported_for_display(cfg):
    r = chk("SELECT id FROM documents", cfg)
    assert any("租户" in x for x in r.rewrites)
    assert any("LIMIT" in x for x in r.rewrites)


def test_not_yet_enforced_is_declared(cfg):
    """未实现的规则必须显式列出，避免"看起来全都实现了"的错觉。"""
    assert set(guard.NOT_YET_ENFORCED) == {"R-06", "R-08", "R-11"}


def test_from_node_handles_both_sqlglot_key_names(cfg):
    """sqlglot 30 把 args["from"] 改成 "from_"，两种都要认。"""
    e = sqlglot.parse_one("SELECT id FROM documents", dialect="duckdb")
    assert guard._from_node(e) is not None
    e.args["from"] = e.args.pop("from_")
    assert guard._from_node(e) is not None


def test_union_is_accepted_and_injected(cfg):
    r = chk("SELECT id FROM documents UNION SELECT id FROM knowledge_bases", cfg)
    assert r.ok
    assert _where_of(r.sql).count("ORG_ID = 65") == 2


def test_r10_outer_join_predicate_goes_to_on_not_where(cfg):
    """把外连接的租户谓词放进 WHERE 会把 LEFT JOIN 悄悄降级成 INNER JOIN。

    表现是结果少行且不报错 —— 最难发现的一类改写事故。
    """
    r = chk("SELECT k.name, COUNT(d.id) AS n FROM knowledge_bases k "
            "LEFT JOIN documents d ON d.kb_id = k.id GROUP BY k.id, k.name", cfg)
    assert r.ok
    s = " ".join(r.sql.split())
    on_part = s.split("WHERE")[0]
    assert "D.ORG_ID = 65" in on_part.upper()          # 进了 ON
    assert "K.ORG_ID = 65" in s.upper().split("WHERE")[1]   # FROM 上的表仍进 WHERE


def test_r10_inner_join_predicate_may_stay_in_where(cfg):
    """内连接没有这个问题，放 WHERE 语义等价。"""
    r = chk("SELECT k.name FROM knowledge_bases k JOIN documents d ON d.kb_id = k.id", cfg)
    assert r.ok
    s = " ".join(r.sql.split()).upper()
    assert "D.ORG_ID = 65" in s.split("WHERE")[1]


def test_left_join_keeps_rows_without_match(cfg, ex):
    """端到端验证：0 文档的知识库不能因为改写而消失。"""
    r = chk("SELECT k.name AS 库, COUNT(d.id) AS 文档数 FROM knowledge_bases k "
            "LEFT JOIN documents d ON d.kb_id = k.id AND d.status = 'NOPE_NEVER_MATCHES' "
            "GROUP BY k.id, k.name", cfg)
    assert r.ok
    res = ex.run(r.sql)
    assert res.row_count > 0, "外连接被降级成内连接，无匹配的左表行被吃掉了"
    assert all(row[1] == 0 for row in res.rows)
