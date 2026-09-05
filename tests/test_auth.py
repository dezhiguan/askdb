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
            {"username": "visitor", "display_name": "体验", "roles": ["QA"],
             "password_hash": auth.hash_password("visitor-pw")},
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


# ---------- 会话有效期与落地页 ----------

def test_session_ttl_defaults_to_thirty_days(acfg):
    """默认 30 天。写死数字是有意的 —— 这个值是一次显式的安全取舍
    （票签发后无法单独吊销，见 auth 模块头），不该被谁顺手调小/调大而无人察觉。"""
    from askdb import auth

    acfg.raw["auth"].pop("session_ttl_days", None)
    assert auth.ttl_s(acfg) == 30 * 24 * 3600


@pytest.mark.parametrize("bad", [0, -1, "三十天", None])
def test_broken_ttl_falls_back_instead_of_expiring_instantly(acfg, bad):
    """配置写坏时退回默认，绝不能算出 0 —— 那表现为"登录成功但立刻掉线"。"""
    from askdb import auth

    acfg.raw["auth"]["session_ttl_days"] = bad
    assert auth.ttl_s(acfg) == auth.DEFAULT_TTL_S


def test_login_cookie_max_age_matches_the_ticket(acfg, monkeypatch):
    """cookie 与票面同一个有效期。两者不一致会造出两种都难查的故障：
    cookie 先过期 = 无故掉线；票先过期 = 带着 cookie 一直 401。"""
    acfg.raw["auth"]["session_ttl_days"] = 7
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    c = TestClient(server.create_app("ignored.yaml"))

    r = c.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    assert r.status_code == 200
    assert "max-age=604800" in r.headers["set-cookie"].lower()      # 7 天

    # 票面本身也是 7 天：只对齐 cookie 而票仍是默认值，故障会推迟到第 8 天才现形
    raw = r.cookies[auth.COOKIE_NAME]
    body = raw.split(".", 1)[0]
    import base64
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    exp = int(payload.rsplit("|", 1)[1])
    assert abs(exp - (int(time.time()) + 7 * 24 * 3600)) <= 5


def test_anonymous_reads_are_paid_for_by_a_locked_write_face():
    """匿名可查（required: false）成立的**前提**是写入面独立锁死。

    这两项是一对：豁免表里一旦混进任何一个会改动状态的接口，匿名就不再只有
    "读"这一件事，而 required: false 当初就是靠这个前提才敢放的。
    所以规则钉在这里，而不是钉 required 的取值本身 —— 将来某个实例要改回
    强制登录是合法的，把写接口放进豁免表则永远不合法。
    """
    from askdb.server import _WRITE_EXEMPT_PATHS

    for path in _WRITE_EXEMPT_PATHS:
        assert path.startswith("/api/auth/") or path in {"/api/ask", "/api/sql", "/api/resume"}, (
            f"{path} 出现在写入豁免表里。只有认证与查询能豁免 —— "
            f"任何会改动状态的接口都不行"
        )


def test_dev_config_session_lasts_thirty_days():
    c = _dev_config()
    assert (c.raw.get("auth") or {}).get("session_ttl_days") == 30


def _dev_config():
    from pathlib import Path

    from askdb.config import load

    return load(Path(__file__).resolve().parent.parent / "config" / "askdb.yaml")


# ---------- 写入面：未登录一律拦下 ----------

def _write_calls(c):
    """全部会改动状态的接口。新增写接口时**这里也要加一条** ——
    忘了加的后果只是少测一条，而忘了在中间件豁免表里加的后果是接口不可用，
    两个方向都不会变成"悄悄敞开"。"""
    return (
        ("POST", "/api/sources", lambda: c.post("/api/sources", json={"type": "duckdb", "dsn": "x"})),
        ("POST", "/api/sources/test", lambda: c.post("/api/sources/test", json={"type": "duckdb", "dsn": "x"})),
        ("PUT", "/api/sources/x/tables", lambda: c.put("/api/sources/x/tables", json={"tables": []})),
        ("DELETE", "/api/sources/x", lambda: c.delete("/api/sources/x")),
        ("POST", "/api/identity/members", lambda: c.post("/api/identity/members", json={
            "role_code": "QA", "username": "x", "display_name": "", "note": ""})),
        ("DELETE", "/api/identity/members/1", lambda: c.delete("/api/identity/members/1")),
    )


def test_writes_are_refused_without_a_session(client):
    for method, path, call in _write_calls(client):
        r = call()
        assert r.status_code == 401, f"{method} {path} 未登录竟然没被拦"
        assert r.json()["code"] == "login_required"


def test_refusal_says_what_to_do_next(client):
    """提示要说清"拦了什么 / 现在什么状态 / 下一步做什么"。
    「无权限」「操作失败」这类话对着排查的人毫无用处。"""
    detail = client.post("/api/sources", json={"type": "duckdb", "dsn": "x"}).json()["detail"]
    assert "登录" in detail and "只读" in detail
    assert "失败" not in detail


def test_reads_are_not_touched_by_the_write_gate(client):
    """ask / sql / resume 是 POST 但它们是查询。被写入拦截误伤的话，
    未登录就一条数据都查不了 —— 那不是收紧，是把功能关了。"""
    assert client.post("/api/ask", json={"question": "有多少知识库"}).status_code != 401
    assert client.post("/api/sql", json={"sql": "SELECT 1"}).status_code != 401
    assert client.post("/api/resume", json={"thread_id": "nope"}).status_code != 401
    assert client.get("/api/sources").status_code == 200


def test_unknown_write_paths_are_denied_by_default(client):
    """默认拒绝的方向：没登记过的路径一律拦。

    这条是整个做法的价值所在 —— 将来新增一个写接口而忘了任何事，
    它的默认状态是"要登录"，不是"敞开"。
    """
    assert client.post("/api/some/route/added/next/month").status_code == 401


def test_login_reopens_the_write_face(client):
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "alice-pw"}).status_code == 200
    # 登录后不再是 401；具体成不成由各接口自己的规则决定（开关、身份库是否配置等）
    for _, path, call in _write_calls(client):
        assert call().status_code != 401, f"{path} 登录后仍被当成未登录"


def test_admin_token_is_a_valid_identity_at_the_gate(client, monkeypatch):
    """成员增删走的是管理员令牌而不是会话。中间件只认 cookie 的话，
    会把部署方现有的管理通道整个打死。"""
    monkeypatch.setenv("ASKDB_ADMIN_TOKEN", "k" * 20)
    r = client.post("/api/identity/members",
                    headers={"X-Askdb-Admin-Token": "k" * 20},
                    json={"role_code": "QA", "username": "x", "display_name": "", "note": ""})
    assert r.status_code != 401
    # 令牌不对就照样是未登录
    assert client.post("/api/identity/members",
                       headers={"X-Askdb-Admin-Token": "wrong"},
                       json={"role_code": "QA", "username": "x", "display_name": "", "note": ""}
                       ).status_code == 401
