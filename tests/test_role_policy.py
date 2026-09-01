"""角色策略：收窄语义与提权面。

整个角色机制只有一件事：**按角色生成一份收窄的配置**，护栏一行不改。
所以这个文件里最要紧的不是"某个角色能看几张表"，而是那条不变量 ——
角色层永远只能收窄，任何配置写法都不可能让人看到实例白名单之外的东西。

不变量一旦破了，权限模型就从"边界"变成"建议"，而这种破法通常没有症状：
多看见一张表不会报错，只会静悄悄地返回本不该返回的数据。
"""

from __future__ import annotations

from askdb import identity


def _spec(cfg, role, **spec):
    cfg.raw["role_policies"] = {role: spec}
    return identity.narrow(cfg, identity.policy_for(cfg, role))


# ---------- 不变量：只能收窄 ----------

def test_role_cannot_widen_table_whitelist(cfg):
    """配置里给一张实例白名单之外的表，必须无效 —— 交集，不是并集。"""
    outside = "document_chunks"          # 真实存在但**有意不开放**：130 万行、存正文
    assert outside not in cfg.tables

    narrowed = _spec(cfg, "DEV", tables=[*cfg.tables, outside])
    assert outside not in narrowed.tables
    assert set(narrowed.tables) == set(cfg.tables)


def test_role_cannot_raise_row_cap(cfg):
    """角色写一个比实例更大的行上限，必须取小。"""
    instance_cap = cfg.max_rows
    narrowed = _spec(cfg, "DEV", max_rows=instance_cap * 100)
    assert narrowed.max_rows == instance_cap


def test_role_can_lower_row_cap(cfg):
    narrowed = _spec(cfg, "QA", max_rows=5)
    assert narrowed.max_rows == 5


def test_role_can_narrow_tables(cfg):
    keep = sorted(cfg.tables)[0]
    narrowed = _spec(cfg, "QA", tables=[keep])
    assert set(narrowed.tables) == {keep}


# ---------- 内置默认 ----------

def test_system_admin_has_no_data_access_by_default(cfg):
    """职责分离是内置默认，不是配置项 —— 忘了配也不会漏。"""
    cfg.raw.pop("role_policies", None)
    narrowed = identity.narrow(cfg, identity.policy_for(cfg, "SYS_ADMIN"))
    assert narrowed.tables == {}
    assert narrowed.max_rows == 0


def test_config_cannot_grant_data_access_to_system_admin(cfg):
    """即便配置试图给系统角色开表，也必须无效 —— 内置默认与配置取交集。"""
    narrowed = _spec(cfg, "SYS_ADMIN", tables=list(cfg.tables), max_rows=999)
    assert narrowed.tables == {}
    assert narrowed.max_rows == 0


def test_unconfigured_role_changes_nothing(cfg):
    """没配策略的角色 = 实例默认。

    接入角色机制不该悄悄改变任何现有实例的可查范围；要收窄必须是显式决定。
    """
    cfg.raw.pop("role_policies", None)
    narrowed = identity.narrow(cfg, identity.policy_for(cfg, identity.ANONYMOUS))
    assert set(narrowed.tables) == set(cfg.tables)
    assert narrowed.max_rows == cfg.max_rows


# ---------- 与护栏的衔接 ----------

def test_narrowed_config_makes_guard_reject_hidden_table(cfg):
    """收窄之后，护栏自己就会拦下不可见的表 —— 不需要在 R-03 里加任何角色分支。"""
    from askdb import guard

    keep = "knowledge_bases"
    hidden = "documents"
    assert {keep, hidden} <= set(cfg.tables)

    narrowed = _spec(cfg, "QA", tables=[keep])
    result = guard.check(f"SELECT * FROM {hidden}", narrowed, org_id=65)
    assert not result.ok and result.rejected_by == "R-03"

    # 同一条 SQL 在实例配置下是放行的 —— 差别只来自收窄
    assert guard.check(f"SELECT * FROM {hidden}", cfg, org_id=65).ok


def test_metrics_referencing_hidden_tables_are_dropped(cfg):
    """口径引用了不可见的表就一并摘掉。

    留着会让模型照口径写出引用不可见表的 SQL，然后被 R-03 拦下 ——
    报错指向一个用户完全无法理解的地方。
    """
    scoped = [m for m in cfg.metrics if m.scope]
    assert scoped, "样例配置里应当有带 scope 的口径"
    table = scoped[0].scope[0]

    narrowed = _spec(cfg, "QA", tables=[t for t in cfg.tables if t != table])
    assert all(table not in m.scope for m in narrowed.metrics)


def test_narrowing_does_not_mutate_the_instance_config(cfg):
    """收窄必须返回新配置。就地改会让下一次调用继承上一次的角色 ——
    这种串号在并发下才暴露，且表现为"偶尔查不到数据"。
    """
    before_tables = set(cfg.tables)
    before_cap = cfg.max_rows

    _spec(cfg, "QA", tables=[sorted(cfg.tables)[0]], max_rows=1)

    assert set(cfg.tables) == before_tables
    assert cfg.max_rows == before_cap


# ---------- 两条查询链路都要收窄 ----------

def _client(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from askdb import server

    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_direct_sql_path_enforces_role(cfg, monkeypatch):
    """直查绕过模型，但**不绕过权限**。

    这条路径最容易被漏掉：它不经过图、不调模型，改权限时很自然地只想着 /api/ask。
    漏掉它就是一条无声的提权路径 —— 页面上还是那个查询框，换个入口就绕开了角色。
    """
    cfg.raw["role_policies"] = {identity.ANONYMOUS: {"tables": ["knowledge_bases"]}}
    client = _client(cfg, monkeypatch)

    r = client.post("/api/sql", json={"sql": "SELECT * FROM documents"}).json()
    assert r["ok"] is False and r["rejected_by"] == "R-03"

    # 角色可见的表照常放行 —— 收窄不是把功能关掉
    ok = client.post("/api/sql", json={"sql": "SELECT id FROM knowledge_bases"}).json()
    assert ok["ok"] is True


def test_ask_path_receives_narrowed_config(cfg, monkeypatch):
    """问答链路拿到的必须是收窄后的配置，且带上角色标记。"""
    from askdb import server

    cfg.raw["role_policies"] = {identity.ANONYMOUS: {"tables": ["knowledge_bases"], "max_rows": 7}}
    seen = {}

    def _capture(question, config, org_id=None, **kw):
        seen["tables"] = set(config.tables)
        seen["max_rows"] = config.max_rows
        seen["role"] = config.role
        raise RuntimeError("stop")           # 只验入参，不跑真链路

    monkeypatch.setattr(server, "run_ask", _capture)
    client = _client(cfg, monkeypatch)
    try:
        client.post("/api/ask", json={"question": "有多少知识库"})
    except RuntimeError:
        pass

    assert seen["tables"] == {"knowledge_bases"}
    assert seen["max_rows"] == 7
    assert seen["role"] == identity.ANONYMOUS


def test_audit_records_the_role(cfg, monkeypatch):
    """同一个问题在不同角色下拿到不同行数，事后必须解释得了是谁的可见范围。"""
    from askdb import audit

    cfg.raw["role_policies"] = {identity.ANONYMOUS: {"tables": ["knowledge_bases"]}}
    client = _client(cfg, monkeypatch)
    client.post("/api/sql", json={"sql": "SELECT id FROM knowledge_bases"})

    items = audit.list_audits(cfg.audit_log)["items"]
    assert items and items[0]["role"] == identity.ANONYMOUS


def test_old_records_without_role_are_marked_not_recorded(cfg):
    """角色是后加的字段。老记录没有它，要如实标"未记录" ——
    默认成 ANONYMOUS 会把历史调用说成匿名发起的，那是编造。
    """
    import json

    from askdb import audit

    cfg.audit_log.parent.mkdir(parents=True, exist_ok=True)
    cfg.audit_log.write_text(
        json.dumps({"trace_id": "aaaaaaaaaaa1", "ts": "2026-01-01T00:00:00+08:00",
                    "question": "老记录"}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    item = audit.list_audits(cfg.audit_log)["items"][0]
    assert item["role"] == "（未记录）"
