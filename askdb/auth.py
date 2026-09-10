"""登录与会话。

**范围**：固定几个账号，没有自助注册、没有找回密码、没有短信。
这不是把 auth-gateway 重做一遍 —— 那套东西（注册、重置、验证码、风控、
应用级注销）一个都不在这里。这里只有「核对一个口令，发一张会话票」。

为什么不接网关：本实例的定位是**任何人可访问的对外站点**，可用性优先。
接网关等于把可用性押在另一个服务上（它今天刚因 Redis 失联挂过，
两个产品登录全灭）。访客点开链接看到登录报错，比没有登录糟得多。
要展示企业身份接入时，网关可以作为第二种登录方式接进来，不影响这里。

**会话是无状态签名票**，不落库：
  · 多副本天然一致，不需要共享会话存储
  · 代价是**签发后无法单独吊销**，只能靠有效期到期与换密钥整体失效。
    默认有效期 30 天（见 DEFAULT_TTL_DAYS），因此这个窗口并不短 ——
    固定账号、无自助注册的场景下这个代价可以接受；真要做单点吊销，
    得先有会话表，那是另一个量级的东西，不该悄悄混进来。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config

# scrypt 参数。n=2^14 在本机约 60~100ms —— 登录慢一点没关系，
# 但要让离线爆破足够贵。改小之前先想清楚泄露后的后果。
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_DKLEN = 32

SESSION_SECRET_ENV = "ASKDB_SESSION_SECRET"
COOKIE_NAME = "askdb_session"
#: 会话默认有效期 30 天。
#:
#: **这是一个被明确接受的代价**：见模块头，会话是无状态签名票、签发后无法单独
#: 吊销，短有效期本来是唯一的兜底。拉到 30 天等于把"某张票被人拿走"的窗口
#: 也拉到 30 天，此时唯一的收回手段是换 ASKDB_SESSION_SECRET —— 那会让所有人
#: 一起掉线。固定十个内置账号、无自助注册的前提下可以接受；真要做单点吊销，
#: 得先有会话表，那是另一个量级的东西。
DEFAULT_TTL_DAYS = 30
DEFAULT_TTL_S = DEFAULT_TTL_DAYS * 24 * 3600


class AuthError(RuntimeError):
    """登录被拒。对外一律用同一句话，不区分「账号不存在」与「口令不对」。"""


# ---------- 口令 ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    # 整段包起来：配置里的哈希写坏（被编辑器折行截断过一次）是配置问题，
    # 表现应当是"登不上"，不是 500 —— 500 会把内部栈暴露给登录接口的调用方
    try:
        kind, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"),
                            salt=base64.b64decode(salt_b64),
                            n=int(n), r=int(r), p=int(p), dklen=_DKLEN)
        expect = base64.b64decode(dk_b64)
    except (ValueError, TypeError):        # binascii.Error 是 ValueError 的子类
        return False
    # 定时安全比较：普通 == 会按前缀提前返回，泄露匹配了多少字节
    return hmac.compare_digest(dk, expect)


# ---------- 会话票 ----------

def _secret() -> bytes:
    return (os.environ.get(SESSION_SECRET_ENV) or "").encode("utf-8")


def session_available() -> bool:
    """没配密钥就整体关闭登录。

    fail-closed 是有意的：自动生成一把随机密钥会让**每个副本各签各的**，
    表现是"刷新几次就掉线"，而且重启即全体掉线 —— 这种故障比登不上更难查。
    """
    return len(_secret()) >= 16


def issue(username: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    if not session_available():
        raise AuthError("未配置会话密钥")
    payload = f"{username}|{int(time.time()) + ttl_s}".encode("utf-8")
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    sig = base64.urlsafe_b64encode(
        hmac.new(_secret(), body, hashlib.sha256).digest()).rstrip(b"=")
    return f"{body.decode()}.{sig.decode()}"


def read(token: str | None) -> str | None:
    """验票，返回用户名。任何一步不对都返回 None，不解释原因。"""
    if not token or not session_available():
        return None
    try:
        body, sig = token.split(".", 1)
        expect = base64.urlsafe_b64encode(
            hmac.new(_secret(), body.encode(), hashlib.sha256).digest()).rstrip(b"=")
        # 先验签再解内容 —— 顺序反了就是在处理未经验证的数据
        if not hmac.compare_digest(sig.encode(), expect):
            return None
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        username, exp = raw.rsplit("|", 1)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if int(exp) < int(time.time()):
        return None
    return username or None


# ---------- 账号 ----------

@dataclass(frozen=True)
class Account:
    username: str
    display_name: str
    roles: tuple[str, ...]
    password_hash: str = ""
    note: str = ""


def _section(cfg: Config) -> dict[str, Any]:
    return cfg.raw.get("auth") or {}


def enabled(cfg: Config) -> bool:
    return bool(_section(cfg).get("enabled")) and session_available()


def ttl_s(cfg: Config) -> int:
    """本实例的会话有效期（秒）。配置 auth.session_ttl_days 可覆盖。

    非法值（负数、写成字符串）一律退回默认，不让一个配置笔误把有效期
    变成 0 —— 那表现为"登录成功但立刻掉线"，比登不上更难查。
    """
    try:
        days = int(_section(cfg).get("session_ttl_days", DEFAULT_TTL_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_TTL_S
    return days * 24 * 3600 if days > 0 else DEFAULT_TTL_S


def required(cfg: Config) -> bool:
    """是否强制登录。

    对外开放实例为 false：匿名可用，登录是**可选的能力展示**而不是门。
    登录页是访客流失最大的一处，而这个站要给人看的是护栏与审计，不是登录框。
    """
    return bool(_section(cfg).get("required")) and enabled(cfg)


def accounts(cfg: Config) -> dict[str, Account]:
    out: dict[str, Account] = {}
    for spec in _section(cfg).get("accounts") or []:
        name = str(spec.get("username", "")).strip()
        if not name:
            continue
        out[name.lower()] = Account(
            username=name,
            display_name=str(spec.get("display_name") or name),
            roles=tuple(str(r) for r in (spec.get("roles") or [])),
            password_hash=str(spec.get("password_hash") or ""),
            note=str(spec.get("note") or ""),
        )
    return out


def authenticate(cfg: Config, username: str, password: str) -> Account:
    acc = accounts(cfg).get((username or "").strip().lower())
    # 账号不存在时也走一次哈希：直接返回会让"用户名存在与否"从响应耗时里漏出来
    stored = acc.password_hash if acc else hash_password(secrets.token_hex(8))
    ok = verify_password(password or "", stored)
    if not acc or not acc.password_hash or not ok:
        raise AuthError("账号或口令不正确")
    return acc


def identity_of(cfg: Config, username: str) -> tuple[list[str], str]:
    """一次查库同时拿到**角色**与**姓名**。

    两者是同一条并集口径（配置内置 ∪ 身份库里管理员登记的）：对外实例只有
    配置（不需要数据库），真实部署可以在身份库里继续加人，两者互不干扰。
    分成两个函数各查一次的话，/api/auth/me 这个每次页面加载都会调的接口，
    对身份库的往返就翻了一倍 —— 而它俩要的是同一份名单。

    身份库连不上不抛：已登录的人不该因为名册查不到就掉线、顶栏也不该空掉，
    退化成"只用配置里的那份"。
    """
    name = (username or "").strip()
    acc = accounts(cfg).get(name.lower())
    roles = list(acc.roles) if acc else []
    # accounts() 在没写 display_name 时会拿用户名兜底，那不算"有姓名"
    display = acc.display_name if acc and acc.display_name != acc.username else ""

    from . import identity

    if identity.enabled(cfg):
        try:
            # 定向查这一个账号。这里曾经是 list_members(cfg) 整表读再逐行比
            # 用户名 —— 名册涨到万人之后，每次页面加载（/api/auth/me）都要把
            # 全表拉进内存，只为拿回一两行。
            for _uname, code, member_name in identity.member_rows(cfg, [name]):
                if code not in roles:
                    roles.append(code)
                if not display and member_name:
                    display = member_name
        except Exception:
            pass
    return roles, display


def display_name_of(cfg: Config, username: str) -> str:
    """这个账号的**姓名**。

    界面上要显示的是人，不是网关用户名：后者是账号标识（guandezhi），
    对同事没有辨识度，也不是这套权限体系要展示的东西。取不到返回空串，
    由调用方决定退回什么 —— 这里不替它编一个名字。
    """
    return identity_of(cfg, username)[1]


def roles_of(cfg: Config, username: str) -> list[str]:
    """账号的角色：配置内置 ∪ 身份库里管理员登记的。"""
    return identity_of(cfg, username)[0]
