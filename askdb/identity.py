"""身份与权限：角色定义与成员名单。

**边界（这是本模块存在的前提，改之前先读这段）**

  · 认证（你是谁）不归 askdb —— 交给 auth-gateway。它已经有 JWKS、
    OAuth token-exchange、应用级 membership、找回密码、短信、风控这一整套，
    重做一遍既贵又每条都是安全敏感项。
  · 授权（你能干什么）归 askdb —— 环境范围、数据期限、脱敏、表白名单
    都是本项目的领域概念，网关不该知道 PROD-RO 是什么意思。

所以这里只存"谁属于哪个角色"，不存口令、不签发令牌、不做会话。
关联键是网关的 auth_user_id：`auth_users` 表里手机号与邮箱都是哈希存的，
**明文手机号根本没法当关联键**，用户名又可改，只有 id 稳定。

登录尚未接入，所以成员先由管理员按用户名手工登记，auth_user_id 留空，
待接入后回填并核对 —— 页面会把「未绑定网关用户」如实标出来，
不会让人以为已经关联上了。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .config import Config


class IdentityDisabled(RuntimeError):
    """未配置身份库。功能整体关闭，不是错误 —— 对外实例就该是这个状态。"""


class IdentityError(RuntimeError):
    """写入被业务规则拒绝（重名、未知角色等）。"""


@dataclass(frozen=True)
class Role:
    code: str
    name: str
    scope: str
    desc: str
    system: bool = False


# 角色是**固定**的，不开放自定义。
#
# 权限模型的每一条都要能对应到护栏上的一个具体行为；让人随手新建角色，
# 就会出现一批没有任何执行含义的名字，看着像有权限体系，实际什么也没约束。
# 新增角色应当是一次带设计的改动，不是一次点击。
ROLES: tuple[Role, ...] = (
    Role("PRODUCT", "产品", "PROD-RO",
         "在生产只读镜像上查询业务数据。敏感字段按脱敏策略返回，不可见原始个人信息。"),
    Role("DEV", "开发", "DEV + STAGING",
         "开发与测试环境全量可查，用于排障与验证。"),
    Role("QA", "测试", "STAGING",
         "仅测试环境与模拟数据，不接触任何生产数据。"),
    Role("DATA_OWNER", "数据负责人", "DOMAIN ALL",
         "配置策略、审批高风险与高成本查询。"),
    # 职责分离：管人的不自动获得看数据的权限。
    # 把两者合在一起，等于让管理员可以给自己开任意数据权限而不留痕。
    Role("SYS_ADMIN", "系统管理员", "SYSTEM",
         "管理角色成员。**不因此获得任何数据访问权** —— 需要查数须另行加入数据角色。",
         system=True),
)

ROLE_BY_CODE = {r.code: r for r in ROLES}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS askdb_role_members (
    id            BIGSERIAL PRIMARY KEY,
    role_code     TEXT        NOT NULL,
    auth_user_id  BIGINT,
    username      TEXT        NOT NULL,
    display_name  TEXT        NOT NULL DEFAULT '',
    note          TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT        NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_role_member
    ON askdb_role_members (role_code, lower(username));
"""


#: 连接串可整体走环境变量。仓库里的开发配置**不能写死某台机器的库和账号** ——
#: 新克隆拿到的配置会指向一个不存在的库，而症状只是页面报 503，很难联想到配置。
DSN_ENV = "ASKDB_IDENTITY_DSN"


def _raw_dsn(cfg: Config) -> str:
    section = cfg.raw.get("identity") or {}
    return (os.environ.get(DSN_ENV) or str(section.get("dsn") or "")).strip()


def enabled(cfg: Config) -> bool:
    section = cfg.raw.get("identity") or {}
    return bool(section.get("enabled")) and bool(_raw_dsn(cfg))


def _dsn(cfg: Config) -> str:
    section = cfg.raw.get("identity") or {}
    dsn = _raw_dsn(cfg)
    if not dsn:
        raise IdentityDisabled(f"未配置 identity.dsn，也没有 {DSN_ENV}")
    env = section.get("password_env")
    pwd = os.environ.get(env) if env else None
    if pwd and "password=" not in dsn:
        dsn = f"{dsn} password={pwd}"
    return dsn


def _connect(cfg: Config):
    if not enabled(cfg):
        raise IdentityDisabled("身份与权限未启用")
    try:
        import psycopg
    except ImportError as e:  # pragma: no cover - 依赖缺失
        raise IdentityDisabled(f'未安装 psycopg：uv pip install "psycopg[binary]"') from e
    return psycopg.connect(_dsn(cfg), connect_timeout=5, autocommit=True)


def ensure_schema(cfg: Config) -> None:
    """建表。幂等，每次读写前调一次成本可以忽略，省掉一套迁移工具。"""
    with _connect(cfg) as con:
        con.execute(_SCHEMA)


def roles_with_counts(cfg: Config) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    if enabled(cfg):
        ensure_schema(cfg)
        with _connect(cfg) as con:
            rows = con.execute(
                "SELECT role_code, COUNT(*) FROM askdb_role_members GROUP BY role_code"
            ).fetchall()
        counts = {code: int(n) for code, n in rows}
    return [
        {"code": r.code, "name": r.name, "scope": r.scope, "desc": r.desc,
         "system": r.system, "members": counts.get(r.code, 0)}
        for r in ROLES
    ]


def list_members(cfg: Config, role_code: str = "") -> list[dict[str, Any]]:
    ensure_schema(cfg)
    sql = ("SELECT id, role_code, auth_user_id, username, display_name, note,"
           " created_at, created_by FROM askdb_role_members")
    params: tuple[Any, ...] = ()
    if role_code:
        sql += " WHERE role_code = %s"
        params = (role_code,)
    sql += " ORDER BY created_at DESC, id DESC"
    with _connect(cfg) as con:
        rows = con.execute(sql, params).fetchall()
    return [
        {"id": r[0], "role_code": r[1], "auth_user_id": r[2], "username": r[3],
         "display_name": r[4], "note": r[5],
         "created_at": r[6].isoformat(), "created_by": r[7],
         # 登录接入前一律未绑定。如实标出来，别让人以为已经关联上网关账号了
         "bound": r[2] is not None}
        for r in rows
    ]


def add_member(cfg: Config, *, role_code: str, username: str,
               display_name: str = "", note: str = "", created_by: str = "") -> dict[str, Any]:
    if role_code not in ROLE_BY_CODE:
        raise IdentityError(f"未知角色：{role_code}")
    username = username.strip()
    if not username:
        raise IdentityError("用户名不能为空")
    if len(username) > 64:
        raise IdentityError("用户名过长（上限 64）")

    ensure_schema(cfg)
    import psycopg

    try:
        with _connect(cfg) as con:
            row = con.execute(
                "INSERT INTO askdb_role_members"
                " (role_code, username, display_name, note, created_by)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
                (role_code, username, display_name.strip()[:64], note.strip()[:200], created_by),
            ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        raise IdentityError(f"{username} 已经在该角色里了") from e

    return {"id": row[0], "role_code": role_code, "username": username,
            "display_name": display_name.strip()[:64], "note": note.strip()[:200],
            "auth_user_id": None, "bound": False,
            "created_at": row[1].isoformat(), "created_by": created_by}


def remove_member(cfg: Config, member_id: int) -> bool:
    ensure_schema(cfg)
    with _connect(cfg) as con:
        cur = con.execute("DELETE FROM askdb_role_members WHERE id = %s", (member_id,))
        return cur.rowcount > 0
