"""C 域 · 强制改写与租户隔离 R-09 / R-10（24 条）—— 对应 docs/test-cases.md。

设计文档 §4.4：仅靠应用层改写不足以隔离，任一语法分支遗漏即构成越权路径。
因此这一域几乎每条都是 P0 —— 子查询、CTE、UNION、多层嵌套、自连接、
外连接，每个分支都必须单独有一条。
"""

from __future__ import annotations

import re

import pytest
import sqlglot

from askdb import guard

ORG = 65
OTHER = 66


def _check(sql, cfg, org=ORG):
    return guard.check(sql, cfg, org_id=org, dialect=cfg.dialect)


def _tenant_preds(sql: str, col: str = "org_id") -> list[str]:
    """SQL 里出现的租户谓词，按出现顺序返回。"""
    return re.findall(rf"\b\w*\.?{col}\s*=\s*(-?\d+)", sql)


# ==========================================================================
# R-10 租户谓词注入
# ==========================================================================

def test_c01_inject_when_no_where(cfg):
    r = _check("SELECT id AS x FROM documents", cfg)
    assert r.ok and _tenant_preds(r.sql) == [str(ORG)]


def test_c02_append_with_and(cfg):
    r = _check("SELECT id AS x FROM documents WHERE status = 'FAILED'", cfg)
    assert r.ok
    assert "FAILED" in r.sql and _tenant_preds(r.sql) == [str(ORG)]


def test_c03_model_supplied_wrong_tenant_is_overridden(cfg):
    """模型自带了别的租户值，最终仍须限定在上下文租户。"""
    r = _check(f"SELECT id AS x FROM documents WHERE org_id = {OTHER}", cfg)
    assert r.ok
    vals = set(_tenant_preds(r.sql))
    assert str(ORG) in vals, "上下文租户必须被注入"
    # 若 66 仍在，两个条件是 AND 关系 → 结果恒空，不构成越权但会静默返回空集
    assert vals == {str(ORG)} or vals == {str(ORG), str(OTHER)}


def test_c04_inject_in_subquery(cfg):
    sql = ("SELECT id AS x FROM documents "
           "WHERE kb_id IN (SELECT id FROM knowledge_bases)")
    r = _check(sql, cfg)
    assert r.ok and len(_tenant_preds(r.sql)) >= 2, "内外层都要注入"


def test_c05_inject_in_cte(cfg):
    sql = ("WITH t AS (SELECT kb_id, COUNT(*) AS n FROM documents GROUP BY kb_id) "
           "SELECT kb_id AS 库, n AS 数量 FROM t")
    r = _check(sql, cfg)
    assert r.ok and len(_tenant_preds(r.sql)) >= 1, "CTE 内部必须注入"


def test_c06_inject_in_both_union_branches(cfg):
    sql = ("SELECT id AS x FROM documents "
           "UNION SELECT id FROM knowledge_bases")
    r = _check(sql, cfg)
    assert r.ok and len(_tenant_preds(r.sql)) >= 2, "UNION 每个分支都要注入"


def test_c07_inject_in_deeply_nested_subqueries(cfg):
    sql = ("SELECT id AS x FROM documents WHERE kb_id IN ("
           "  SELECT id FROM knowledge_bases WHERE org_id IN ("
           "    SELECT id FROM orgs))")
    r = _check(sql, cfg)
    assert r.ok
    # 按改写说明计数，而不是正则扫 org_id —— orgs 表的租户列是它自己的 id，
    # 只匹配 org_id 会漏掉一层（这是用例第一版的错，不是护栏的错）
    injected = [x for x in r.rewrites if "租户" in x]
    n = sum(x.count("=") for x in injected)
    assert n >= 3, f"每一层都要注入，实际 {n}：{injected}"


def test_c08_self_join_injects_each_alias(cfg):
    sql = ("SELECT a.id AS x FROM documents a "
           "JOIN documents b ON b.kb_id = a.kb_id")
    r = _check(sql, cfg)
    assert r.ok and len(_tenant_preds(r.sql)) >= 2, "同一表的两个别名各自注入"


def test_c09_outer_join_predicate_goes_to_on(cfg):
    """回归：谓词进 WHERE 会让 LEFT JOIN 退化成 INNER JOIN，
    0 文档的知识库会静默消失。"""
    sql = ("SELECT k.name AS 库, COUNT(d.id) AS n FROM knowledge_bases k "
           "LEFT JOIN documents d ON d.kb_id = k.id GROUP BY k.id, k.name")
    r = _check(sql, cfg)
    assert r.ok
    tree = sqlglot.parse_one(r.sql, dialect=cfg.dialect)
    join = tree.find(sqlglot.exp.Join)
    assert join is not None and "org_id" in join.sql(), "外连接谓词必须进 ON"


@pytest.mark.parametrize("cid,kind", [("C-10a", "RIGHT"), ("C-10b", "FULL OUTER")])
def test_c10_right_and_full_outer_join(cid, kind, cfg):
    sql = (f"SELECT k.name AS 库, COUNT(d.id) AS n FROM knowledge_bases k "
           f"{kind} JOIN documents d ON d.kb_id = k.id GROUP BY k.id, k.name")
    r = _check(sql, cfg)
    if not r.ok:
        pytest.fail(f"{cid} 被拒：{r.rejected_by} {r.reason}")
    tree = sqlglot.parse_one(r.sql, dialect=cfg.dialect)
    join = tree.find(sqlglot.exp.Join)
    assert join is not None and "org_id" in join.sql(), f"{cid}: 谓词必须进 ON"


def test_c11_indirect_tenancy_via_subquery(cfg, monkeypatch):
    """无租户列的表须经 tenant_filter 间接限定。"""
    spec = cfg.tables["documents"]
    for c in spec.columns.values():
        c.tenant = False
    spec.tenant_filter = "{ref}.kb_id IN (SELECT id FROM knowledge_bases WHERE org_id = {ctx})"
    r = _check("SELECT id AS x FROM documents", cfg)
    assert r.ok and "kb_id IN" in r.sql and str(ORG) in r.sql


def test_c12_tenant_exempt_table(cfg):
    cfg.tables["orgs"].tenant_exempt = True
    for c in cfg.tables["orgs"].columns.values():
        c.tenant = False
    r = _check("SELECT id AS x FROM orgs", cfg)
    assert r.ok and not _tenant_preds(r.sql), "豁免表不该被注入"


def test_c13_reject_when_tenancy_unresolvable(cfg):
    spec = cfg.tables["documents"]
    for c in spec.columns.values():
        c.tenant = False
    spec.tenant_filter = None
    spec.tenant_exempt = False
    r = _check("SELECT id AS x FROM documents", cfg)
    assert not r.ok and r.rejected_by == "R-10", "无法确定归属必须拒绝，不能放行"


def test_c14_no_injection_when_tenant_disabled(cfg):
    cfg.raw["tenant"]["enabled"] = False
    r = _check("SELECT id AS x FROM documents", cfg)
    assert r.ok and not _tenant_preds(r.sql)


def test_c15_org_id_zero(cfg):
    """0 是合法租户 ID，不得被当作假值跳过注入。"""
    r = _check("SELECT id AS x FROM documents", cfg, org=0)
    assert r.ok and _tenant_preds(r.sql) == ["0"]


@pytest.mark.parametrize("cid,org", [("C-16a", -1), ("C-16b", 2**63 - 1)])
def test_c16_extreme_org_ids(cid, org, cfg):
    r = _check("SELECT id AS x FROM documents", cfg, org=org)
    assert r.ok and _tenant_preds(r.sql) == [str(org)], cid


def test_c17_non_integer_org_id_never_reaches_sql(cfg):
    """字符串租户 ID 绝不能被拼进 SQL —— 那是注入面。"""
    payload = "1 OR 1=1"
    try:
        r = guard.check("SELECT id AS x FROM documents", cfg,
                        org_id=payload, dialect=cfg.dialect)
    except (ValueError, TypeError):
        return                          # 拒绝也是合格行为
    assert "OR 1=1" not in r.sql, "非法租户值被原样拼进了 SQL"


def test_c18_rewrites_are_visible(cfg):
    r = _check("SELECT id AS x FROM documents", cfg)
    assert r.rewrites and any("租户" in x for x in r.rewrites)


def test_c19_rewritten_sql_is_reparsable(cfg):
    r = _check("SELECT id AS x FROM documents WHERE status = 'FAILED'", cfg)
    sqlglot.parse_one(r.sql, dialect=cfg.dialect)      # 不抛即通过


def test_c20_rewrite_is_idempotent(cfg):
    once = _check("SELECT id AS x FROM documents", cfg)
    twice = _check(once.sql, cfg)
    assert twice.ok
    assert len(_tenant_preds(twice.sql)) == 1, "二次改写不得重复注入"


# ==========================================================================
# R-09 强制 LIMIT
# ==========================================================================

def test_c21_limit_injected(cfg):
    r = _check("SELECT id AS x FROM documents", cfg)
    assert r.ok and f"LIMIT {cfg.max_rows}" in r.sql.upper().replace("  ", " ")


def test_c22_smaller_limit_preserved(cfg):
    r = _check("SELECT id AS x FROM documents LIMIT 10", cfg)
    assert r.ok and re.search(r"LIMIT\s+10\b", r.sql, re.I)


def test_c23_oversized_limit_lowered(cfg):
    r = _check("SELECT id AS x FROM documents LIMIT 999999", cfg)
    assert r.ok and not re.search(r"LIMIT\s+999999", r.sql, re.I)
    assert re.search(rf"LIMIT\s+{cfg.max_rows}\b", r.sql, re.I)


def test_c24_union_gets_single_outer_limit(cfg):
    sql = "SELECT id AS x FROM documents UNION SELECT id FROM knowledge_bases"
    r = _check(sql, cfg)
    assert r.ok
    sqlglot.parse_one(r.sql, dialect=cfg.dialect)      # 语法必须仍然正确
    assert len(re.findall(r"\bLIMIT\b", r.sql, re.I)) == 1, "只应有一个最外层 LIMIT"


def test_c25_cte_shadowing_is_not_injected(cfg):
    """CTE 别名遮蔽同名真实表时，R-10 不得往它身上注入租户谓词。

    线上验收抓到的：`WITH documents AS (SELECT 1 AS x) SELECT x FROM documents`
    被改写成 `... FROM documents WHERE documents.org_id = 65`，
    而 CTE 里根本没有 org_id 列 —— 干跑阶段 Binder Error。

    R-03、R-04 都已认得 CTE 遮蔽（后者是上一轮 BUG-2 修的），R-10 漏了。
    三条规则对"这个名字指谁"的认定必须一致，否则总会在某个组合上炸。
    """
    r = _check("WITH documents AS (SELECT 1 AS x) SELECT x AS y FROM documents", cfg)
    assert r.ok, f"被拒：{r.rejected_by} {r.reason}"
    assert not _tenant_preds(r.sql), f"不该注入：{r.sql}"

    # 但 CTE **内部**引用的真实表仍要注入 —— 不能因为身处 CTE 就放过
    r2 = _check("WITH t AS (SELECT id FROM documents) SELECT id AS x FROM t", cfg)
    assert r2.ok and _tenant_preds(r2.sql) == [str(ORG)], r2.sql
