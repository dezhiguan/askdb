"""运行时数据源注册表。

这条路径的每一个错误方向都指向同一类事故：服务端按用户填的地址主动建连，
而 askdb 不设账号体系。所以测试钉的不是"功能能用"，而是"关得住、不外泄、
不默认放行"。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from askdb import auth, server, sources


@pytest.fixture
def open_cfg(cfg, tmp_path):
    """开启运行时添加，并把注册表写到临时目录 —— 不污染仓库里的 var/。"""
    cfg.raw["datasources"] = {"allow_runtime_add": True}
    cfg.root = tmp_path
    return cfg


@pytest.fixture
def client(open_cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: open_cfg)
    # 写接口从 2026-09-05 起统一要登录（server 里那道写入中间件）。这一组用例
    # 测的是数据源管理本身、不是登录，所以在这里把身份补上。
    # **有意不复用开发配置里的账号** —— 那边改一次口令就要回来改测试，
    # conftest 顶上那条"别跟着开发配置漂"的教训同样适用。
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    open_cfg.raw["auth"] = {
        "enabled": True,
        "required": False,
        "accounts": [{
            "username": "ops", "display_name": "运维", "roles": ["DATA_OWNER"],
            "password_hash": auth.hash_password("ops-pw"),
        }],
    }
    # 限流器是模块级单例（生产上一进程一个 app，这是有意的）。测试里多个
    # 用例共用一个进程，配额会跨用例累积 —— 攒满之后后面的用例全部拿到
    # 429，而报错是 KeyError: 'source' 这种看不出根因的样子。
    # 每个用例发一对干净的限流器。
    monkeypatch.setattr(server, "_SOURCE_DIAL_RL", server._RateLimit(limit=10, window_s=60))
    monkeypatch.setattr(server, "_SOURCE_MANAGE_RL", server._RateLimit(limit=30, window_s=60))
    c = TestClient(server.create_app("ignored.yaml"))
    assert c.post("/api/auth/login",
                  json={"username": "ops", "password": "ops-pw"}).status_code == 200
    return c


def _body(**over):
    return {"name": "样例副本", "type": "duckdb",
            "dsn": str(over.pop("dsn", "")) or "", **over}


# --------------------------------------------------------------- 准入

def test_write_endpoints_are_closed_by_default(cfg, monkeypatch):
    """默认必须是关的，而且**两道门各自独立**。

    未登录撞的是写入中间件（401），登录之后撞的是 allow_runtime_add 开关（403）。
    两条都要钉：只钉前者的话，将来谁把开关默认值改成 true，登录用户就能在
    「本实例不允许运行时加源」的实例上加源，而测试全绿。
    """
    cfg.raw.pop("datasources", None)
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [{"username": "ops", "roles": ["DATA_OWNER"],
                      "password_hash": auth.hash_password("ops-pw")}],
    }
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.get("/api/sources").status_code == 200        # 列表恒可读
    assert c.get("/api/sources").json()["can_add"] is False

    writes = (
        lambda: c.post("/api/sources", json={"type": "duckdb", "dsn": "x"}),
        lambda: c.post("/api/sources/test", json={"type": "duckdb", "dsn": "x"}),
        lambda: c.put("/api/sources/src_000000000000/tables", json={"tables": []}),
        lambda: c.delete("/api/sources/src_000000000000"),
    )

    # 未登录：中间件先拦，连开关是什么状态都问不到
    for call in writes:
        r = call()
        assert r.status_code == 401
        assert r.json()["code"] == "login_required"

    # 扫描是 GET，写入中间件管不着它，但它会让服务端真去连一次库 ——
    # 所以它自己带一条登录判据（server.py 里唯一一个要登录的 GET）
    assert c.get("/api/sources/src_000000000000/scan").status_code == 401

    # 登录之后开关依然挡着。**登录不解锁开关**
    assert c.post("/api/auth/login",
                  json={"username": "ops", "password": "ops-pw"}).status_code == 200
    for call in writes:
        assert call().status_code == 403
    assert c.get("/api/sources/src_000000000000/scan").status_code == 403


def test_public_instance_keeps_its_remaining_guards():
    """对外实例的 allow_runtime_add 于 2026-09-01 按决定放开。

    开关没了之后，挡在「任何人都能让服务器向任意地址建连」前面的只剩两样东西，
    所以这条测试改成钉住它们 —— 一道边界撤了，剩下的两道不能再悄悄消失。
    """
    import os
    from pathlib import Path

    from askdb import server
    from askdb.config import load

    root = Path(__file__).resolve().parent.parent
    assert sources.enabled(load(root / "config" / "public.yaml")) is True

    # 1. 出站建连限流。守的是"地址由调用方给定"那条路 —— 它才是能被拿去
    #    当端口扫描器的那一条，放宽它等于把这道防线拆掉。
    assert server._SOURCE_DIAL_RL.limit <= 10, "出站建连限流被放宽了"
    assert server._SOURCE_DIAL_RL.window_s >= 60

    # 2. 没有主密钥就不接受明文口令
    key = os.environ.pop("ASKDB_SECRET_KEY", None)
    try:
        with pytest.raises(sources.SourceError):
            sources.encrypt_password("x")
    finally:
        if key is not None:
            os.environ["ASKDB_SECRET_KEY"] = key


# --------------------------------------------------------------- 生命周期

def test_new_source_opens_no_table(client, sample_db):
    r = client.post("/api/sources", json=_body(dsn=str(sample_db)))
    assert r.status_code == 201, r.text
    # 扫描只解决"看得见"。默认开放任何一张表，等于把白名单这道边界取消掉
    assert r.json()["source"]["table_count"] == 0
    assert len(r.json()["tables"]) > 0


def test_whitelist_carries_column_types(client, open_cfg, sample_db):
    """白名单必须带字段名与类型 —— R-04（字段真实性）与 R-05（展开 SELECT *）
    靠它判定，缺了会退化成放行。"""
    sid = client.post("/api/sources", json=_body(dsn=str(sample_db))).json()["source"]["id"]
    assert client.put(f"/api/sources/{sid}/tables",
                      json={"tables": ["orgs"]}).status_code == 200

    stored = sources.get_source(open_cfg, sid)
    assert stored is not None
    cols = stored.tables[0]["columns"]
    assert cols and all(spec.get("type") for spec in cols.values())


def test_unknown_table_is_rejected(client, sample_db):
    sid = client.post("/api/sources", json=_body(dsn=str(sample_db))).json()["source"]["id"]
    r = client.put(f"/api/sources/{sid}/tables", json={"tables": ["查无此表"]})
    assert r.status_code == 400 and "查无此表" in r.json()["detail"]


def test_delete_then_gone(client, sample_db):
    sid = client.post("/api/sources", json=_body(dsn=str(sample_db))).json()["source"]["id"]
    assert client.delete(f"/api/sources/{sid}").status_code == 200
    assert client.delete(f"/api/sources/{sid}").status_code == 404


# --------------------------------------------------------------- 不外泄

def test_connection_string_never_leaves_the_server(client, sample_db):
    """dsn 里带主机名与用户名，是内网拓扑信息；口令更不必说。
    列表接口一个字都不该带出去。"""
    client.post("/api/sources", json=_body(dsn=str(sample_db)))
    payload = client.get("/api/sources").json()
    assert str(sample_db) not in client.get("/api/sources").text

    leaky = {"dsn", "password", "password_env", "password_enc"}
    for item in payload["items"]:
        assert not (leaky & set(item)), f"列表接口带出了敏感字段：{leaky & set(item)}"


def test_plaintext_password_needs_a_master_key(monkeypatch):
    monkeypatch.delenv("ASKDB_SECRET_KEY", raising=False)
    with pytest.raises(sources.SourceError, match="ASKDB_SECRET_KEY"):
        sources.encrypt_password("hunter2")


def test_encrypted_password_round_trips(monkeypatch):
    monkeypatch.setenv("ASKDB_SECRET_KEY", "master-key-for-test")
    enc = sources.encrypt_password("hunter2")
    assert "hunter2" not in enc
    src = sources.Source(id="src_000000000000", name="x", type="postgresql",
                         dsn="host=h", password_enc=enc)
    assert sources.resolve_password(src) == "hunter2"
    assert src.credential == "已加密存储"


def test_wrong_master_key_fails_closed(monkeypatch):
    """主密钥换过之后，解不出来要退回 None 由连接层按认证失败报，
    而不是抛一个看不懂的密码学异常把整页打崩。"""
    monkeypatch.setenv("ASKDB_SECRET_KEY", "key-a")
    enc = sources.encrypt_password("hunter2")
    monkeypatch.setenv("ASKDB_SECRET_KEY", "key-b")
    src = sources.Source(id="src_000000000000", name="x", type="postgresql",
                         dsn="host=h", password_enc=enc)
    assert sources.resolve_password(src) is None


# --------------------------------------------------------------- 输入校验

@pytest.mark.parametrize("over, hit", [
    ({"type": "mysql"}, "不支持的数据库类型"),
    ({"name": ""}, "名称不能为空"),
    ({"password_env": "小写不合规"}, "环境变量名不合规"),
    ({"password_env": "OK_ENV", "password": "p"}, "二选一"),
])
def test_build_rejects_bad_input(over, hit):
    kw = {"name": "x", "type_": "duckdb", "dsn": "d.duckdb"}
    kw.update({k if k != "type" else "type_": v for k, v in over.items()})
    with pytest.raises(sources.SourceError, match=hit):
        sources.build(**kw)


# --------------------------------------------------------------- 派生

def test_derived_config_disables_tenancy(cfg):
    """一次结构扫描看不出哪一列代表租户，更看不出间接归属。
    猜错的后果是越权，所以如实按单租户处理。"""
    src = sources.build(name="x", type_="duckdb", dsn="d.duckdb")
    derived = sources.derive_config(cfg, src)
    assert derived.raw["tenant"]["enabled"] is False
    assert derived.raw["guard"] == cfg.raw["guard"]      # 护栏阈值是部署策略，跟着走


# --------------------------------------------------------------- 按源查询

def test_query_against_source_without_tables_is_refused(client, sample_db):
    """0 张开放表的源查不出任何东西。与其让人查完撞 R-03，不如当场说清楚。"""
    sid = client.post("/api/sources", json=_body(dsn=str(sample_db))).json()["source"]["id"]
    r = client.post("/api/sql", json={"sql": "SELECT 1", "source": sid})
    assert r.status_code == 400 and "没有开放任何表" in r.json()["detail"]


def test_query_against_unknown_source_is_404(client):
    r = client.post("/api/sql", json={"sql": "SELECT 1", "source": "src_000000000000"})
    assert r.status_code == 404


def test_query_is_isolated_to_the_chosen_source(client, open_cfg, sample_db):
    """选了源就只能看见那个源的白名单。

    换源换掉的是整份配置（白名单、方言、连接），不是加一个过滤条件 ——
    所以内置源上开放的表，在新源上必须照样查不到。
    """
    sid = client.post("/api/sources", json=_body(dsn=str(sample_db))).json()["source"]["id"]
    client.put(f"/api/sources/{sid}/tables", json={"tables": ["orgs"]})

    ok = client.post("/api/sql", json={"sql": "SELECT id FROM orgs", "source": sid}).json()
    assert ok["ok"], ok

    # documents 在内置配置里开放，但这个源只开了 orgs
    blocked = client.post("/api/sql", json={"sql": "SELECT id FROM documents", "source": sid}).json()
    assert not blocked["ok"] and blocked["rejected_by"] == "R-03"


def test_audit_records_which_source_was_queried(client, sample_db):
    """多源之后，审计不记数据源就说不清「这条 SQL 打的哪个库」——
    而那是审计存在的全部意义。"""
    sid = client.post("/api/sources", json=_body(dsn=str(sample_db))).json()["source"]["id"]
    client.put(f"/api/sources/{sid}/tables", json={"tables": ["orgs"]})
    client.post("/api/sql", json={"sql": "SELECT id FROM orgs", "source": sid})
    client.post("/api/sql", json={"sql": "SELECT id FROM documents"})

    items = client.get("/api/audit?page=1&page_size=5").json()["items"]
    got = {i["source"] for i in items if i.get("source")}
    assert sid in got, "按源查询没有记下数据源"
    assert "builtin" in got, "内置源的调用没有记成 builtin"


def test_scan_needs_login_because_it_dials_out(open_cfg, monkeypatch):
    """扫描不改任何东西，但每调一次服务端就真去连一次那个库。

    写入中间件只管 POST/PUT/PATCH/DELETE，罩不住一个会向外建连的 GET，
    所以它自己带判据。这条用例存在的意义是：将来谁把这条判据删了，
    「未登录可反复触发出站建连」会立刻被测出来，而不是等被人拿去当扫描器。
    """
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    open_cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [{"username": "ops", "roles": ["DATA_OWNER"],
                      "password_hash": auth.hash_password("ops-pw")}],
    }
    monkeypatch.setattr(server, "load", lambda _p: open_cfg)
    monkeypatch.setattr(server, "_SOURCE_DIAL_RL", server._RateLimit(limit=10, window_s=60))
    monkeypatch.setattr(server, "_SOURCE_MANAGE_RL", server._RateLimit(limit=30, window_s=60))
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.get("/api/sources/nope/scan").status_code == 401
    # 列表仍然匿名可读 —— 收紧的只是"让服务端去连一次"这个动作
    assert c.get("/api/sources").status_code == 200

