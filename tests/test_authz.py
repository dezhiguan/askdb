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

def test_all_roles_have_exactly_the_same_capabilities():
    """整套权限模型的主张就是这一条（2026-09-06 产品决定）。

    四个数据角色的能力位**逐位相同**。这条用例存在的意义是挡住"给某个角色
    单独加一位"这种改动 —— 那正是上一版权限体系长成一张解释不清的分档表的
    起点，而它最后的形态是：产品角色登录之后看到的东西比匿名还少。
    """
    base = identity.caps_of(["PRODUCT"])
    for code in ("DEV", "QA", "DATA_OWNER"):
        assert identity.caps_of([code]) == base, code
    assert base                                    # 不是"都为空"这种退化的相等


def test_system_admin_differs_only_by_approve_and_member_writes():
    """唯一的角色差别。多出来的两位各有各的理由，别再多第三位。

    · APPROVE      —— 提出与放行分属两人（V1.1 决定）
    · MEMBERS_WRITE —— 它是上一条的前提：谁能改成员名单，谁就能把自己加进
      系统管理员，于是"只有系统管理员能审批"变成一次点击的距离
    """
    extra = identity.caps_of(["SYS_ADMIN"]) - identity.caps_of(["PRODUCT"])
    assert extra == {identity.APPROVE, identity.MEMBERS_WRITE}


def test_system_admin_can_query_like_everyone_else():
    """2026-09-06 起系统管理员也能查数。

    它换掉的是一条结构性保证（查不到数据 → 不可能是发起人 → 自批不可能），
    补上的是 approvals.decide 里的显式判定。两者一起改的，别只改一半。
    """
    assert identity.can(["SYS_ADMIN"], identity.QUERY)
    assert identity.can(["SYS_ADMIN"], identity.QUERY_SQL)


def test_data_owner_proposes_but_cannot_approve():
    """提出与放行分属两人：数据负责人能改数据源，但批不了自己的申请。"""
    assert identity.can(["DATA_OWNER"], identity.SOURCES_WRITE)
    assert not identity.can(["DATA_OWNER"], identity.APPROVE)


def test_audit_surface_is_the_same_for_everyone():
    """审计可见范围不再按角色分。

    原来系统管理员看得到行、看不到问题原文（职责分离），产品与测试只看得到
    自己那几行。可见面统一之后这些差别全部消失 —— 匿名本来就有 AUDIT_ALL 与
    AUDIT_CONTENT，留着那些差别的实际效果只是"登录反而看得更少"。
    """
    for code in ("PRODUCT", "DEV", "QA", "DATA_OWNER", "SYS_ADMIN", identity.ANONYMOUS):
        caps = identity.caps_of([code])
        assert identity.AUDIT_ALL in caps, code
        assert identity.AUDIT_CONTENT in caps, code


def test_anonymous_reads_everything_and_writes_nothing():
    """未登录可读不可写，落在能力位上就是这个形状。

    注意这里**不是**安全边界：真正拦住未登录写操作的是 server._gate_writes
    中间件（按 HTTP 方法拦，新增接口默认落在安全那边）。能力位在这里只是让
    页面能提前把按钮置灰。两处都要在，少哪一处都不对：只有中间件，用户会
    点完才知道做不了；只有能力位，新增一个写接口就是敞开的。
    """
    anon = identity.caps_of([identity.ANONYMOUS])
    logged_in = identity.caps_of(["PRODUCT"])

    # 读类：与登录用户**一位不差**
    assert anon == logged_in - {identity.SOURCES_TEST, identity.SOURCES_SCAN,
                                identity.SOURCES_WRITE}
    # 写类：一位都没有
    assert identity.SOURCES_WRITE not in anon
    assert identity.APPROVE not in anon
    assert identity.MEMBERS_WRITE not in anon
    # 读类里那几个"看起来敏感"的位确实给了匿名 —— 这是对外实例要展示的东西
    for cap in (identity.AUDIT_CONTENT, identity.REPLAY, identity.QUALITY_READ):
        assert cap in anon


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

def test_data_source_surface_is_open_to_every_logged_in_role(zcfg, monkeypatch,
                                                             sources_store):
    """数据源的读与写对所有登录角色一视同仁。

    原来这里是三档（产品连测试连接都不行、测试能测不能删、开发全开），
    档位之间没有业务依据，只有一句"看起来该这样"。
    """
    for user in ("lin", "qa", "owner"):
        c = _as(_client(zcfg, monkeypatch), user)
        assert c.get("/api/sources").status_code == 200, user
        # 403 是能力位的拒绝，这里要断言的就是"不再因为角色被拒"。
        # 连接失败（400/422）是另一回事，与本用例无关。
        assert c.post("/api/sources/test",
                      json={"type": "duckdb", "dsn": "x"}).status_code != 403, user
        assert c.delete("/api/sources/src_whatever").status_code != 403, user


def test_refusal_names_the_role_in_plain_language(zcfg, monkeypatch):
    """看到这句话的是业务方，不是读代码的人。

    「PRODUCT lacks approve」对他毫无用处，他需要知道的是
    "我这个角色不行"以及"该找谁"。

    用审批举例是因为它现在是**唯一**一个会撞上角色拒绝的动作。
    """
    c = _as(_client(zcfg, monkeypatch), "lin")
    r = c.post("/api/approvals/aaaaaaaaaaaa/decide", json={"approved": True})
    detail = r.json()["detail"]
    assert r.status_code == 403
    assert "产品" in detail and "系统管理员" in detail


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


def test_every_role_sees_every_audit_row(seeded, monkeypatch):
    """审计流水对所有人一样，包括未登录。

    原来产品与测试只看得到自己那几行。取消这条差别是产品决定的一部分；
    要注意它同时取消的还有"登录反而看得更少"这个后果 —— 匿名一直都有
    AUDIT_ALL。
    """
    for user in ("lin", "dev", "qa", "owner", "root"):
        c = _as(_client(seeded, monkeypatch), user)
        assert c.get("/api/audit").json()["total"] == 2, user

    anon = _client(seeded, monkeypatch)
    assert anon.get("/api/audit").json()["total"] == 2


def test_stats_are_scoped_the_same_way(seeded, monkeypatch):
    """列表与统计必须同源。

    可见范围现在人人相同，这条用例守的仍是原来那件事：两处若各判各的，
    哪天再收窄一次列表而漏掉统计，那张按天聚合的成本卡就是一次泄露。
    """
    for user in ("lin", "owner"):
        c = _as(_client(seeded, monkeypatch), user)
        assert c.get("/api/audit").json()["total"] == 2, user
        assert c.get("/api/audit/stats").json()["calls"] == 2, user


def test_roster_is_readable_by_every_role(zcfg, monkeypatch):
    """成员名册对所有角色可读；**增删**仍然只有系统管理员。

    读写在这里是分开的两件事，别一起放开：名单决定谁属于哪个角色，能改它
    就能把自己加进系统管理员，于是"只有系统管理员能审批"不再是一条约束。
    """
    c = _as(_client(zcfg, monkeypatch), "dev")
    for params in ({"role": "DEV"}, {"role": "DATA_OWNER"}, {}):
        assert c.get("/api/identity/members", params=params).status_code != 403, params

    assert not identity.can(["DEV"], identity.MEMBERS_WRITE)
    # 端到端也验一次。这个实例没接身份库，写入会先撞上 404 —— 断言"不成功"
    # 而不是断言某个具体码，否则这条用例测的就变成了身份库有没有接上。
    r = c.post("/api/identity/members", json={"role_code": "SYS_ADMIN", "username": "dev"})
    assert r.status_code not in (200, 201), "任何登录用户都不该能把自己加进系统管理员"


def test_system_admin_sees_audit_content_like_everyone_else(seeded, monkeypatch):
    """系统管理员现在也看得到问题原文。

    原来它只看得到元数据（"管人的不需要知道业务上问了什么"）。可见面统一
    之后这条差别没有了 —— 保留它的唯一效果会是：同一份审计，管理员看到的
    比任何一个未登录访客还少。
    """
    c = _as(_client(seeded, monkeypatch), "root")
    body = c.get("/api/audit").json()
    assert body["total"] == 2
    assert body["text_visible"] is True
    assert sorted(i["question"] for i in body["items"]) == ["产品问的", "负责人问的"]


def test_search_runs_over_the_same_rows_that_are_listed(seeded, monkeypatch):
    """搜索的范围必须与列表的范围同源。

    可见范围人人相同之后，这条不再是"搜不到别人的"，而是"搜的就是列出来的
    那些"。守的是同一个性质：收敛与搜索一旦分成两处各判各的，total 就会变成
    一个能问出可见面之外内容的预言机。
    """
    c = _as(_client(seeded, monkeypatch), "lin")
    assert c.get("/api/audit", params={"q": "负责人问的"}).json()["total"] == 1
    assert c.get("/api/audit", params={"q": "查无此问"}).json()["total"] == 0


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


def test_replay_is_open_to_every_role(zcfg, monkeypatch):
    """复放不再按角色分（原来 QA 与产品没有 REPLAY 位）。

    真正拦住复放的那道门仍在，且与角色无关：**按复放者本人可见的表重新收窄**
    （见上一条用例），越界记录一律 404 —— 403 与 404 的差别本身就会告诉对方
    "这条记录存在"。
    """
    zcfg.raw["observability"]["replay_api"] = True
    _seed(Path(zcfg.audit_log), [{
        "trace_id": "d" * 12, "ts": "2026-09-05T10:00:00+08:00", "kind": "sql",
        "user": "qa", "role": "QA", "question": "x", "tables_hit": [],
        "sql_final": "SELECT 1",
    }])
    for user in ("qa", "lin", "root"):
        monkeypatch.setattr(server, "_REPLAY_RL", server._RateLimit())
        c = _as(_client(zcfg, monkeypatch), user)
        assert c.get("/api/replay", params={"trace_id": "d" * 12}).status_code == 200, user


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

    收窄面因此只剩表与行数（护栏 R-03 / R-13）与期限。
    """
    assert not hasattr(identity.Policy(), "envs"), "Policy 又长回环境维度了"
    for code in ("QA", "PRODUCT", "DEV", "DATA_OWNER", "SYS_ADMIN"):
        p = identity.DEFAULT_POLICIES.get(code, identity.Policy())
        assert not hasattr(p, "envs")


def test_no_role_narrows_anything_by_default():
    """内置默认一条收窄都不写（2026-09-06 产品决定）。

    这条与 test_all_roles_have_exactly_the_same_capabilities 是一对：那条守
    能力位（进不进得了功能），这条守可见面（查不查得到数据）。两条都在，
    "所有角色看到的内容完全一样"才是被测住的，而不是一句注释。

    连系统管理员的空表集也撤了 —— 它换来的"自批在结构上不可能"改由
    approvals.decide 的显式判定接手，见 test_requester_cannot_approve_own_query。
    """
    assert identity.DEFAULT_POLICIES == {}
    assert not hasattr(identity.Policy(), "unmask"), "unmask 又长回来了：脱敏不该可关"


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
    # 也不能再吐脱敏字段：脱敏 2026-09-06 起对所有人无条件生效，
    # 给一个按角色取值的字段就是在暗示它可调
    for r in roles.values():
        assert "unmask" not in r
    # 数据期限那一格仍是真值，只是内置默认对每个角色都是"不限"
    assert all(r["max_age_days"] is None for r in roles.values())


# ---------- 数据期限（Q-07 / R-19） ----------

def test_window_predicate_is_injected(cfg):
    """时间窗口复用 R-10 的谓词注入，不新造判定。"""
    from askdb import guard

    # 内置默认不再设窗口（所有角色一致），所以这里显式配一档 ——
    # 测的是 R-19 这条机制本身，它仍然要能被部署方配起来
    cfg.raw["role_policies"] = {"QA": {"max_age_days": 180}}
    scoped = identity.for_roles(cfg, ["QA"])
    g = guard.check("SELECT id FROM documents", scoped,
                    org_id=scoped.default_org, dialect=scoped.dialect)
    assert g.ok, g.reason
    assert "R-19" in g.rules_fired
    assert "created_at" in g.sql and any("数据期限" in r for r in g.rewrites)


def test_window_does_not_touch_dimension_tables(cfg):
    """维表显式声明 time_exempt，不该被注入 —— 注入了会直接查空。"""
    from askdb import guard

    cfg.raw["role_policies"] = {"QA": {"max_age_days": 180}}
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
    cfg.raw["role_policies"] = {"PRODUCT": {"max_age_days": 90}}
    scoped = identity.for_roles(cfg, ["PRODUCT"])
    g = guard.check("SELECT id FROM documents", scoped,
                    org_id=scoped.default_org, dialect=scoped.dialect)
    assert not g.ok and g.rejected_by == "R-19"
    assert "time: true" in g.reason          # 措辞要告诉人怎么修


def test_no_window_role_is_untouched(cfg):
    """没配期限的角色（内置默认下就是全部角色），SQL 不该被动一个字。"""
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
    derived.raw["role_policies"] = {"QA": {"max_age_days": 180}}
    scoped = identity.for_roles(derived, ["QA"])
    assert scoped.window_days == 180              # 角色策略照常算出来
    assert scoped.window_enforceable is False     # 但在这个源上落不了地


# ---------- 列级脱敏（Q-06 / P03） ----------

def test_sensitive_columns_are_masked():
    from askdb.executor import _masked

    assert _masked("13800001234") == "1*********4"
    assert _masked("张三") == "**"           # 短值整体打星，保留首尾等于原样交出
    assert _masked(12345) == "1***5"          # 与列的存储类型无关


def test_mask_keeps_first_and_last_for_reconciliation():
    """保留首尾是为了让审计与对账还能做，全星会把那件事彻底做不了。"""
    from askdb.executor import _masked

    m = _masked("18565040934")
    assert m.startswith("1") and m.endswith("4") and set(m[1:-1]) == {"*"}


def test_masking_cannot_be_turned_off_by_anyone(cfg):
    """脱敏对**所有角色**无条件生效，且没有任何开关能关掉它。

    原来开发与数据负责人有 unmask（看原值），配置写不了、只能改代码。
    2026-09-06 连这条角色差别也撤了：字段从 Policy 上删除，执行器里那个
    `if cfg.unmask: return rows` 的出口一并删掉 —— 留一个恒为假的分支，
    下一个人只要把它改成真就整片放开了。
    """
    assert not hasattr(identity.Policy(), "unmask")
    for code in ("QA", "DEV", "DATA_OWNER", "SYS_ADMIN", identity.ANONYMOUS):
        scoped = identity.for_roles(cfg, [code])
        assert not hasattr(scoped, "unmask"), code

    # 配置也塞不进来：这个键根本没有读它的代码
    cfg.raw["role_policies"] = {"QA": {"unmask": True}}
    assert not hasattr(identity.policy_for(cfg, "QA"), "unmask")


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


def test_requester_cannot_approve_own_query(tight, monkeypatch):
    """自批被显式拒绝。

    **这条用例接替的是一条结构性保证。** 2026-09-06 之前系统管理员一张表都
    查不到，因此永远提不出申请，自批在结构上不可能发生，不需要判。现在它也
    能查数了，同一个人既能开出审批单又是唯一有 APPROVE 的角色 —— 挡住这件事
    的只剩 approvals.decide 里那一行。它被删掉或被重构掉的时候，红的必须是
    这条用例。
    """
    c = _as(_client(tight, monkeypatch), "root")
    r = c.post("/api/sql", json={"sql": "SELECT id FROM orgs"}).json()
    assert r["ok"] is False and r["approval_id"], "系统管理员现在也该能提出申请"

    decide = c.post(f"/api/approvals/{r['approval_id']}/decide", json={"approved": True})
    assert decide.status_code == 403
    assert "自己" in decide.json()["detail"]

    # 而别人发起的那一条，它照批不误 —— 只测拒绝会把功能测没
    other = _as(_client(tight, monkeypatch), "qa")
    aid = _ask_sql(other, "SELECT id FROM orgs")["approval_id"]
    root = _as(_client(tight, monkeypatch), "root")
    assert root.post(f"/api/approvals/{aid}/decide",
                     json={"approved": True}).status_code == 200
