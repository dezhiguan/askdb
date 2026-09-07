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


def test_system_admin_can_query_like_everyone_else(client):
    """系统管理员查数不再被拒（2026-09-06 产品决定）。

    这条用例原来断言的是相反的事：只有系统角色的人查数会拿到一句
    「当前角色没有数据访问权限」。可见面统一之后它和其他角色一样能查 ——
    留着原来那条会把一个已经撤掉的承诺继续钉在测试里。
    """
    client.post("/api/auth/login", json={"username": "root", "password": "root-pw"})
    r = client.post("/api/sql", json={"sql": "SELECT id FROM orgs"})
    assert r.status_code == 200, r.text


def test_required_mode_rejects_anonymous(acfg, monkeypatch):
    acfg.raw["auth"]["required"] = True
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.post("/api/sql", json={"sql": "SELECT id FROM orgs"}).status_code == 401
    # 用 visitor（QA）而不是 alice（PRODUCT）：直查是 QA 有、PRODUCT 没有的
    # 能力位，拿 alice 测会把"登录门"和"能力位"两件事混在一条断言里。
    c.post("/api/auth/login", json={"username": "visitor", "password": "visitor-pw"})
    assert c.post("/api/sql", json={"sql": "SELECT id FROM orgs"}).status_code == 200


def test_direct_sql_is_open_to_every_role(client):
    """直查对所有角色开放（2026-09-06 产品决定）。

    原来产品角色没有 QUERY_SQL，理由是"直查绕开业务口径层，而口径归口正是
    产品角色的职责"。那是一条职责收敛，不是安全判定 —— 而它的实际效果是
    产品经理登录之后比未登录的访客还少一条路（匿名一直有 QUERY_SQL）。
    """
    client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    assert client.get("/api/schema").status_code == 200
    assert client.post("/api/sql", json={"sql": "SELECT id FROM orgs"}).status_code == 200


def test_login_disabled_without_secret(acfg, monkeypatch):
    """密钥没配时登录接口整体 404，而不是让人填了口令才发现签不出票。"""
    monkeypatch.delenv(auth.SESSION_SECRET_ENV, raising=False)
    monkeypatch.setattr(server, "load", lambda _p: acfg)
    c = TestClient(server.create_app("ignored.yaml"))

    assert c.post("/api/auth/login",
                  json={"username": "alice", "password": "alice-pw"}).status_code == 404
    assert c.get("/api/auth/me").json()["enabled"] is False


# ---------- 演示实例的配置意图 ----------

def test_anonymous_read_on_a_real_database_keeps_its_replacement_boundaries():
    """匿名可读与"连的是什么库"必须绑在一起判，不能各自漂。

    这条断言翻过三次，每次都是**产品取舍**改了，不是安全判据松了：
      · 2026-09-03 实例从合成样例库改连 ragforge 生产主库，同日 required 改
        为 true，这条测试随之写成"连真实库就必须强制登录"。
      · 2026-09-07 按 @guandezhi 决定 required 改回 false：登录页是访客流失
        最大的一处，而对外实例存在的意义就是让人不登录也能把整条链路走一遍。
        同日撤掉内置数据源、改走运行时注册表后一度又改回 true。
      · 当日最后按 @guandezhi 决定回到 false 并就此定下。

    原来这条钉的是**翻转必须成对**：放开匿名读，就得拿得出接替登录的那几层
    （只读库账号 + 双层租户隔离）。那两条断言现在**已经删掉**，删的理由必须
    写在这里，否则下次读到的人会以为它们还在挡：

      · 内置 datasource 段撤了，配置里根本没有 dsn 可断言 —— 只读库账号改由
        运行时源各自持有，askdb_ro / careermate_ro 的授权在 scripts/ 的建库
        脚本里，配置文件管不着。
      · 租户隔离在运行时源上是**关的**（sources.derive_config 写死），
        所以"双层隔离"这条断言不是被放宽，是它描述的东西不存在了。

    于是这条测试现在只剩下配置层面还成立的那几样。**行级边界确实没有了** ——
    匿名可读的实际范围是 ragforge 全部组织 + careermate 生产库全部数据。
    要收窄，改的是运行时源的表白名单或给它配回租户列，不在这个文件里。
    """
    from pathlib import Path

    from askdb.config import load

    root = Path(__file__).resolve().parent.parent
    c = load(root / "config" / "public.yaml")
    required = bool((c.raw.get("auth") or {}).get("required"))

    ds = c.raw.get("datasource") or {}
    synthetic = c.db_type == "duckdb" and ds.get("path", "").endswith("sample.duckdb")
    if required or synthetic:
        return

    # 匿名可读时仍然成立的几层。**这就是全部**，别把这份清单读成"边界还很厚"。
    assert (c.raw.get("auth") or {}).get("enabled") is True, (
        "匿名可读的实例仍必须启用登录 —— 写操作要靠它认人，审计要靠它记名"
    )
    assert c.raw["observability"]["replay_api"] is False, (
        "回放返回 SQL 全文，匿名可读时等于把库结构透给任何访客"
    )
    # 内置源若哪天配回来，那两条旧断言必须一起回来：它一回来就又是一条
    # 不经注册表、直接吃配置的链路，只读账号与租户隔离在那条路上仍然是门。
    if ds:
        assert "user=askdb_ro" in ds.get("dsn", ""), (
            "内置源必须连只读库账号：护栏拦在应用层，库账号是被绕过之后的最后一道"
        )
        assert c.tenant_enabled and c.raw["tenant"]["mode"] == "rls_and_predicate", (
            "内置源上必须双层租户隔离，应用层谓词被绕过时库侧 RLS 仍在"
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


def test_reads_are_not_touched_by_the_write_gate(client, sources_store):
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


def test_creating_a_task_needs_login_but_plain_asking_does_not(client):
    """任务与普通提问走同一条链路，后端分辨不出来，所以由调用方带 as_task 声明。

    这个标记**只用来加严**：为真时要求登录，为假时行为与从前完全一致 ——
    伪造它只会把自己挡在外面，没有可乘之机。这条用例钉的就是这个方向：
    匿名提问必须仍然通得过，否则「未登录能查数」这条就被顺手废掉了。
    """
    assert client.post("/api/ask", json={"question": "有多少知识库", "as_task": True}
                       ).status_code == 401
    assert client.post("/api/ask", json={"question": "有多少知识库"}).status_code != 401

    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "alice-pw"}).status_code == 200
    assert client.post("/api/ask", json={"question": "有多少知识库", "as_task": True}
                       ).status_code != 401

