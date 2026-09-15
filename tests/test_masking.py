"""脱敏（P03）：判定落在 AST 上，别名改不动它。

2026-09-06 的实测事故：模型写 `SELECT phone AS 手机号`，脱敏按**返回列名**
匹配敏感列，"手机号"对不上 phone，整层静默失效，匿名访客拿到明文手机号。
这批用例锁的就是"改个别名就绕过"这条路。
"""

from __future__ import annotations

from askdb import guard
from askdb.config import Column, looks_sensitive


def test_column_name_alone_marks_sensitive():
    """结构扫描生成的白名单没人来标 sensitive —— 列名必须自己算数。"""
    assert Column(name="phone", type="VARCHAR").sensitive
    assert Column(name="userEmail", type="VARCHAR").sensitive
    assert Column(name="password_hash", type="VARCHAR").sensitive
    assert Column(name="id_card", type="VARCHAR").sensitive


def test_config_cannot_switch_masking_off():
    """`sensitive: false` 摘不掉 phone —— 只能往上加，不能往下摘。"""
    assert Column(name="phone", type="VARCHAR", sensitive=False).sensitive


def test_ordinary_columns_are_not_swept_in():
    """误伤多了就会有人来关它，关掉的那一刻这层防护就没了。"""
    for name in ("platform_role", "telemetry_id", "translated_at",
                 "username", "display_name", "status", "created_at"):
        assert not looks_sensitive(name), name


def test_alias_does_not_escape_masking(cfg):
    sql = "SELECT file_name AS 文件名, id AS 编号 FROM documents"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_count_is_not_masked(cfg):
    """COUNT 不暴露值本身，脱它只会把数字毁掉。"""
    sql = "SELECT COUNT(file_name) AS 篇数 FROM documents"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == set()


def test_min_max_is_masked(cfg):
    """MIN/MAX 原样吐出某一行的真值 —— 与 COUNT 不是一回事。"""
    sql = "SELECT MAX(file_name) AS 最后一个 FROM documents"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_expression_over_sensitive_column_is_masked(cfg):
    sql = "SELECT SUBSTR(file_name, 1, 4) AS 前缀 FROM documents"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_cte_carries_sensitivity_through(cfg):
    sql = ("WITH t AS (SELECT file_name AS f, id FROM documents) "
           "SELECT f AS 名称, id FROM t")
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_subquery_carries_sensitivity_through(cfg):
    sql = "SELECT x.f FROM (SELECT file_name AS f FROM documents) x"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_unresolvable_sql_returns_none(cfg):
    """解析不出必须如实说不知道，由调用方从严处理。"""
    assert guard.sensitive_output_columns("SELECT * FROM documents", cfg, "duckdb") is None


# ---------------------------------------------------------------------------
# 集合运算：分支按位置对齐。
#
# 2026-09-15 线上 trace af565a7a7074 第 07 步：一条三段 UNION ALL 的枚举探查
# 被 P03 从严拒答，白烧一轮决策。原判定只拆一层（sides = [this, expression]），
# 而 A∪B∪C 解析出来是 Union(Union(A,B), C) —— 于是任何数据源上任何三段及以上
# 的集合运算都必然被拒，与它碰没碰个人信息无关。
# ---------------------------------------------------------------------------


def test_two_branch_union_aligns_by_position(cfg):
    """两分支原来就是对的，一并锁住，免得递归改动把它带坏。"""
    sql = ("SELECT file_name, id FROM documents "
           "UNION ALL SELECT file_name, id FROM documents")
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_three_branch_union_is_resolved(cfg):
    """三分支：这条正是线上被拒的那个形状。"""
    sql = ("SELECT file_name, id FROM documents "
           "UNION ALL SELECT file_name, id FROM documents "
           "UNION ALL SELECT file_name, id FROM documents")
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_union_masks_position_sensitive_in_any_branch(cfg):
    """任一分支敏感则该位置敏感 —— 只看第一支就会漏掉后面那支的明文。"""
    sql = ("SELECT id AS a, id AS b FROM documents "
           "UNION ALL SELECT id AS a, id AS b FROM documents "
           "UNION ALL SELECT id AS a, file_name AS b FROM documents")
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {1}


def test_parenthesised_union_branches_are_unwrapped(cfg):
    """带括号的写法每支裹了一层 Subquery，不拆就又退回"解析不出"。"""
    sql = ("(SELECT file_name FROM documents) "
           "UNION ALL (SELECT file_name FROM documents) "
           "UNION ALL (SELECT file_name FROM documents)")
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_except_is_resolved(cfg):
    """EXCEPT / INTERSECT 与 UNION 同属集合运算，判定不该因类名不同而失效。"""
    sql = "SELECT file_name FROM documents EXCEPT SELECT file_name FROM documents"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") == {0}


def test_union_with_unresolvable_branch_returns_none(cfg):
    """一支拆不开就整条说不知道 —— 从严的方向不因为分支多了而松动。"""
    sql = "SELECT file_name FROM documents UNION ALL SELECT * FROM documents"
    assert guard.sensitive_output_columns(sql, cfg, "duckdb") is None


def test_executor_masks_aliased_column(ex):
    """端到端：真库、真 SQL、真别名。"""
    r = ex.run("SELECT file_name AS 文件名 FROM documents LIMIT 3")
    assert r.rows, "样例库里得有数据，否则这条用例什么也没验证"
    for row in r.rows:
        assert "*" in str(row[0]), row
    assert r.masked_columns == ["文件名"]
    assert not r.mask_degraded


def test_executor_keeps_ordinary_columns_intact(ex):
    r = ex.run("SELECT id AS 编号 FROM documents LIMIT 3")
    assert r.masked_columns == []
    for row in r.rows:
        assert "*" not in str(row[0])


def test_direct_sql_reports_masked_columns(cfg, monkeypatch):
    """直查这条路也要给出脱敏字段 —— 它才是最容易一次拉出整表的那条路。

    2026-09-07 实测：/api/sql 的返回体里没有 masked_columns / mask_degraded，
    值脱了、界面却说不出是谁脱的；审计里同样没记，事后无从证明脱没脱。
    """
    import copy

    from fastapi.testclient import TestClient

    from askdb import server

    c = copy.deepcopy(cfg)
    c.raw = copy.deepcopy(cfg.raw)
    monkeypatch.setattr(server, "load", lambda _p: c)
    client = TestClient(server.create_app("ignored.yaml"))

    r = client.post("/api/sql", json={"sql": "SELECT file_name FROM documents LIMIT 3"}).json()
    assert r["ok"] is True, r
    assert r["masked_columns"] == ["file_name"]
    assert r["mask_degraded"] is False
    assert all("*" in str(row[0]) for row in r["rows"]), r["rows"]
