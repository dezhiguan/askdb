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


# ---------- 环境范围（Q-05 / D-1） ----------

def test_role_scope_is_now_enforced_not_decorative():
    """角色详情第一格从展示字符串变成真判定。

    此前 sources.py 里 env 的注释明写「仅用于界面区分，不参与鉴权」，
    而角色卡上却写着 STAGING —— 页面上唯一一个看起来是真值、实际不成立的
    字段，比纯占位更有误导性。
    """
    def envs(code: str):
        return identity.DEFAULT_POLICIES.get(code, identity.Policy()).envs

    assert envs("QA") == frozenset({"test"})
    assert envs("PRODUCT") == frozenset({"prod_ro"})
    assert envs("DEV") == frozenset({"dev", "test"})
    assert envs("DATA_OWNER") is None                  # 跨全域，不额外收窄
    assert envs("SYS_ADMIN") == frozenset()            # 一个都不给


def test_qa_cannot_reach_the_production_mirror(zcfg, monkeypatch, tmp_path):
    """测试角色连不上生产只读镜像 —— 这是 QA 角色描述里承诺过的那句话。

    表白名单拦不住这个：生产镜像与测试库的表结构往往一模一样，
    SQL 一字不差、数据完全不同。所以必须在选源时判。
    """
    from askdb import sources as S

    src = S.build(name="prod-mirror", type_="duckdb",
                  dsn=str(tmp_path / "p.duckdb"), env="prod_ro")
    monkeypatch.setattr(S, "list_sources", lambda _c: [src])
    monkeypatch.setattr(S, "get_source", lambda _c, sid: src if sid == src.id else None)

    c = _client(zcfg, monkeypatch)

    _as(c, "qa")
    r = c.post("/api/sql", json={"sql": "SELECT 1", "source": src.id})
    assert r.status_code == 403 and "PROD-RO" in r.json()["detail"]
    # 列表里也看不到它：列表本身就是信息
    assert [i["id"] for i in c.get("/api/sources").json()["items"]] == ["builtin"]

    _as(c, "owner")                                   # 数据负责人跨全域
    assert src.id in [i["id"] for i in c.get("/api/sources").json()["items"]]


def test_env_falls_back_to_the_conservative_value(tmp_path):
    """拼错 env 的后果必须是"看得更少"，不能是"看得更多"。"""
    from askdb import sources as S
    src = S.build(name="x", type_="duckdb", dsn=str(tmp_path / "a.duckdb"), env="PRODUCTION")
    assert src.env == "test"


def test_roles_endpoint_exposes_the_effective_envs(zcfg, monkeypatch):
    """前端那一格要读真值，否则又是一处"配了但看不出有没有生效"。"""
    c = _client(zcfg, monkeypatch)
    roles = {r["code"]: r for r in c.get("/api/identity/roles").json()["roles"]}
    assert roles["QA"]["envs"] == ["test"]
    assert roles["DATA_OWNER"]["envs_unrestricted"] is True
