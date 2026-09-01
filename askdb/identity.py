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
import dataclasses
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

#: 匿名调用的角色码。它是一个**普通角色**，不是绕过分支 ——
#: 授权代码里因此不存在"没有身份"这种第三态，少一整类判空错误。
ANONYMOUS = "ANONYMOUS"


@dataclass(frozen=True)
class Policy:
    """一个角色能看到什么。

    只有两个维度，都是**收窄**语义：
      · tables   —— 可见表。None 表示不额外收窄（用实例白名单）
      · max_rows —— 返回行上限。None 表示不额外收窄（用实例配置）

    刻意不做"允许列表"之外的能力位。权限模型每多一个维度，就多一处
    "这条规则到底拦不拦得住"的争论；这两个维度直接落在既有的 R-03 与 R-13 上，
    不需要新造任何判定。
    """
    tables: frozenset[str] | None = None
    max_rows: int | None = None


#: 内置默认。配置可以在此基础上**继续收窄**，不能放宽。
#:
#: 除系统角色外一律不额外收窄 —— 默认行为与没有角色时完全一致，
#: 接入角色不会悄悄改变任何现有实例的可查范围。要收窄是部署方的显式决定。
DEFAULT_POLICIES: dict[str, Policy] = {
    # 职责分离在这里落到实处：管人的角色拿不到任何数据。
    # 这不是配置项，是内置默认 —— 忘了配也不会漏。
    "SYS_ADMIN": Policy(tables=frozenset(), max_rows=0),
}


def for_role(cfg: Config, role_code: str) -> Config:
    """取某个角色眼里的配置。调用方只需要这一个入口。"""
    return narrow(cfg, policy_for(cfg, role_code), role_code)


def policy_for(cfg: Config, role_code: str) -> Policy:
    """取角色的生效策略：内置默认与配置取**交集**。

    两边都能收窄，谁也不能放宽 —— 配置写错了最多让人少看见几张表，
    不会让人多看见。
    """
    base = DEFAULT_POLICIES.get(role_code, Policy())
    spec = (cfg.raw.get("role_policies") or {}).get(role_code)
    if not spec:
        return base

    tables = base.tables
    if spec.get("tables") is not None:
        want = frozenset(str(t).lower() for t in spec["tables"])
        tables = want if tables is None else (tables & want)

    max_rows = base.max_rows
    if spec.get("max_rows") is not None:
        cap = int(spec["max_rows"])
        max_rows = cap if max_rows is None else min(max_rows, cap)

    return Policy(tables=tables, max_rows=max_rows)


def combine(policies: list[Policy]) -> Policy:
    """多个角色叠加。

    RBAC 是**加法**：一个人身兼两职，看得到的是两者之和。表取并集、
    行上限取大 —— 但每个策略本身都已经是实例白名单的子集，所以并集
    仍然是子集，「只能收窄」这条不变量不受影响。

    这个语义顺带把职责分离表达对了：SYS_ADMIN 的策略是空表集，
    与任何数据角色取并集都等于那个数据角色 —— 当管理员既不增加也不减少
    数据权限。只有 SYS_ADMIN 的人则并集为空，一张表也看不到。
    """
    if not policies:
        return Policy()

    tables: frozenset[str] | None = frozenset()
    for p in policies:
        if p.tables is None:          # 有一个不额外收窄，合起来就不收窄
            tables = None
            break
        tables |= p.tables

    caps = [p.max_rows for p in policies]
    max_rows = None if any(c is None for c in caps) else max(caps)

    return Policy(tables=tables, max_rows=max_rows)


def for_roles(cfg: Config, role_codes: list[str]) -> Config:
    """取一组角色叠加后的配置。登录用户走这条。"""
    if not role_codes:
        return for_role(cfg, ANONYMOUS)
    combined = combine([policy_for(cfg, code) for code in role_codes])
    return narrow(cfg, combined, "+".join(sorted(role_codes)))


def narrow(cfg: Config, policy: Policy, role_code: str = ANONYMOUS) -> Config:
    """按策略收窄一份配置，供本次调用使用。

    这是整个角色机制的全部实现 —— **护栏一行没改**。
    guard / executor / schema_rag 全都从 cfg.tables 与 cfg.max_rows 取值，
    所以喂给它们一份收窄的配置，R-03（表白名单）、R-13（行上限）自动按角色生效，
    连 Schema 召回都只会看到该角色可见的表，模型压根不知道别的表存在。

    在护栏内部按角色分支是另一条路，但那会让每条规则都多一个"当前是谁"的
    入参，每加一个角色都要重读一遍全部规则 —— 收窄配置只有一处要读对。
    """
    tables = cfg.tables
    if policy.tables is not None:
        # 交集：角色给的表若不在实例白名单里，一律无效。
        # 这条保证角色永远不可能成为提权路径。
        tables = {n: t for n, t in cfg.tables.items() if n in policy.tables}

    raw = cfg.raw
    if policy.max_rows is not None and policy.max_rows < cfg.max_rows:
        raw = {**cfg.raw, "guard": {**cfg.raw["guard"], "max_rows": policy.max_rows}}

    # 口径引用的表若已不可见，一并摘掉 —— 留着只会让模型照口径写出
    # 引用不可见表的 SQL，然后被 R-03 拦下，报错指向一个用户无法理解的地方
    metrics = [m for m in cfg.metrics if all(t in tables for t in m.scope)]

    return dataclasses.replace(cfg, tables=tables, raw=raw, metrics=metrics, role=role_code)


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
