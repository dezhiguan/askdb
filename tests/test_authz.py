"""鉴权收口：登录门、能力位、审计可见范围、复放重新收窄、MCP 角色。

这个文件覆盖的是《角色与权限设计》阶段一的五件事。它们共同的性质是
**「漏掉的方向必须落在安全的那边」**，所以每条用例都成对写：
既断言"该拦的拦住了"，也断言"该放的还放着" —— 只测前者会把功能测没，
只测后者等于没测。

与 test_auth.py 的分工：那边测"你是谁"（会话票、口令、登录门），
这边测"你能干什么"（能力位与可见范围）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from askdb import audit, auth, identity, server

SECRET = "s" * 40

ACCOUNTS = [
    ("lin", "PRODUCT"),
    ("dev", "DEV"),
    ("qa", "QA"),
    ("owner", "DATA_OWNER"),
    ("root", "SYS_ADMIN"),
]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, SECRET)


@pytest.fixture
def zcfg(cfg):
    cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [
            {"username": u, "roles": [r], "password_hash": auth.hash_password(f"{u}-pw")}
            for u, r in ACCOUNTS
        ],
    }
    return cfg


def _client(zcfg, monkeypatch, *, required: bool = False) -> TestClient:
    zcfg.raw["auth"]["required"] = required
    monkeypatch.setattr(server, "load", lambda _p: zcfg)
    return TestClient(server.create_app("ignored.yaml"))


def _as(client: TestClient, user: str) -> TestClient:
    assert client.post("/api/auth/login",
                       json={"username": user, "password": f"{user}-pw"}).status_code == 200
    return client


# ---------- 能力位模型本身 ----------

def test_system_admin_can_approve_but_never_query():
    """自批在**结构上**不可能发生 —— 这条是把审批收敛到系统管理员的全部理由。

    它不靠流程约定，靠的是 SYS_ADMIN 既没有 QUERY 也没有 QUERY_SQL：
    它永远不可能是查询的发起人，因此不存在"自己批自己"的那条边。
    哪天有人为了图方便给它加上 QUERY，这条用例就会红。
    """
    assert identity.can(["SYS_ADMIN"], identity.APPROVE)
    assert not identity.can(["SYS_ADMIN"], identity.QUERY)
    assert not identity.can(["SYS_ADMIN"], identity.QUERY_SQL)


def test_data_owner_proposes_but_cannot_approve():
    """提出与放行分属两人：数据负责人能改数据源，但批不了自己的申请。"""
    assert identity.can(["DATA_OWNER"], identity.SOURCES_WRITE)
    assert not identity.can(["DATA_OWNER"], identity.APPROVE)


def test_system_admin_sees_audit_metadata_but_not_content():
    """管人的需要知道有没有人在违规访问，不需要知道业务上问了什么。"""
    assert identity.can(["SYS_ADMIN"], identity.AUDIT_ALL)
    assert not identity.can(["SYS_ADMIN"], identity.AUDIT_CONTENT)


def test_roles_add_up():
    """RBAC 是加法：身兼两职拿到两者之和，与 Policy 的语义保持一致。"""
    both = identity.caps_of(["PRODUCT", "QA"])
    assert both >= identity.caps_of(["PRODUCT"])
    assert identity.QUERY_SQL in both          # QA 带进来的
    assert identity.caps_of(["NO_SUCH_ROLE"]) == frozenset()


# ---------- 读门（D-3） ----------

GOVERNANCE = ["/api/audit", "/api/audit/stats", "/api/identity/members?role=DEV",
              "/api/introspect", "/api/schema", "/api/sources"]


def test_required_mode_closes_the_governance_surface(zcfg, monkeypatch):
    """required=true 时治理面整体要登录。

    此前 _require_login 靠逐接口手工调用，全仓只挂在两处，于是 /api/audit
    在要求登录的实例上照样匿名可读 —— 而它一条记录里就有
    user、role、question 三个字段。
    """
    c = _client(zcfg, monkeypatch, required=True)
    for path in GOVERNANCE:
        assert c.get(path).status_code == 401, path

    # 登录后只断言"不再是 401"。断言 200 会把身份库有没有接上（members 未启用
    # 时给 404）也算进这条用例，而这里测的是登录门，不是那些接口各自的可用性。
    _as(c, "dev")
    for path in GOVERNANCE:
        assert c.get(path).status_code != 401, path


def test_health_and_roles_stay_open_even_when_required(zcfg, monkeypatch):
    """白名单是穷举的，但确实要放行 —— 页面加载与登录页自己要靠这几条。

    放行清单收得过紧会让登录页自己白屏，那是比多开一个接口更难查的故障。
    """
    c = _client(zcfg, monkeypatch, required=True)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    assert c.get("/api/identity/roles").status_code == 200


def test_anonymous_instance_keeps_working(zcfg, monkeypatch):
    """required=false 是部署方的明确选择，不能被这道门顺手锁上。

    对外实例的目的就是让人看到护栏、审计与角色收窄；把它锁上等于
    把要展示的东西全挡住。匿名在那里仍是一个**普通角色**，照样受能力位约束。
    """
    c = _client(zcfg, monkeypatch, required=False)
    assert c.get("/api/audit").status_code == 200
    assert c.get("/api/schema").status_code == 200


# ---------- 能力位在接口上（矩阵） ----------

def test_product_cannot_touch_data_sources(zcfg, monkeypatch):
    c = _as(_client(zcfg, monkeypatch), "lin")
    assert c.get("/api/sources").status_code == 200          # 列表能看
    r = c.post("/api/sources/test", json={"type": "duckdb", "dsn": "x"})
    assert r.status_code == 403 and "测试数据源连接" in r.json()["detail"]


def test_qa_can_test_connection_but_not_write(zcfg, monkeypatch):
    c = _as(_client(zcfg, monkeypatch), "qa")
    assert c.post("/api/sources/test",
                  json={"type": "duckdb", "dsn": "x"}).status_code != 403
    r = c.delete("/api/sources/src_whatever")
    assert r.status_code == 403 and "删除数据源" in r.json()["detail"]


def test_refusal_names_the_role_in_plain_language(zcfg, monkeypatch):
    """看到这句话的是业务方，不是读代码的人。

    「PRODUCT lacks sources.test」对他毫无用处，他需要知道的是
    "我这个角色不行"以及"该找谁"。
    """
    c = _as(_client(zcfg, monkeypatch), "lin")
    detail = c.post("/api/sources/test", json={"type": "duckdb", "dsn": "x"}).json()["detail"]
    assert "产品" in detail and "系统管理员" in detail


def test_system_admin_refusal_points_at_the_real_problem(zcfg, monkeypatch):
    """只有系统角色的人查数，要给"你没有数据角色"，而不是"你无权用这个功能"。

    后者会让他去找系统管理员 —— 而他自己就是。这是判定顺序的用例：
    _require_scope 必须排在能力位之前。
    """
    c = _as(_client(zcfg, monkeypatch), "root")
    r = c.post("/api/sql", json={"sql": "SELECT id FROM orgs"})
    assert r.status_code == 403 and "数据访问权限" in r.json()["detail"]


# ---------- 审计可见范围（A-01） ----------

def _seed(path: Path, rows: list[dict]) -> None:
    from askdb.trace import write_audit
    for r in rows:
        write_audit(path, r)


@pytest.fixture
def seeded(zcfg):
    _seed(Path(zcfg.audit_log), [
        {"trace_id": "a" * 12, "ts": "2026-09-05T10:00:00+08:00", "kind": "sql",
         "user": "lin", "role": "PRODUCT", "question": "产品问的", "elapsed_ms": 5},
        {"trace_id": "b" * 12, "ts": "2026-09-05T10:01:00+08:00", "kind": "sql",
         "user": "owner", "role": "DATA_OWNER", "question": "负责人问的", "elapsed_ms": 5},
    ])
    return zcfg


def test_product_sees_only_own_audit_rows(seeded, monkeypatch):
    c = _as(_client(seeded, monkeypatch), "lin")
    body = c.get("/api/audit").json()
    assert body["total"] == 1
    assert [i["question"] for i in body["items"]] == ["产品问的"]


def test_dev_sees_everyone(seeded, monkeypatch):
    c = _as(_client(seeded, monkeypatch), "dev")
    assert c.get("/api/audit").json()["total"] == 2


def test_stats_are_scoped_the_same_way(seeded, monkeypatch):
    """列表只给本人、统计却给全量，那张按天聚合的成本卡就是一次泄露。

    同一道边界只做一半等于没做 —— 这条用例守的就是"两处必须同源"。
    """
    c = _as(_client(seeded, monkeypatch), "lin")          # PRODUCT：无 AUDIT_ALL
    assert c.get("/api/audit").json()["total"] == 1
    assert c.get("/api/audit/stats").json()["calls"] == 1

    c2 = _as(_client(seeded, monkeypatch), "owner")       # DATA_OWNER：有 AUDIT_ALL
    assert c2.get("/api/audit").json()["total"] == 2
    assert c2.get("/api/audit/stats").json()["calls"] == 2


def test_own_role_roster_is_visible_without_the_capability(zcfg, monkeypatch):
    """看自己所属角色的成员不需要 MEMBERS_READ，跨角色才需要。

    一个人有权知道自己和谁同组；完整名册是组织结构，属于治理数据。
    """
    c = _as(_client(zcfg, monkeypatch), "dev")
    assert c.get("/api/identity/members", params={"role": "DEV"}).status_code != 403
    r = c.get("/api/identity/members", params={"role": "DATA_OWNER"})
    assert r.status_code == 403 and "其他角色" in r.json()["detail"]
    assert c.get("/api/identity/members").status_code == 403      # 不带参数 = 全量


def test_system_admin_sees_all_rows_without_the_questions(seeded, monkeypatch):
    """跨用户可见 + 内容不可见，两件事同时成立才是设计要的那个形状。"""
    c = _as(_client(seeded, monkeypatch), "root")
    body = c.get("/api/audit").json()
    assert body["total"] == 2                      # 谁在查，看得到
    assert all(i["question"] is None for i in body["items"])   # 问了什么，看不到
    assert body["text_visible"] is False


def test_search_cannot_reach_other_peoples_rows(seeded, monkeypatch):
    """收敛必须排在搜索之前，否则 total 就是一个预言机。

    先搜后滤的话，搜"负责人"仍会让命中数变化 —— 那已经把内容说出来了。
    """
    c = _as(_client(seeded, monkeypatch), "lin")
    assert c.get("/api/audit", params={"q": "负责人问的"}).json()["total"] == 0


# ---------- 复放按复放者收窄（D-4） ----------

def test_replay_refuses_records_touching_invisible_tables(zcfg, monkeypatch):
    """低权限者拿到 trace_id 也读不到越界记录，且**同为 404**。

    403 与 404 的差别本身就是一位信息：它会告诉对方"这条记录存在"。
    本接口的全部结局必须收敛到同一个响应。
    """
    zcfg.raw["observability"]["replay_api"] = True
    zcfg.raw["role_policies"] = {"PRODUCT": {"tables": ["orgs"]}}
    _seed(Path(zcfg.audit_log), [{
        "trace_id": "c" * 12, "ts": "2026-09-05T10:00:00+08:00", "kind": "sql",
        "user": "owner", "role": "DATA_OWNER", "question": "x",
        "tables_hit": ["documents"], "sql_final": "SELECT 1 FROM documents",
    }])
    monkeypatch.setattr(server, "_REPLAY_RL", server._RateLimit())
    c = _client(zcfg, monkeypatch)

    _as(c, "lin")                                   # PRODUCT：只看得到 orgs
    assert c.get("/api/replay", params={"trace_id": "c" * 12}).status_code == 404

    _as(c, "dev")                                   # DEV：实例白名单全量
    assert c.get("/api/replay", params={"trace_id": "c" * 12}).status_code == 200


def test_replay_is_denied_to_roles_without_the_capability(zcfg, monkeypatch):
    """QA 没有 REPLAY 位，且拿到的同样是 404 而不是 403。"""
    zcfg.raw["observability"]["replay_api"] = True
    _seed(Path(zcfg.audit_log), [{
        "trace_id": "d" * 12, "ts": "2026-09-05T10:00:00+08:00", "kind": "sql",
        "user": "qa", "role": "QA", "question": "x", "tables_hit": [],
    }])
    monkeypatch.setattr(server, "_REPLAY_RL", server._RateLimit())
    c = _as(_client(zcfg, monkeypatch), "qa")
    assert c.get("/api/replay", params={"trace_id": "d" * 12}).status_code == 404


# ---------- MCP 通道接入角色（D-5） ----------

def test_mcp_narrows_by_role(cfg):
    """MCP 曾是唯一一条完全绕开角色的通道：直接吃启动配置，全量白名单。

    这里不起 MCP 服务（那要装 SDK），只断言收窄这一步本身 ——
    build_server 内部就是把这份收窄后的 cfg 交给三个工具。
    """
    cfg.raw["role_policies"] = {"QA": {"tables": ["orgs"], "max_rows": 5}}
    narrowed = identity.for_roles(cfg, ["QA"], user="mcp")
    assert set(narrowed.tables) == {"orgs"}
    assert narrowed.max_rows == 5
    assert narrowed.user == "mcp"        # 审计里那一栏不再是空的
    assert narrowed.role == "QA"


def test_mcp_refuses_unknown_role(cfg):
    """拼错角色码就退回全量，正是这次要消灭的那类静默失效。"""
    with pytest.raises(SystemExit):
        from askdb import mcp_server
        mcp_server.build_server(cfg, role="TYPO")


# ---------- 角色与数据源解绑（2026-09-06 产品决定） ----------

def test_roles_are_not_bound_to_data_sources():
    """角色策略里**不再有环境维度**。

    askdb 是共享平台：大家在同一批数据源上工作，按角色挡"能连哪个库"
    与这个前提冲突。撤的是整条判定，不是留一个不生效的字段 —— 留着它
    就会变成页面上那种"看起来在拦、其实没拦"的东西，而那正是当初加它
    要消灭的。

    收窄面因此只剩表与行数（护栏 R-03 / R-13），加上期限与脱敏。
    """
    assert not hasattr(identity.Policy(), "envs"), "Policy 又长回环境维度了"
    for code in ("QA", "PRODUCT", "DEV", "DATA_OWNER", "SYS_ADMIN"):
        p = identity.DEFAULT_POLICIES.get(code, identity.Policy())
        assert not hasattr(p, "envs")

    # 系统管理员的"没有数据权限"不靠环境实现，靠空表集 —— 解绑不能把它放开
    sys_admin = identity.DEFAULT_POLICIES["SYS_ADMIN"]
    assert sys_admin.tables == frozenset() and sys_admin.max_rows == 0


def test_any_role_can_pick_any_data_source(zcfg, monkeypatch, tmp_path):
    """测试角色照样能选标着 prod_ro 的源，列表里也看得到它。

    这条**推翻了**此前的 test_qa_cannot_reach_the_production_mirror ——
    那是按"角色绑环境"写的，现在的产品口径是共享平台。能不能查出东西
    仍由表白名单与护栏决定，那两层没有放松（下面一并验）。
    """
    from askdb import sources as S

    src = S.build(name="prod-mirror", type_="duckdb",
                  dsn=str(tmp_path / "p.duckdb"), env="prod_ro")
    monkeypatch.setattr(S, "list_sources", lambda _c: [src])
    monkeypatch.setattr(S, "get_source", lambda _c, sid: src if sid == src.id else None)

    c = _client(zcfg, monkeypatch)

    _as(c, "qa")
    # 列表：不再按角色藏卡
    assert src.id in [i["id"] for i in c.get("/api/sources").json()["items"]]
    # 选源：不再因为环境档位被 403 挡回
    r = c.post("/api/sql", json={"sql": "SELECT 1", "source": src.id})
    assert r.status_code != 403 or "环境" not in r.json().get("detail", "")

    _as(c, "owner")
    assert src.id in [i["id"] for i in c.get("/api/sources").json()["items"]]


def test_env_falls_back_to_the_conservative_value(tmp_path):
    """拼错 env 只影响界面标签的取值，仍然要落在保守的那一档。

    解绑之后它不再参与鉴权，但顶栏据它告诉人"当前连的是哪一档" ——
    同一台机器上同时跑多个实例，说错一次就会有人拿着另一个库的结论下判断。
    """
    from askdb import sources as S
    src = S.build(name="x", type_="duckdb", dsn=str(tmp_path / "a.duckdb"), env="PRODUCTION")
    assert src.env == "test"


def test_roles_endpoint_no_longer_advertises_environments(zcfg, monkeypatch):
    """接口不能再吐环境字段：页面据它渲染「环境范围」那一格，
    留着就会继续对人宣称一件不再执行的事。"""
    c = _client(zcfg, monkeypatch)
    roles = {r["code"]: r for r in c.get("/api/identity/roles").json()["roles"]}
    for r in roles.values():
        assert "envs" not in r and "envs_unrestricted" not in r
    # 仍然给的是真值那几格
    assert roles["QA"]["max_age_days"] == 180
    assert roles["DEV"]["unmask"] is True


# ---------- 数据期限（Q-07 / R-19） ----------

def test_window_predicate_is_injected(cfg):
    """时间窗口复用 R-10 的谓词注入，不新造判定。"""
    from askdb import guard

    scoped = identity.for_roles(cfg, ["QA"])          # 内置默认 180 天
    g = guard.check("SELECT id FROM documents", scoped,
                    org_id=scoped.default_org, dialect=scoped.dialect)
    assert g.ok, g.reason
    assert "R-19" in g.rules_fired
    assert "created_at" in g.sql and any("数据期限" in r for r in g.rewrites)


def test_window_does_not_touch_dimension_tables(cfg):
    """维表显式声明 time_exempt，不该被注入 —— 注入了会直接查空。"""
    from askdb import guard

    scoped = identity.for_roles(cfg, ["QA"])
    g = guard.check("SELECT id FROM orgs", scoped,
                    org_id=scoped.default_org, dialect=scoped.dialect)
    assert g.ok, g.reason
    assert "R-19" not in g.rules_fired


def test_undeclared_time_column_is_rejected_not_ignored(cfg):
    """漏标的表要被拒，**不能静默放行**。

    静默放行等于把权限页上"只能看 90 天"那句话变成一句空话，
    而那句话是写给人看的承诺。
    """
    from askdb import guard

    cfg.tables["documents"].columns["created_at"].time = False    # 模拟漏标
    scoped = identity.for_roles(cfg, ["PRODUCT"])
    g = guard.check("SELECT id FROM documents", scoped,
                    org_id=scoped.default_org, dialect=scoped.dialect)
    assert not g.ok and g.rejected_by == "R-19"
    assert "time: true" in g.reason          # 措辞要告诉人怎么修


def test_no_window_role_is_untouched(cfg):
    """开发角色没有期限，SQL 不该被动一个字。"""
    from askdb import guard

    scoped = identity.for_roles(cfg, ["DEV"])
    g = guard.check("SELECT id FROM documents", scoped,
                    org_id=scoped.default_org, dialect=scoped.dialect)
    assert g.ok and "R-19" not in g.rules_fired


def test_window_is_disabled_on_scanned_sources(cfg, tmp_path):
    """运行时源的表结构来自扫描，扫描看不出哪一列该算新旧。

    与租户隔离在那里一律关闭是同一个问题的同一个答案：猜错会**悄悄给出
    错误的数据**，比越权更难发现，因为结果看着正常。
    """
    from askdb import sources as S

    src = S.build(name="x", type_="duckdb", dsn=str(tmp_path / "a.duckdb"))
    derived = S.derive_config(cfg, src)
    assert derived.window_enforceable is False
    scoped = identity.for_roles(derived, ["QA"])
    assert scoped.window_days == 180              # 角色策略照常算出来
    assert scoped.window_enforceable is False     # 但在这个源上落不了地


# ---------- 列级脱敏（Q-06 / P03） ----------

def test_sensitive_columns_are_masked_for_roles_without_unmask():
    from askdb.executor import _masked

    assert _masked("13800001234") == "1*********4"
    assert _masked("张三") == "**"           # 短值整体打星，保留首尾等于原样交出
    assert _masked(12345) == "1***5"          # 与列的存储类型无关


def test_mask_keeps_first_and_last_for_reconciliation():
    """保留首尾是为了让审计与对账还能做，全星会把那件事彻底做不了。"""
    from askdb.executor import _masked

    m = _masked("18565040934")
    assert m.startswith("1") and m.endswith("4") and set(m[1:-1]) == {"*"}


def test_unmask_is_additive_across_roles(cfg):
    """RBAC 是加法：兼任开发的人看得到原值，与 tables 取并集同一个语义。"""
    assert identity.for_roles(cfg, ["QA"]).unmask is False
    assert identity.for_roles(cfg, ["DEV"]).unmask is True
    assert identity.for_roles(cfg, ["QA", "DEV"]).unmask is True


def test_unmask_cannot_be_switched_on_by_config(cfg):
    """脱敏这一位有意不从配置读 —— 能配的东西就会被配错，
    而这一位配错等于把个人信息交出去。"""
    cfg.raw["role_policies"] = {"QA": {"unmask": True}}
    assert identity.policy_for(cfg, "QA").unmask is False


# ---------- 高成本查询审批（Q-08 / P07） ----------

@pytest.fixture
def tight(zcfg):
    """把扫描阈值压到 0，让任何查询都超阈值 —— 这样才测得到挂起。

    压到 1 不够：护栏会先注入租户谓词，DuckDB 据此把 orgs 的预估收到 1 行，
    正好不大于阈值。测阈值行为时要绕开"改写会改变预估"这件事。
    """
    zcfg.raw["guard"] = {**zcfg.raw["guard"], "max_scan_rows": 0}
    return zcfg


def _ask_sql(c, sql, **kw):
    return c.post("/api/sql", json={"sql": sql, **kw}).json()


def test_over_threshold_opens_an_approval_instead_of_dead_ending(tight, monkeypatch):
    """R-11 超阈值不再是终点。

    此前只回一句"缩小时间范围"，对一次性的年度对账来说那是一句无解的话 ——
    需求本身就要扫那么多行。于是人要么放弃，要么绕开 askdb 直接连库，
    而后者正是这套系统要消灭的行为。
    """
    c = _as(_client(tight, monkeypatch), "qa")
    r = _ask_sql(c, "SELECT id FROM orgs")
    assert r["ok"] is False and r["rejected_by"] == "R-11"
    assert r["approval_id"] and r["approval_status"] == "REQUESTED"
    assert "待审批" in r["hint"]


def test_only_system_admin_can_decide(tight, monkeypatch):
    """数据负责人有意批不了：它是数据源变更的提出方，
    兼任放行方会让"提出与放行分属两人"失效。"""
    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]

    _as(c, "owner")                                   # DATA_OWNER
    r = c.post(f"/api/approvals/{aid}/decide", json={"approved": True})
    assert r.status_code == 403 and "审批高成本查询" in r.json()["detail"]

    _as(c, "root")                                    # SYS_ADMIN
    assert c.post(f"/api/approvals/{aid}/decide",
                  json={"approved": True}).status_code == 200


def test_approved_query_runs_once_and_only_once(tight, monkeypatch):
    """放行是一次性的 —— 否则一次审批等于永久豁免。"""
    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]
    _as(c, "root")
    c.post(f"/api/approvals/{aid}/decide", json={"approved": True})

    _as(c, "qa")
    ok = _ask_sql(c, "SELECT id FROM orgs", approval_id=aid)
    assert ok["ok"] is True, ok

    again = c.post("/api/sql", json={"sql": "SELECT id FROM orgs", "approval_id": aid})
    assert again.status_code == 403 and "一次性" in again.json()["detail"]


def test_waiver_is_bound_to_the_exact_request(tight, monkeypatch):
    """拿小查询骗到批准、再用同一个单号跑别的，审批就成了摆设。"""
    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]
    _as(c, "root")
    c.post(f"/api/approvals/{aid}/decide", json={"approved": True})

    _as(c, "qa")
    r = c.post("/api/sql", json={"sql": "SELECT name FROM orgs", "approval_id": aid})
    assert r.status_code == 403 and "不一致" in r.json()["detail"]


def test_waiver_cannot_be_borrowed_by_another_account(tight, monkeypatch):
    """别人批下来的额度不能借用。"""
    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]
    _as(c, "root")
    c.post(f"/api/approvals/{aid}/decide", json={"approved": True})

    _as(c, "dev")
    r = c.post("/api/sql", json={"sql": "SELECT id FROM orgs", "approval_id": aid})
    assert r.status_code == 403 and "不属于当前账号" in r.json()["detail"]


def test_rejection_reason_reaches_the_requester(tight, monkeypatch):
    """驳回时申请人拿到的唯一信息就是那句话，必须传到。"""
    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]
    _as(c, "root")
    c.post(f"/api/approvals/{aid}/decide",
           json={"approved": False, "note": "请改用按月汇总表"})

    _as(c, "qa")
    r = c.post("/api/sql", json={"sql": "SELECT id FROM orgs", "approval_id": aid})
    assert r.status_code == 403 and "请改用按月汇总表" in r.json()["detail"]


def test_queue_visibility_follows_the_audit_rule(tight, monkeypatch):
    """有 APPROVE 的看全部，没有的只看自己提的 —— 与审计同一条口径。

    自己提的必须看得到，否则申请人无从知道批没批，只能反复重试。
    """
    c = _as(_client(tight, monkeypatch), "qa")
    _ask_sql(c, "SELECT id FROM orgs")
    _as(c, "dev")
    _ask_sql(c, "SELECT name FROM orgs")

    mine = c.get("/api/approvals").json()                     # dev 自己的
    assert mine["can_approve"] is False and len(mine["items"]) == 1

    _as(c, "root")
    all_ = c.get("/api/approvals").json()
    assert all_["can_approve"] is True and len(all_["items"]) == 2


def test_decision_is_recorded_with_the_approver(tight, monkeypatch):
    """审批动作独立留痕，并记下审批人看过原文 ——
    那是放开"系统管理员不看查询内容"的代价，代价要能被事后核对。"""
    from askdb import approvals

    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]
    _as(c, "root")
    c.post(f"/api/approvals/{aid}/decide", json={"approved": True, "note": "季度对账"})

    rec = approvals.state(tight)[aid]
    assert rec["approver"] == "root" and rec["note"] == "季度对账"
    assert rec["approver_saw_content"] is True
    assert rec["user"] == "qa"                 # 发起人照旧记着，不被决策覆盖


def test_double_decision_is_refused(tight, monkeypatch):
    """不能重复决策 —— 悄悄覆盖前一个人的结论比拒绝更糟。"""
    c = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(c, "SELECT id FROM orgs")["approval_id"]
    _as(c, "root")
    assert c.post(f"/api/approvals/{aid}/decide", json={"approved": True}).status_code == 200
    r = c.post(f"/api/approvals/{aid}/decide", json={"approved": False})
    assert r.status_code == 409


def test_system_admin_can_never_be_the_requester(tight, monkeypatch):
    """自批在**结构上**不可能：审批人没有查询能力，永远提不出申请。

    这是把审批收敛到系统管理员最主要的收益，用一条端到端用例钉住。
    """
    c = _as(_client(tight, monkeypatch), "root")
    r = c.post("/api/sql", json={"sql": "SELECT id FROM orgs"})
    assert r.status_code == 403                       # 连查询都发不出去
    assert c.get("/api/approvals").json()["items"] == []
