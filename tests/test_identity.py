"""身份与权限：角色定义与写接口的准入。

这一版**只做授权，不做认证** —— 谁是谁交给 auth-gateway。因此在登录接入
之前，写接口没有任何请求方身份可依据，必须靠一把部署方持有的令牌兜底，
而且必须 fail-closed。这个文件里最要紧的就是那几条准入用例：一旦写接口
在没配令牌时也能调，任何能打开页面的人都可以给自己加角色。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from askdb import identity, server


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


@pytest.fixture
def enabled_client(cfg, monkeypatch):
    """把身份功能打开，但不给真实数据库 —— 准入判定发生在碰库之前，
    这几条用例因此不需要 PostgreSQL，CI 上也能跑。"""
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    monkeypatch.setattr(identity, "enabled", lambda _cfg: True)
    return TestClient(server.create_app("ignored.yaml"))


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

def test_write_refused_when_admin_token_not_configured(enabled_client, monkeypatch):
    """没配 ASKDB_ADMIN_TOKEN 就整体拒绝写入。

    fail-closed 是有意的：登录未接入前，开着写接口等于任何能打开页面的人
    都能给自己加角色。
    """
    monkeypatch.delenv("ASKDB_ADMIN_TOKEN", raising=False)
    r = enabled_client.post("/api/identity/members",
                            json={"role_code": "PRODUCT", "username": "x"})
    assert r.status_code == 403
    assert "ASKDB_ADMIN_TOKEN" in r.json()["detail"]


def test_write_refused_with_wrong_token(enabled_client, monkeypatch):
    monkeypatch.setenv("ASKDB_ADMIN_TOKEN", "right")
    r = enabled_client.post("/api/identity/members",
                            headers={"X-Askdb-Admin-Token": "wrong"},
                            json={"role_code": "PRODUCT", "username": "x"})
    assert r.status_code == 401


def test_delete_also_requires_admin_token(enabled_client, monkeypatch):
    """删除和新增一样危险 —— 把人踢出角色同样是越权路径，别只守住新增。"""
    monkeypatch.delenv("ASKDB_ADMIN_TOKEN", raising=False)
    assert enabled_client.delete("/api/identity/members/1").status_code == 403

    monkeypatch.setenv("ASKDB_ADMIN_TOKEN", "right")
    r = enabled_client.delete("/api/identity/members/1",
                              headers={"X-Askdb-Admin-Token": "wrong"})
    assert r.status_code == 401


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
