"""登录与会话。

范围有意很小：固定账号、无注册、无找回、无短信。所以这里测的不是
"认证系统对不对"，而是几条**一旦破了就说不清**的性质：票不能伪造、
错误提示不能泄露账号是否存在、一键体验不能变成授权旁路。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from askdb import auth, server

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, SECRET)


@pytest.fixture
def acfg(cfg):
    """带三个账号的配置：一个可一键体验，一个仅口令，一个只有系统角色。"""
    cfg.raw["auth"] = {
        "enabled": True,
        "required": False,
        "accounts": [
            {"username": "demo", "display_name": "体验", "roles": ["QA"],
             "demo": True, "password_hash": auth.hash_password("demo-pw")},
            {"username": "alice", "display_name": "Alice", "roles": ["PRODUCT"],
             "password_hash": auth.hash_password("alice-pw")},
            {"username": "root", "display_name": "管理员", "roles": ["SYS_ADMIN"],
             "password_hash": auth.hash_password("root-pw")},
        ],
    }
    cfg.raw["role_policies"] = {
        "ANONYMOUS": {"tables": ["orgs"]},
        "QA": {"tables": ["orgs", "knowledge_bases"]},
        "PRODUCT": {"tables": ["orgs", "knowledge_bases", "documents"]},
    }
    return cfg


@pytest.fixture
def client(acfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    return TestClient(server.create_app("ignored.yaml"))


# ---------- 口令 ----------

def test_password_roundtrip():
    h = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("hunter3", h)


def test_same_password_hashes_differently():
    """每次带独立盐 —— 否则相同口令的两个账号在配置里一眼可见。"""
    assert auth.hash_password("x") != auth.hash_password("x")


@pytest.mark.parametrize("broken", [
    "", "notascheme", "scrypt$1$2$3", "scrypt$16384$8$1$@@@$@@@",
    "scrypt$16384$8$1$aGk=$zzz",          # 尾串填充不合法
    "bcrypt$16384$8$1$aGk=$aGk=",
])
def test_malformed_hash_never_raises(broken):
    """配置里的哈希写坏必须表现为「登不上」，不能变成 500。

    实际踩过：CLI 用 rich 打印哈希，被终端宽度折行，粘进配置就是截断的，
    登录接口直接 500 并把栈暴露出去。
    """
    assert auth.verify_password("anything", broken) is False


# ---------- 会话票 ----------

def test_session_roundtrip():
    assert auth.read(auth.issue("alice", 60)) == "alice"


def test_tampered_session_rejected():
    """改用户名必须重新签名 —— 否则任何人都能把自己签成任意账号。"""
    import base64

    forged = base64.urlsafe_b64encode(
        f"root|{int(time.time()) + 60}".encode()).rstrip(b"=").decode()
    sig = auth.issue("alice", 60).split(".", 1)[1]
    assert auth.read(f"{forged}.{sig}") is None


def test_expired_session_rejected():
    assert auth.read(auth.issue("alice", -1)) is None


def test_session_unavailable_without_secret(monkeypatch):
    """没配密钥就整体关闭，不自动生成 —— 自动生成会让每个副本各签各的，
    表现是「刷新几次就掉线」，比登不上更难查。"""
    monkeypatch.delenv(auth.SESSION_SECRET_ENV, raising=False)
    assert auth.session_available() is False
    assert auth.read("anything") is None


# ---------- 接口 ----------

def test_login_success_sets_httponly_cookie(client):
    r = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    # HttpOnly 挡 JS 读取，SameSite 挡跨站携带 —— 两个都掉了才是问题，缺一个也是
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_wrong_password_and_unknown_user_are_indistinguishable(client):
    """区分「账号不存在」与「口令不对」就是账号枚举。状态码与提示语都要一致。"""
    a = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    b = client.post("/api/auth/login", json={"username": "nobody", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


def test_demo_entry_only_for_whitelisted_accounts(client):
    """一键体验是白名单，不是「允许免密」的总开关。"""
    assert client.post("/api/auth/demo", json={"username": "demo"}).status_code == 200
    # alice 有口令但没标 demo
    assert client.post("/api/auth/demo", json={"username": "alice"}).status_code == 403
    assert client.post("/api/auth/demo", json={"username": "nobody"}).status_code == 403


def test_demo_entry_skips_authentication_not_authorization(client):
    """免密进来的账号，拿到的仍是它自己角色的收窄配置。"""
    client.post("/api/auth/demo", json={"username": "demo"})
    me = client.get("/api/auth/me").json()
    assert me["roles"] == ["QA"]
    assert set(me["scope"]["tables"]) == {"orgs", "knowledge_bases"}

    r = client.post("/api/sql", json={"sql": "SELECT id FROM documents"}).json()
    assert r["ok"] is False and r["rejected_by"] == "R-03"


def test_logout_clears_session(client):
    client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    assert client.get("/api/auth/me").json()["username"] == "alice"
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["username"] is None


# ---------- 授权衔接 ----------

def test_login_widens_scope_versus_anonymous(client):
    """登录必须看得出差别 —— 看不出差别的登录就是个摆设。"""
    anon = client.get("/api/auth/me").json()["scope"]
    assert anon["tables"] == ["orgs"]

    client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    named = client.get("/api/auth/me").json()["scope"]
    assert set(named["tables"]) > set(anon["tables"])


def test_system_admin_only_user_gets_a_clear_refusal(client):
    """只有系统角色的人查数，要给一句能懂的话。

    否则他会撞上 R-03「用到了没有开放的表」—— 那句措辞是给"表没开放"准备的，
    用在"你没有数据角色"上会把人引向完全错误的排查方向。
    """
    client.post("/api/auth/login", json={"username": "root", "password": "root-pw"})
    r = client.post("/api/sql", json={"sql": "SELECT id FROM orgs"})
    assert r.status_code == 403
    assert "数据访问权限" in r.json()["detail"]


def test_required_mode_rejects_anonymous(acfg, monkeypatch):
    acfg.raw["auth"]["required"] = True
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.post("/api/sql", json={"sql": "SELECT id FROM orgs"}).status_code == 401
    c.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    assert c.post("/api/sql", json={"sql": "SELECT id FROM orgs"}).status_code == 200


def test_login_disabled_without_secret(acfg, monkeypatch):
    """密钥没配时登录接口整体 404，而不是让人填了口令才发现签不出票。"""
    monkeypatch.delenv(auth.SESSION_SECRET_ENV, raising=False)
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.post("/api/auth/login",
                  json={"username": "alice", "password": "alice-pw"}).status_code == 404
    assert c.get("/api/auth/me").json()["enabled"] is False


# ---------- 演示实例的配置意图 ----------

def test_anonymous_access_only_survives_on_a_synthetic_database():
    """匿名可查与"连的是什么库"必须绑在一起判，不能各自漂。

    这条原来断言 required 恒为 false，理由是：站里要给人看的是护栏与审计，
    登录页是访客流失最大的一处。那个理由成立的前提是**库里是合成数据**。
    2026-09-03 对外实例改连 ragforge 生产主库，前提没了。

    所以不是把断言翻个面，而是把规则写进去：连真实库就必须强制登录。
    将来若有人把某个实例改回样例库，匿名可查会自动重新变得合法 ——
    规则跟着事实走，不用再改一次测试。
    """
    from pathlib import Path

    from askdb.config import load

    root = Path(__file__).resolve().parent.parent
    c = load(root / "config" / "public.yaml")
    required = bool((c.raw.get("auth") or {}).get("required"))

    synthetic = c.db_type == "duckdb" and c.raw["datasource"].get("path", "").endswith("sample.duckdb")
    if synthetic:
        assert not required, "连合成样例库时不必强制登录 —— 登录页会白挡掉访客"
    else:
        assert required, (
            f"这个实例连的是真实库（{c.db_type}），必须强制登录 —— "
            f"库里是真数据，至少要让调用方在审计里有名有姓"
        )


def test_public_instance_stores_no_plaintext_password():
    """配置里只放哈希。口令写在简历上没关系，写进版本库不行。"""
    from pathlib import Path

    from askdb.config import load

    root = Path(__file__).resolve().parent.parent
    c = load(root / "config" / "public.yaml")
    for spec in (c.raw.get("auth") or {}).get("accounts") or []:
        assert "password" not in spec, f"{spec.get('username')} 配了明文口令"
        assert str(spec.get("password_hash", "")).startswith("scrypt$")
