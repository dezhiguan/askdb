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

askdb 的登录是固定体验账号、不携带网关身份，auth-gateway 对接尚未落地，
所以成员先由管理员按用户名手工登记，auth_user_id 留空，接入后回填并核对
—— 页面会把「未绑定网关用户」如实标出来，不会让人以为已经关联上了。
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

    三个维度，都是**收窄**语义：
      · tables   —— 可见表。None 表示不额外收窄（用实例白名单）
      · max_rows —— 返回行上限。None 表示不额外收窄（用实例配置）
      · envs     —— 可用数据源的环境档位。None 表示不额外收窄

    刻意不做"允许列表"之外的能力位 —— 那些是 CAPABILITIES 的事。
    这三个维度都落在既有判定上：tables/max_rows 落在护栏 R-03 与 R-13，
    envs 落在选源那一步，不需要新造任何规则。

    envs 与另外两个的区别值得记一笔：tables/max_rows 收窄的是"查到的东西"，
    envs 收窄的是"连哪个库"。后者必须在选源时判，不能等进了护栏 ——
    护栏只看 SQL，它没有"这条连接通向哪台机器"这个信息。
    """
    tables: frozenset[str] | None = None
    max_rows: int | None = None
    envs: frozenset[str] | None = None
    #: 可见数据的时间窗口（天）。None = 不限。落在护栏 R-19 的谓词注入上，
    #: 与租户谓词（R-10）是同一类动作 —— 都是行级收窄，都靠往 SQL 里加条件。
    max_age_days: int | None = None
    #: 能不能看到个人信息列的**原值**。默认 False = 看脱敏值。
    #: 这一位与其他维度方向相反（其他是"收窄"，它是"放开"），所以
    #: combine 取或、且**不开放配置** —— 能配的东西就会被配错，
    #: 而这一位配错等于把个人信息交出去。
    unmask: bool = False


#: 内置默认。配置可以在此基础上**继续收窄**，不能放宽。
#:
#: 除系统角色外一律不额外收窄 —— 默认行为与没有角色时完全一致，
#: 接入角色不会悄悄改变任何现有实例的可查范围。要收窄是部署方的显式决定。
DEFAULT_POLICIES: dict[str, Policy] = {
    # 职责分离在这里落到实处：管人的角色拿不到任何数据。
    # 这不是配置项，是内置默认 —— 忘了配也不会漏。
    "SYS_ADMIN": Policy(tables=frozenset(), max_rows=0, envs=frozenset()),

    # 环境档位是**内置的**，不是配置项：它就是 Role.scope 那一行字的执行含义。
    # 放进配置意味着可以把测试角色配到生产只读镜像上，而那恰恰是
    # 「仅测试环境与模拟数据，不接触任何生产数据」这句话承诺过不会发生的事。
    # 要改这几行必须改代码、走评审 —— 与 ROLES 固定不开放自定义同一个理由。
    # 数据期限取设计文档矩阵 Q-07 的值。它们是内置的，理由同 envs。
    #
    # unmask 只给两个角色，各有各的理由：
    #   · DEV —— 它只连 dev/test（见上面的 envs），那些环境里本就没有真实
    #     个人信息；在合成数据上脱敏只会妨碍排障，拦不住任何东西。
    #   · DATA_OWNER —— 它是数据的归口人，判断口径本身就需要看到原值。
    # 产品与测试一律看脱敏值：矩阵 Q-06 写的就是"强制脱敏"。
    "PRODUCT": Policy(envs=frozenset({"prod_ro"}), max_age_days=90),
    "DEV": Policy(envs=frozenset({"dev", "test"}), unmask=True),
    "QA": Policy(envs=frozenset({"test"}), max_age_days=180),
    "DATA_OWNER": Policy(max_age_days=365, unmask=True),
}


# ---------------------------------------------------------------------------
# 能力位
#
# 与 Policy 是**两个不同的问题**，别合并：
#   · Policy 管「看得到哪些数据」—— 表、行数，落在护栏 R-03 / R-13 上。
#   · 能力位管「进不进得了这个功能」—— 落在接口入口处。
#
# 分开的理由是它们的失效方式不同。Policy 配错了，人少看见几张表；
# 能力位配错了，人能调一个本不该调的接口。把两者塞进一个结构，
# 就得在每个判定点回答"这个字段现在是哪种语义"，迟早判错一次。
#
# 名称直接对应设计文档《角色与权限设计》第三节的权限点编码，
# 便于在矩阵与代码之间对读 —— 矩阵改了而代码没改，grep 一遍就能发现。
# ---------------------------------------------------------------------------

QUERY = "query"                     # Q-01 自然语言查询
QUERY_SQL = "query.sql"             # Q-02 直查 SQL
SOURCES_READ = "sources.read"       # S-01 数据源列表
SOURCES_TEST = "sources.test"       # S-02 测试连接
SOURCES_WRITE = "sources.write"     # S-03/04/05 增删改
SOURCES_SCAN = "sources.scan"       # S-06 元数据扫描
GLOSSARY_READ = "glossary.read"     # G-01/G-02 口径查看与校验
QUALITY_READ = "quality.read"       # E-01 质量中心
SELFCHECK = "selfcheck"             # E-02 运行时自检
INTROSPECT = "introspect"           # E-03 库结构内省
AUDIT_READ = "audit.read"           # A-01/A-02 审计（默认只有本人的）
AUDIT_ALL = "audit.all"             # A-01 跨用户查看
AUDIT_CONTENT = "audit.content"     # A-01 看得到 question 与 sql_final
REPLAY = "replay"                   # A-03 查询复放
TASKS_ALL = "tasks.all"             # T-03 他人任务（仅元数据）
MEMBERS_READ = "members.read"       # I-02 跨角色成员名册
MEMBERS_WRITE = "members.write"     # I-03 增删成员
APPROVE = "approve"                 # Q-08 / S-03~05 审批放行

#: 角色 → 能力位。**固定，不开放配置**，理由同 ROLES 那段注释：
#: 能配的东西就会被配错，而这一层配错等于开门。
#:
#: 三条要点，改之前先读：
#:   1. SYS_ADMIN 有 APPROVE 但没有 QUERY —— 它的 Policy 是空表集，
#:      永远不可能是查询发起人，所以**自批在结构上不可能发生**。
#:      这是把审批收敛到系统管理员最主要的收益，别为了"方便"给它加 QUERY。
#:   2. SYS_ADMIN 有 AUDIT_READ + AUDIT_ALL 但**没有 AUDIT_CONTENT** ——
#:      管人的需要知道有没有人在违规访问，不需要知道业务上问了什么。
#:      审批场景是唯一例外，走单独的判定（见 server 的待审批队列）。
#:   3. DATA_OWNER 没有 APPROVE：数据源变更由它提出、由系统管理员放行，
#:      提出与放行分属两人。给它 APPROVE 就等于自己批自己。
CAPABILITIES: dict[str, frozenset[str]] = {
    "PRODUCT": frozenset({
        QUERY, SOURCES_READ, GLOSSARY_READ, QUALITY_READ,
        AUDIT_READ, AUDIT_CONTENT,          # 只有本人的：没有 AUDIT_ALL
    }),
    "DEV": frozenset({
        QUERY, QUERY_SQL, SOURCES_READ, SOURCES_TEST, SOURCES_WRITE, SOURCES_SCAN,
        GLOSSARY_READ, QUALITY_READ, SELFCHECK, INTROSPECT,
        AUDIT_READ, AUDIT_ALL, AUDIT_CONTENT, REPLAY, TASKS_ALL,
    }),
    "QA": frozenset({
        QUERY, QUERY_SQL, SOURCES_READ, SOURCES_TEST, SOURCES_SCAN,
        GLOSSARY_READ, QUALITY_READ, SELFCHECK, INTROSPECT,
        AUDIT_READ, AUDIT_CONTENT,          # 只有本人的
    }),
    "DATA_OWNER": frozenset({
        QUERY, QUERY_SQL, SOURCES_READ, SOURCES_TEST, SOURCES_WRITE, SOURCES_SCAN,
        GLOSSARY_READ, QUALITY_READ, SELFCHECK, INTROSPECT,
        AUDIT_READ, AUDIT_ALL, AUDIT_CONTENT, REPLAY, TASKS_ALL,
        MEMBERS_READ,
    }),
    "SYS_ADMIN": frozenset({
        MEMBERS_READ, MEMBERS_WRITE, APPROVE,
        SOURCES_READ, QUALITY_READ, SELFCHECK,
        AUDIT_READ, AUDIT_ALL,              # 元数据可见，AUDIT_CONTENT 不给
    }),
    # 匿名只在 auth.required=false 的实例上出现 —— 那是部署方明确选择的
    # "对外可看"状态，不是漏配。它保留今天的可见面，因为收紧它等于把
    # 一个以展示护栏与审计为目的的实例整个关掉。要锁就把 required 打开。
    ANONYMOUS: frozenset({
        QUERY, QUERY_SQL, SOURCES_READ, GLOSSARY_READ, QUALITY_READ,
        # AUDIT_CONTENT 在这里是**产品决定**（2026-09-06）：审计与追踪两页
        # 要讲的是"这套东西在真实调用上如何运转"，问题原文一律遮掉的话
        # 这两页就没有可读性了。对外实例上那正是要展示的东西。
        #
        # 与系统管理员**看不到**原文并不矛盾：那一条是职责分离（管人的不该
        # 翻业务问题），这一条是对外展示。两者约束的是不同的人和不同的目的。
        AUDIT_READ, AUDIT_ALL, AUDIT_CONTENT,
        INTROSPECT, SELFCHECK,
    }),
}


def caps_of(role_codes: list[str]) -> frozenset[str]:
    """一组角色的能力位并集。

    与 Policy 一样，RBAC 在这里是**加法**：身兼两职的人拿到两者之和。
    未知角色码贡献空集 —— 名单里出现一个已下线的角色不会让判定崩掉，
    但也绝不会因此多给一位。
    """
    out: frozenset[str] = frozenset()
    for code in role_codes:
        out |= CAPABILITIES.get(code, frozenset())
    return out


def can(role_codes: list[str], cap: str) -> bool:
    """这组角色有没有某个能力位。空名单一律没有。"""
    return cap in caps_of(role_codes)


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

    envs = base.envs
    if spec.get("envs") is not None:
        want_envs = frozenset(str(e).strip().lower() for e in spec["envs"])
        envs = want_envs if envs is None else (envs & want_envs)

    age = base.max_age_days
    if spec.get("max_age_days") is not None:
        want_age = int(spec["max_age_days"])
        age = want_age if age is None else min(age, want_age)

    # unmask 有意不从配置读：见 Policy.unmask 那段注释。
    return Policy(tables=tables, max_rows=max_rows, envs=envs,
                  max_age_days=age, unmask=base.unmask)


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

    envs: frozenset[str] | None = frozenset()
    for p in policies:
        if p.envs is None:
            envs = None
            break
        envs |= p.envs

    # 期限取**最长**：与 max_rows 取大同一个道理，身兼两职看得到两者之和
    ages = [p.max_age_days for p in policies]
    max_age = None if any(a is None for a in ages) else max(ages)

    return Policy(tables=tables, max_rows=max_rows, envs=envs,
                  max_age_days=max_age,
                  unmask=any(p.unmask for p in policies))


def for_roles(cfg: Config, role_codes: list[str], user: str = "") -> Config:
    """取一组角色叠加后的配置。登录用户走这条。"""
    if not role_codes:
        return dataclasses.replace(for_role(cfg, ANONYMOUS), user=user)
    combined = combine([policy_for(cfg, code) for code in role_codes])
    return dataclasses.replace(narrow(cfg, combined, "+".join(sorted(role_codes))), user=user)


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
    # 与 max_rows 走同一条路：塞进 raw，护栏与执行器照常从 cfg 取值，
    # 因此它们一行都不用知道"角色"这个概念的存在。
    raw = {**raw, "_role_window_days": policy.max_age_days,
           "_role_unmask": policy.unmask}

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
    """建表。幂等，省掉一套迁移工具。

    **只在写路径调。** 原来读路径也调一次，于是任何人一个匿名 GET
    （/api/identity/roles）就能让服务端对身份库执行一次 DDL —— 幂等归幂等，
    但"未登录不碰库"这条口径它是不满足的。读路径改为容忍表不存在，
    见 _rows()。
    """
    with _connect(cfg) as con:
        con.execute(_SCHEMA)


def _rows(cfg: Config, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """读一次库，**表还没建出来时按空处理**。

    没有 askdb_role_members 表 = 一个成员都还没登记过，与查出来 0 行是同一回事，
    不该为了区分这一点而在读路径上建表。第一次写入时 add_member 会建。
    """
    import psycopg

    try:
        with _connect(cfg) as con:
            return con.execute(sql, params).fetchall()
    except psycopg.errors.UndefinedTable:
        return []


def builtin_members(cfg: Config, role_code: str = "") -> list[dict[str, Any]]:
    """配置里内置的人员，按角色摊平成成员条目。

    这些人**能登录**（auth.accounts 带 password_hash），所以他们就是实打实的
    角色持有者。名单只从库表读的话，页面会说"测试角色 0 人"，而实际上有两个
    人拿着口令随时能以 QA 身份查数 —— 权限页最不能出的就是这种谎。

    auth.roles_of() 早就是"配置 ∪ 库表"的并集，这里只是把同一条口径补到名单上。
    内置条目 id 恒为 0：删除接口按 id 匹配，因此天然删不掉 —— 它们由配置文件
    管理，不该能在页面上点掉。
    """
    from . import auth                      # 延迟导入：auth.roles_of 反向依赖本模块

    want = (role_code or "").strip()
    out: list[dict[str, Any]] = []
    for acc in auth.accounts(cfg).values():
        for code in acc.roles:
            if code not in ROLE_BY_CODE or (want and code != want):
                continue
            out.append({
                "id": 0, "role_code": code, "auth_user_id": None,
                "username": acc.username, "display_name": acc.display_name,
                "note": acc.note, "created_at": "", "created_by": "配置内置",
                "bound": False, "builtin": True,
            })
    out.sort(key=lambda m: (m["role_code"], m["username"]))
    return out


def roles_with_counts(cfg: Config) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    if enabled(cfg):
        rows = _rows(
            cfg, "SELECT role_code, COUNT(*) FROM askdb_role_members GROUP BY role_code")
        counts = {code: int(n) for code, n in rows}
    # 内置人员一并计入，并按 (角色, 用户名) 去重 —— 同一个人既写在配置里
    # 又被管理员登记过一次，是一个人，不是两个
    seen = {(m["role_code"], m["username"].lower()) for m in builtin_members(cfg)}
    for code, uname in _db_member_keys(cfg):
        if (code, uname) in seen:
            counts[code] = counts.get(code, 0) - 1     # 该行与内置条目重复，扣回
    for code, _ in seen:
        counts[code] = counts.get(code, 0) + 1
    return [
        {"code": r.code, "name": r.name, "scope": r.scope, "desc": r.desc,
         "system": r.system, "members": counts.get(r.code, 0),
         # envs 是 scope 那行字的**执行值**。一并给出去，前端的「环境范围」
         # 就不再是照抄设计稿的字符串，而是这套部署真正在拦的东西 ——
         # 权限体系最怕的是"配了但看不出有没有生效"。
         "envs": sorted(policy_for(cfg, r.code).envs or ()),
         "envs_unrestricted": policy_for(cfg, r.code).envs is None,
         # 另外两格同理给真值。页面此前把这三格写死成设计稿取值，
         # 于是「数据期限 90 DAYS」在后端根本没有对应字段时也照样显示 ——
         # 权限体系最怕的就是"配了但看不出有没有生效"，而这比看不出更糟：
         # 它显示了一个从未生效过的值。
         "max_age_days": policy_for(cfg, r.code).max_age_days,
         "unmask": policy_for(cfg, r.code).unmask}
        for r in ROLES
    ]


def list_members(cfg: Config, role_code: str = "") -> list[dict[str, Any]]:
    sql = ("SELECT id, role_code, auth_user_id, username, display_name, note,"
           " created_at, created_by FROM askdb_role_members")
    params: tuple[Any, ...] = ()
    if role_code:
        sql += " WHERE role_code = %s"
        params = (role_code,)
    sql += " ORDER BY created_at DESC, id DESC"
    rows = _rows(cfg, sql, params)

    out = builtin_members(cfg, role_code)          # 内置名册排在前，它是固定的那部分
    seen = {(m["role_code"], m["username"].lower()) for m in out}
    for r in rows:
        if (r[1], r[3].lower()) in seen:           # 与内置条目是同一个人，不重复列
            continue
        out.append(
            {"id": r[0], "role_code": r[1], "auth_user_id": r[2], "username": r[3],
             "display_name": r[4], "note": r[5],
             "created_at": r[6].isoformat(), "created_by": r[7],
             # 登录接入前一律未绑定。如实标出来，别让人以为已经关联上网关账号了
             "bound": r[2] is not None, "builtin": False})
    return out


def add_member(cfg: Config, *, role_code: str, username: str,
               display_name: str = "", note: str = "", created_by: str = "") -> dict[str, Any]:
    if role_code not in ROLE_BY_CODE:
        raise IdentityError(f"未知角色：{role_code}")
    username = username.strip()
    if not username:
        raise IdentityError("用户名不能为空")
    if len(username) > 64:
        raise IdentityError("用户名过长（上限 64）")
    # 内置条目删不掉（id=0），再登记一条同名的只会造出一行看不见也删不掉的影子
    if any(m["username"].lower() == username.lower()
           for m in builtin_members(cfg, role_code)):
        raise IdentityError(f"{username} 已由配置内置在该角色里")

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


def _db_member_keys(cfg: Config) -> list[tuple[str, str]]:
    """库表里的 (角色, 小写用户名)。只服务于计数去重，不对外。"""
    if not enabled(cfg):
        return []
    return [(r[0], r[1]) for r in _rows(
        cfg, "SELECT role_code, lower(username) FROM askdb_role_members")]
