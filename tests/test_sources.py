"""运行时数据源注册表。

这条路径的每一个错误方向都指向同一类事故：服务端按用户填的地址主动建连，
而 askdb 不设账号体系。所以测试钉的不是"功能能用"，而是"关得住、不外泄、
不默认放行"。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from askdb import server, sources


@pytest.fixture
def open_cfg(cfg, tmp_path):
    """开启运行时添加，并把注册表写到临时目录 —— 不污染仓库里的 var/。"""
    cfg.raw["datasources"] = {"allow_runtime_add": True}
    cfg.root = tmp_path
    return cfg


@pytest.fixture
def client(open_cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: open_cfg)
    return TestClient(server.create_app("ignored.yaml"))


def _body(**over):
    return {"name": "样例副本", "type": "duckdb",
            "dsn": str(over.pop("dsn", "")) or "", **over}


# --------------------------------------------------------------- 准入

def test_write_endpoints_are_closed_by_default(cfg, monkeypatch):
    """默认必须是关的。开着等于给出一个无鉴权的出站建连入口。"""
    cfg.raw.pop("datasources", None)
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.get("/api/sources").status_code == 200        # 列表恒可读
    assert c.get("/api/sources").json()["can_add"] is False

    for call in (
        lambda: c.post("/api/sources", json={"type": "duckdb", "dsn": "x"}),
        lambda: c.post("/api/sources/test", json={"type": "duckdb", "dsn": "x"}),
        lambda: c.get("/api/sources/src_000000000000/scan"),
        lambda: c.put("/api/sources/src_000000000000/tables", json={"tables": []}),
        lambda: c.delete("/api/sources/src_000000000000"),
    ):
        assert call().status_code == 403


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

    # 1. 出站建连限流
    assert server._SOURCE_RL.limit <= 10, "出站建连限流被放宽了"
    assert server._SOURCE_RL.window_s >= 60

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
