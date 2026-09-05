"""身份与权限：角色定义与写接口的准入。

认证交给 auth-gateway，授权归 askdb。登录接入后，成员写入的准入是
**角色优先、令牌兜底**：系统管理员按角色直接放行，令牌降为 break-glass
（身份库出问题、没人登得进来时运维还能改回去）。两条路都不通时必须
fail-closed —— 一旦写接口在两者皆无时也能调，任何登录用户都可以给自己加角色。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from askdb import auth, identity, server


def _signed_in(cfg, monkeypatch):
    """已登录的 client。

    写接口外面还有一道写入中间件（未登录一律 401），而这一组用例测的是
    **里面那道门**：身份库没启用给 404、没配管理员令牌给 403。不先进门就
    永远够不着它们，测出来的全是外面那道 401。
    """
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [{"username": "ops", "roles": ["SYS_ADMIN"],
                      "password_hash": auth.hash_password("ops-pw")}],
    }
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    c = TestClient(server.create_app("ignored.yaml"))
    assert c.post("/api/auth/login",
                  json={"username": "ops", "password": "ops-pw"}).status_code == 200
    return c


@pytest.fixture
def client(cfg, monkeypatch):
    return _signed_in(cfg, monkeypatch)


def _signed_in_as(cfg, monkeypatch, role: str):
    """以指定角色登录的 client。

    成员写入的准入自 2026-09 起是**角色优先、令牌兜底**：系统管理员按角色直接放行，
    其他角色仍然只能靠部署方令牌。要把这两条路分别测到，就需要一个"角色不够"的人。
    """
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, "t" * 40)
    cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [{"username": "dev", "roles": [role],
                      "password_hash": auth.hash_password("dev-pw")}],
    }
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    monkeypatch.setattr(identity, "enabled", lambda _cfg: True)
    c = TestClient(server.create_app("ignored.yaml"))
    assert c.post("/api/auth/login",
                  json={"username": "dev", "password": "dev-pw"}).status_code == 200
    return c


@pytest.fixture
def non_admin_client(cfg, monkeypatch):
    return _signed_in_as(cfg, monkeypatch, "DEV")


@pytest.fixture
def enabled_client(cfg, monkeypatch):
    """把身份功能打开，但不给真实数据库 —— 准入判定发生在碰库之前，
    这几条用例因此不需要 PostgreSQL，CI 上也能跑。"""
    monkeypatch.setattr(identity, "enabled", lambda _cfg: True)
    return _signed_in(cfg, monkeypatch)


# ---------- 角色定义 ----------

def test_roles_are_fixed_and_cover_the_agreed_set():
    codes = [r.code for r in identity.ROLES]
    assert codes == ["PRODUCT", "DEV", "QA", "DATA_OWNER", "SYS_ADMIN"]
    assert len(set(codes)) == len(codes)


def test_system_admin_gets_no_data_scope():
    """职责分离：管人的不自动获得看数据的权限。

    两者合一，管理员就能给自己开任意数据权限而不留痕 —— 这类越权
    在审计里看起来完全合规，是最难发现的一种。
    """
    admin = identity.ROLE_BY_CODE["SYS_ADMIN"]
    assert admin.system is True
    assert admin.scope == "SYSTEM"

    data_scopes = {r.scope for r in identity.ROLES if not r.system}
    assert admin.scope not in data_scopes


# ---------- 未启用时的行为 ----------

def test_roles_endpoint_answers_even_when_disabled(client):
    """角色定义写在源码里，不是秘密。未启用时也要给 200 ——
    否则前端分不清「本实例没开」和「接口坏了」。"""
    r = client.get("/api/identity/roles")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert len(body["roles"]) == 5


def test_member_endpoints_404_when_disabled(client):
    assert client.get("/api/identity/members").status_code == 404
    assert client.post("/api/identity/members",
                       json={"role_code": "PRODUCT", "username": "x"}).status_code == 404


# ---------- 写接口准入 ----------

def test_write_refused_without_role_and_without_token(non_admin_client, monkeypatch):
    """角色不够、又没配令牌，就整体拒绝写入。

    fail-closed 是有意的：这两条路都不通时，开着写接口等于任何登录用户
    都能给自己加角色。措辞里要同时点出两条出路，否则排查的人不知道该走哪边。
    """
    monkeypatch.delenv("ASKDB_ADMIN_TOKEN", raising=False)
    r = non_admin_client.post("/api/identity/members",
                              json={"role_code": "PRODUCT", "username": "x"})
    assert r.status_code == 403
    assert "系统管理员" in r.json()["detail"]
    assert "ASKDB_ADMIN_TOKEN" in r.json()["detail"]


def test_write_refused_with_wrong_token(non_admin_client, monkeypatch):
    monkeypatch.setenv("ASKDB_ADMIN_TOKEN", "right")
    r = non_admin_client.post("/api/identity/members",
                              headers={"X-Askdb-Admin-Token": "wrong"},
                              json={"role_code": "PRODUCT", "username": "x"})
    assert r.status_code == 401


def test_delete_also_refused_without_role_or_token(non_admin_client, monkeypatch):
    """删除和新增一样危险 —— 把人踢出角色同样是越权路径，别只守住新增。"""
    monkeypatch.delenv("ASKDB_ADMIN_TOKEN", raising=False)
    assert non_admin_client.delete("/api/identity/members/1").status_code == 403

    monkeypatch.setenv("ASKDB_ADMIN_TOKEN", "right")
    r = non_admin_client.delete("/api/identity/members/1",
                                headers={"X-Askdb-Admin-Token": "wrong"})
    assert r.status_code == 401


def test_system_admin_writes_without_any_token(enabled_client, monkeypatch):
    """系统管理员按**角色**就能改成员，不需要令牌（设计文档 I-03）。

    这是令牌定位的转折点：它从"唯一依据"降为 break-glass。令牌是共享的，
    记不下是谁改的，而成员变更恰恰最需要留痕 —— 日常路径必须走角色。

    这里断言的是"准入这一关过了"：身份库没接真库，因此后面必然撞上
    IdentityDisabled 转成的 404。**不是 403** 就说明角色这条路是通的。
    """
    monkeypatch.delenv("ASKDB_ADMIN_TOKEN", raising=False)
    r = enabled_client.post("/api/identity/members",
                            json={"role_code": "PRODUCT", "username": "x"})
    assert r.status_code == 404


def test_writable_flag_reflects_token_presence(enabled_client, monkeypatch):
    """前端据此决定表单显示还是置灰。它必须跟真实准入条件同源，
    否则会出现「表单能填、提交才 403」。"""
    # writable 只取决于令牌配没配，与身份库连不连得上无关；这里把计数短路掉，
    # 用例才不需要一个真实的 PostgreSQL
    monkeypatch.setattr(identity, "roles_with_counts", lambda _cfg: [])

    monkeypatch.delenv("ASKDB_ADMIN_TOKEN", raising=False)
    assert enabled_client.get("/api/identity/roles").json()["writable"] is False

    monkeypatch.setenv("ASKDB_ADMIN_TOKEN", "t")
    assert enabled_client.get("/api/identity/roles").json()["writable"] is True


# ---------- 配置边界 ----------

def test_public_instance_never_enables_identity():
    """对外开放实例无法区分调用方。身份功能一旦在那里打开，
    写接口就只剩一把共享令牌挡着 —— 那不是给公网用的。
    """
    from pathlib import Path

    from askdb.config import load

    root = Path(__file__).resolve().parent.parent
    c = load(root / "config" / "public.yaml")
    assert not identity.enabled(c), "对外实例不得启用身份与权限"


def test_add_member_rejects_unknown_role(cfg, monkeypatch):
    monkeypatch.setattr(identity, "enabled", lambda _cfg: True)
    with pytest.raises(identity.IdentityError, match="未知角色"):
        identity.add_member(cfg, role_code="NOPE", username="x")


def test_add_member_rejects_blank_username(cfg, monkeypatch):
    monkeypatch.setattr(identity, "enabled", lambda _cfg: True)
    with pytest.raises(identity.IdentityError, match="用户名不能为空"):
        identity.add_member(cfg, role_code="PRODUCT", username="   ")


def test_dev_config_carries_no_machine_specific_dsn():
    """仓库里的开发配置不能写死某台机器的库和账号。

    写死了，别人克隆下来页面就是 503，而症状（接口 503）和原因（配置指向
    一个不存在的库）之间没有任何线索。连接串走 ASKDB_IDENTITY_DSN。
    """
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config" / "askdb.yaml").read_text(encoding="utf-8"))
    section = raw.get("identity") or {}
    assert not section.get("dsn"), "identity.dsn 不该写在仓库配置里，用 ASKDB_IDENTITY_DSN"


def test_identity_off_when_env_dsn_missing(cfg, monkeypatch):
    """开关为真但没给连接串时功能自动关闭，页面显示「未启用」而不是报错 ——
    身份不是跑通 askdb 的必要条件，缺它不该让人以为服务坏了。
    """
    monkeypatch.delenv(identity.DSN_ENV, raising=False)
    cfg.raw["identity"] = {"enabled": True}
    assert identity.enabled(cfg) is False


def test_identity_on_when_env_dsn_present(cfg, monkeypatch):
    monkeypatch.setenv(identity.DSN_ENV, "host=127.0.0.1 dbname=x user=y")
    cfg.raw["identity"] = {"enabled": True}
    assert identity.enabled(cfg) is True
