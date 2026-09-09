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
#
# 2026-09-06：**角色不再与数据源绑定**（产品决定）。askdb 是一个共享平台，
# 大家在同一批数据源上工作、看到的东西基本一致；角色的差别落在审批一类的
# 治理动作上，而不是"你只能连哪个库"。原来 scope 那一列写的是环境档位
# （PROD-RO / STAGING），既然不再据此拦截，就不能继续挂在页面上 ——
# 一个不再执行的承诺比没有承诺更危险。
#
# 2026-09-06（同日，第二步）：**角色之间不再有可见面差异**（产品决定）。
# 上一步撤掉的是"能连哪个库"，这一步撤掉的是"能看哪些表、多少行、多久以内、
# 看不看得到原值"。理由同上：共享平台上大家做的是同一件事，按角色分档位
# 只制造了一批解释不清的差别 —— 最刺眼的一处是产品角色登录之后看到的东西
# 比不登录还少。
#
# 现在整套模型只剩三条，别再往回加第四条：
#   1. 所有角色的可见面**完全相同**（含匿名）。
#   2. 唯一的角色差别是 APPROVE（审批）—— 只有系统管理员有。
#   3. 未登录可读不可写。这一条**不由角色表达**，由 server._gate_writes
#      按 HTTP 方法统一拦截；能力位这里只是把写类能力从匿名身上摘掉，
#      让页面能提前置灰，不是安全边界本身。
ROLES: tuple[Role, ...] = (
    Role("PRODUCT", "产品", "平台可见面",
         "查询业务数据。可见面与其他角色相同；个人信息列一律脱敏。"),
    Role("DEV", "开发", "平台可见面",
         "查询业务数据，用于排障与验证。可见面与其他角色相同；个人信息列一律脱敏。"),
    Role("QA", "测试", "平台可见面",
         "查询业务数据，用于验证。可见面与其他角色相同；个人信息列一律脱敏。"),
    Role("DESIGN", "设计", "平台可见面",
         "查询用户行为与体验相关数据，用于交互与视觉决策。"
         "可见面与其他角色相同；个人信息列一律脱敏。"),
    Role("DATA_OWNER", "数据", "平台可见面",
         "配置口径与数据源。可见面与其他角色相同；审批由系统管理员放行，"
         "提出与放行分属两人。"),
    # 2026-09-08 新增四个业务角色（产品决定）。
    #
    # 原来那五个角色描述的是**平台自身**的分工（产品、开发、测试、数据、
    # 管理员）。可查数的人大多不属于其中任何一个：运营、客服、财务、人力、
    # 管理层进来只能统统记成「产品」，角色这一栏于是不再描述任何事实。
    #
    # 加这四个**不改变任何权限**：可见面与其余角色完全相同，能力位取
    # _READ | _WRITE，一位不多。它们表达的是「这个人是干什么的」，
    # 不是「他能看到什么」—— 后者仍然只有 SYS_ADMIN 那一条差别（审批）。
    # 这与 2026-09-06 拍平可见面的决定不冲突：那次撤的是角色之间的**能力**
    # 差别，不是角色本身。
    Role("OPERATIONS", "运营", "平台可见面",
         "查询商品、交易、履约与用户行为数据，用于日常经营决策。"
         "可见面与其他角色相同；个人信息列一律脱敏。"),
    # 2026-09-08（同日，第二步）再补六个：设计、销售、市场、客服、法务合规、
    # 其他。理由与上一步同源，但指向的是另一半事实 —— 上一步补的是「平台之外
    # 的人是干什么的」，这一步补的是「运营」这一个筐装不下的那些人。
    #
    # 此前销售、市场、客服三条线全部落在 OPERATIONS 名下，于是「运营」一个
    # 角色占掉全公司三分之二（6840/10003）。一个角色装走三分之二的人，它就
    # 不再是一次分类，只是一个默认值 —— 与当初把所有人记成「产品」是同一个
    # 毛病，只是换了个名字。
    #
    # OTHER（其他）是**兜底位**，不是垃圾桶：新入职未定岗、外部顾问、实习生
    # 落这里。它应当长期是全表最小的一档；一旦涨过全员 2%，说明角色分类又缺
    # 项了，该补的是新角色而不是把人继续往这里塞。
    #
    # 这六个同样**不改变任何权限**，能力位与上面逐位相同。
    Role("SALES", "销售", "平台可见面",
         "查询商机、成交与客户经营数据。可见面与其他角色相同；"
         "个人信息列一律脱敏 —— 客户联系方式也不例外。"),
    Role("MARKETING", "市场", "平台可见面",
         "查询投放、渠道与品牌相关数据。可见面与其他角色相同；"
         "个人信息列一律脱敏。"),
    Role("SUPPORT", "客服", "平台可见面",
         "查询工单、售后与服务质量数据。可见面与其他角色相同；"
         "个人信息列一律脱敏。"),
    Role("FINANCE", "财务", "平台可见面",
         "查询交易、支付、对账与成本数据。可见面与其他角色相同；"
         "个人信息列一律脱敏。"),
    Role("HR", "人力", "平台可见面",
         "查询组织与人员相关数据。可见面与其他角色相同；"
         "个人信息列一律脱敏 —— 人力角色也不例外，脱敏对所有人生效。"),
    Role("LEGAL", "法务合规", "平台可见面",
         "查询合同、合规与风险相关数据。可见面与其他角色相同 —— "
         "合规职责不换来更大的可见面，个人信息列一律脱敏。"),
    Role("MANAGEMENT", "管理", "平台可见面",
         "查询各业务域的汇总口径。可见面与其他角色相同 —— "
         "职级高不等于看得多，这一条是有意的。"),
    Role("OTHER", "其他", "平台可见面",
         "尚未定岗的成员：新入职、外部顾问、实习生。"
         "可见面与其他角色相同 —— 未定岗不等于降权，也不等于提权。"),
    Role("SYS_ADMIN", "系统管理员", "平台可见面 + 审批",
         "管理角色成员，并审批高成本查询与数据源变更。"
         "**这是唯一一个多出权限的角色**，多出来的只有审批这一项。",
         system=True),
)

ROLE_BY_CODE = {r.code: r for r in ROLES}

#: 匿名调用的角色码。它是一个**普通角色**，不是绕过分支 ——
#: 授权代码里因此不存在"没有身份"这种第三态，少一整类判空错误。
ANONYMOUS = "ANONYMOUS"


@dataclass(frozen=True)
class Policy:
    """一个角色能看到什么。

    两个维度，都是**收窄**语义：
      · tables   —— 可见表。None 表示不额外收窄（用实例白名单）
      · max_rows —— 返回行上限。None 表示不额外收窄（用实例配置）

    刻意不做"允许列表"之外的能力位 —— 那些是 CAPABILITIES 的事。
    两个维度都落在既有判定上：tables/max_rows 落在护栏 R-03 与 R-13，
    不需要新造任何规则。

    **这个结构现在默认全空**：见 DEFAULT_POLICIES 那段注释 —— 产品决定是
    所有角色可见面相同，因此内置默认一条收窄都不写。留下机制而不留下取值，
    是因为"只能收窄不能放宽"这条不变量本身仍然要被守住：部署方在
    role_policies 里写什么都不可能放宽，这一点由 narrow() 保证并有测试覆盖。

    **曾经有过两个维度，都已撤掉，别加回来：**
      · envs（角色可连哪些环境的数据源）—— 2026-09-06 撤，角色不与数据源绑定。
      · unmask（能否看到个人信息原值）—— 2026-09-06 撤，脱敏对**所有人**生效，
        不再是一个可以按角色放开的位。它当初就注明"能配的东西就会被配错，
        而这一位配错等于把个人信息交出去"；现在它连按角色开的余地也没有了。
    撤的都是整条判定，不是留个不生效的字段。
    """
    tables: frozenset[str] | None = None
    max_rows: int | None = None
    #: 可见数据的时间窗口（天）。None = 不限。落在护栏 R-19 的谓词注入上，
    #: 与租户谓词（R-10）是同一类动作 —— 都是行级收窄，都靠往 SQL 里加条件。
    #: 内置默认不设窗口（所有角色一致）；部署方要收紧仍可在 role_policies 里配。
    max_age_days: int | None = None


#: 内置默认。配置可以在此基础上**继续收窄**，不能放宽。
#:
#: **空字典是有意的，不是漏写。** 2026-09-06 产品决定：所有角色（含匿名）
#: 的可见面完全相同，唯一的角色差别是审批。因此这里一条内置收窄都没有 ——
#: 角色不再改变任何人的可查范围。
#:
#: 这里原来有四条，一并记下撤掉的理由，省得下次又照着"看起来很专业"加回来：
#:   · PRODUCT 90 天 / QA 180 天 / DATA_OWNER 365 天的数据期限 —— 档位差别
#:     解释不清，且与"大家做同一件事"的前提冲突。
#:   · DEV / DATA_OWNER 的 unmask —— 脱敏改为对所有人生效，见 Policy 注释。
#:   · SYS_ADMIN 的空表集（tables=∅, max_rows=0）—— 那是靠"管理员查不到数据"
#:     换来的"自批在结构上不可能"。产品决定系统管理员也要能查数，这条结构性
#:     保证随之消失，改由 approvals 的**发起人不得自批**判定顶上（见
#:     _approvals.decide）。用一条显式判定换一条隐式保证，是这次改动里
#:     唯一需要盯住的地方。
DEFAULT_POLICIES: dict[str, Policy] = {}


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
AUDIT_READ = "audit.read"           # A-01/A-02 审计
AUDIT_ALL = "audit.all"             # A-01 跨用户查看
AUDIT_CONTENT = "audit.content"     # A-01 看得到 question 与 sql_final
REPLAY = "replay"                   # A-03 查询复放
TASKS_ALL = "tasks.all"             # T-03 他人任务（仅元数据）
MEMBERS_READ = "members.read"       # I-02 跨角色成员名册
MEMBERS_WRITE = "members.write"     # I-03 增删成员
APPROVE = "approve"                 # Q-08 / S-03~05 审批放行
EVAL_RUN = "eval.run"               # E-04 触发黄金集回归

#: 读类能力位 —— **每一个角色，包括匿名，都拿到全部这些位。**
#: 「不同角色看到的内容完全一样」这句话在代码里就是这一行。
_READ: frozenset[str] = frozenset({
    QUERY, QUERY_SQL, SOURCES_READ, GLOSSARY_READ, QUALITY_READ,
    SELFCHECK, INTROSPECT,
    AUDIT_READ, AUDIT_ALL, AUDIT_CONTENT, REPLAY, TASKS_ALL, MEMBERS_READ,
})

#: 写类能力位 —— 登录用户都有，匿名一个都没有。
#:
#: 这里摘掉匿名的写位，**不是**安全边界：真正的边界是 server._gate_writes
#: 中间件，它按 HTTP 方法拦下所有未登录的 POST/PUT/PATCH/DELETE，新增接口
#: 默认落在安全那边。能力位在这里的作用是让页面**提前**把按钮置灰，
#: 而不是让人点完才知道做不了（前端 writeGuard 同一件事的另一半）。
#:
#: SOURCES_TEST / SOURCES_SCAN 归在写类：它们不改数据，但都是 POST，
#: 且都会对目标库发起真实连接与扫描。判据取"会不会往外做动作"，
#: 与中间件的方法判据对齐 —— 两处判据形状一致，才不会各自漂移。
_WRITE: frozenset[str] = frozenset({
    SOURCES_TEST, SOURCES_SCAN, SOURCES_WRITE,
    # 跑一轮回归会真的调模型、真的查库，**花钱也压库** —— 判据同 SOURCES_SCAN：
    # 会不会往外做动作。匿名一律不给。
    EVAL_RUN,
})

#: 角色 → 能力位。**固定，不开放配置**：能配的东西就会被配错，
#: 而这一层配错等于开门。
#:
#: 结构就是下面这三行，别再长回一张按角色分档的表：
#:   · 所有登录角色 = _READ | _WRITE，**完全相同**
#:   · SYS_ADMIN 额外多 APPROVE 与 MEMBERS_WRITE
#:   · ANONYMOUS = _READ，未登录可读不可写
#:
#: **MEMBERS_WRITE 为什么也只给系统管理员**（它看起来像是那条"唯一差别"的
#: 例外，其实是它的前提）：成员名单决定谁属于哪个角色。把增删成员开给所有
#: 登录用户，任何人都可以把自己加进 SYS_ADMIN，于是"只有系统管理员能审批"
#: 这条就不再是一条约束，而是一次点击的距离。守住 APPROVE 的唯一性，就必须
#: 同时守住"谁能改成员名单"。它另有一道 ASKDB_ADMIN_TOKEN 的门，两道是与的
#: 关系，不是互相替代。
#:
#: 关于 APPROVE 只给系统管理员：数据源变更由数据负责人提出、由系统管理员
#: 放行，提出与放行分属两人。这条分离过去还有第二层保证 —— SYS_ADMIN 的
#: 策略是空表集，永远不可能是查询发起人，于是自批在结构上不可能。
#: 2026-09-06 系统管理员改为也能查数，那层保证没有了，自批改由
#: _approvals.decide 里的**发起人不得自批**显式判定挡住。改动审批链路前
#: 先读那一处：它现在是唯一一道门。
CAPABILITIES: dict[str, frozenset[str]] = {
    "PRODUCT": _READ | _WRITE,
    "DEV": _READ | _WRITE,
    "QA": _READ | _WRITE,
    "DATA_OWNER": _READ | _WRITE,
    # 2026-09-08 新增的十个业务角色，与上面几个**逐位相同**。
    # 新增角色时若忘了在这里登记，caps_of 会给出空集 —— 那个人进不了任何
    # 功能页，而角色页上他看着一切正常。这里是唯一一处必须同步的地方。
    "DESIGN": _READ | _WRITE,
    "OPERATIONS": _READ | _WRITE,
    "SALES": _READ | _WRITE,
    "MARKETING": _READ | _WRITE,
    "SUPPORT": _READ | _WRITE,
    "FINANCE": _READ | _WRITE,
    "HR": _READ | _WRITE,
    "LEGAL": _READ | _WRITE,
    "MANAGEMENT": _READ | _WRITE,
    "OTHER": _READ | _WRITE,
    "SYS_ADMIN": _READ | _WRITE | {APPROVE, MEMBERS_WRITE},
    ANONYMOUS: _READ,
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

    age = base.max_age_days
    if spec.get("max_age_days") is not None:
        want_age = int(spec["max_age_days"])
        age = want_age if age is None else min(age, want_age)

    return Policy(tables=tables, max_rows=max_rows, max_age_days=age)


def combine(policies: list[Policy]) -> Policy:
    """多个角色叠加。

    RBAC 是**加法**：一个人身兼两职，看得到的是两者之和。表取并集、
    行上限取大 —— 但每个策略本身都已经是实例白名单的子集，所以并集
    仍然是子集，「只能收窄」这条不变量不受影响。

    内置默认现在是空的，所以这个函数在默认部署上恒等于"不收窄"；
    它仍然要正确，因为部署方配了 role_policies 时走的就是这条路。
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

    # 期限取**最长**：与 max_rows 取大同一个道理，身兼两职看得到两者之和
    ages = [p.max_age_days for p in policies]
    max_age = None if any(a is None for a in ages) else max(ages)

    return Policy(tables=tables, max_rows=max_rows, max_age_days=max_age)


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
    raw = {**raw, "_role_window_days": policy.max_age_days}

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
    """连接串。环境变量优先，其次配置里的 identity.dsn。

    **回落到 ASKDB_SOURCES_DSN 是有意的**，且与 sources.py 那边完全对称
    （它缺省回落到 ASKDB_IDENTITY_DSN）：成员名册与数据源注册表同属
    「askdb 自己的元数据」，默认同库，配任意一个变量即可跑起来 —— 少一把
    要分发、要轮转、要在两处保持一致的凭据。要把两者分库，显式设另一个。

    口令仍由 identity.password_env 指定，因此同库部署时把它指向
    ASKDB_SOURCES_PASSWORD 即可，不必再建一份。
    """
    section = cfg.raw.get("identity") or {}
    return (os.environ.get(DSN_ENV)
            or os.environ.get("ASKDB_SOURCES_DSN")
            or str(section.get("dsn") or "")).strip()


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
         # 两格给真值。页面此前把这三格写死成设计稿取值，
         # 于是「数据期限 90 DAYS」在后端根本没有对应字段时也照样显示 ——
         # 权限体系最怕的就是"配了但看不出有没有生效"，而这比看不出更糟：
         # 它显示了一个从未生效过的值。
         "max_age_days": policy_for(cfg, r.code).max_age_days}
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


#: 成员筛选条上「关联状态」那个下拉的四档。all 是不筛；其余三档互斥，
#: 且**配置内置单独成档**：那些人 auth_user_id 恒为 None，混进"未绑定"里
#: 会让人以为改一下就能绑上，而它们由配置文件管，页面上根本动不了。
MEMBER_BOUND_CHOICES = ("all", "bound", "unbound", "builtin")

#: 加入时间档。与任务中心、审计中心同一套取值（audit.SINCE_CHOICES），
#: 三页的筛选条手感必须一致。
MEMBER_SINCE_CHOICES = ("all", "today", "7d", "30d")


def _since_days(since: str) -> int:
    return {"today": 0, "7d": 7, "30d": 30}.get(since, -1)


def members_page(cfg: Config, role_code: str = "", *,
                 page: int = 1, page_size: int = 10,
                 q: str = "", bound: str = "all", since: str = "all") -> dict[str, Any]:
    """成员名册的一页 —— 与 list_members 同一份名单，但只读出这一页。

    名册由两段拼成：配置内置的那些（auth.accounts，进程内、条数固定）排在前，
    库表登记的排在后。所以分页也分两段算：起点还落在内置段里就先从内置切，
    剩下的额度再拿去库里 LIMIT/OFFSET；起点越过内置段之后，偏移量要**减掉
    内置的条数**，否则每页都会跳过同样多的行。

    去重必须落到 SQL 的 WHERE 里，不能像 list_members 那样读出来再滤：
    读出来再滤的话，count 数的行与最终列出的行不是同一批，页码会算错 ——
    最后一页可能是空的，而总数显示得比实际多。

    q / bound / since 是筛选条上的三个条件，**同样两段都要施加**：
      · q      —— 匹配网关用户名、姓名、备注（大小写不敏感）
      · bound  —— MEMBER_BOUND_CHOICES 四档；builtin 那档只剩内置段
      · since  —— 加入时间。内置条目没有 created_at（页面上显示"—"），
        所以选了任何一档时间，内置段整体不参与 —— 拿"今天"去套一个
        没有时间的条目，放行和不放行都是在编一个它没有的事实。
    一条线上的三个条件都作用在**筛之后**的名单上，total 与页码按它算。
    """
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    q = (q or "").strip()
    bound = bound if bound in MEMBER_BOUND_CHOICES else "all"
    since = since if since in MEMBER_SINCE_CHOICES else "all"

    all_builtins = builtin_members(cfg, role_code)
    builtins = all_builtins
    if bound in ("bound", "unbound") or since != "all":
        builtins = []                 # 见上：内置既非已绑也非未绑，且没有加入时间
    if q:
        needle = q.lower()
        builtins = [m for m in builtins
                    if needle in str(m.get("username") or "").lower()
                    or needle in str(m.get("display_name") or "").lower()
                    or needle in str(m.get("note") or "").lower()]

    where: list[str] = []
    params: list[Any] = []
    if role_code:
        where.append("role_code = %s")
        params.append(role_code)
    # 与内置条目是同一个人的不重复列（list_members 的 seen 集合，搬到 SQL 上）
    # **去重按全部内置条目算**，不是按筛完剩下的那些：筛掉一个内置条目
    # 不等于库里那条重复行该冒出来顶替它，否则同一个人会随筛选条件时隐时现。
    for m in all_builtins:
        where.append("NOT (role_code = %s AND lower(username) = %s)")
        params.extend([m["role_code"], m["username"].lower()])
    # 角色 + 去重这两条是**可见范围**，不是手上的筛选：筛之前的总数按它算
    base_clause = (" WHERE " + " AND ".join(where)) if where else ""
    base_params = list(params)
    if q:
        where.append("(username ILIKE %s OR coalesce(display_name,'') ILIKE %s"
                     " OR coalesce(note,'') ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if bound == "bound":
        where.append("auth_user_id IS NOT NULL")
    elif bound == "unbound":
        where.append("auth_user_id IS NULL")
    elif bound == "builtin":
        where.append("false")         # 内置段之外没有内置条目
    days = _since_days(since)
    if since == "today":
        where.append("created_at >= date_trunc('day', now())")
    elif days > 0:
        where.append("created_at >= now() - make_interval(days => %s)")
        params.append(days)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    counted = _rows(cfg, "SELECT COUNT(*) FROM askdb_role_members" + clause,
                    tuple(params))
    db_total = int(counted[0][0]) if counted else 0
    total = len(builtins) + db_total

    # 筛之前这个角色有多少人。页面上"命中 N / M 人"的 M 说的是这个数 ——
    # 只给筛完的数字，"筛完没有"与"这个角色本来就没人"在页面上分不开。
    if base_clause == clause:
        total_all = total                     # 没有任何筛选条件，省一次 COUNT
    else:
        counted_all = _rows(cfg, "SELECT COUNT(*) FROM askdb_role_members" + base_clause,
                            tuple(base_params))
        total_all = len(all_builtins) + (int(counted_all[0][0]) if counted_all else 0)

    start = (page - 1) * page_size
    out = builtins[start:start + page_size]
    want = page_size - len(out)
    if want > 0:
        rows = _rows(
            cfg,
            "SELECT id, role_code, auth_user_id, username, display_name, note,"
            " created_at, created_by FROM askdb_role_members" + clause +
            " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            tuple(params) + (want, max(start - len(builtins), 0)),
        )
        for r in rows:
            out.append(
                {"id": r[0], "role_code": r[1], "auth_user_id": r[2], "username": r[3],
                 "display_name": r[4], "note": r[5],
                 "created_at": r[6].isoformat(), "created_by": r[7],
                 # 登录接入前一律未绑定。如实标出来，别让人以为已经关联上网关账号了
                 "bound": r[2] is not None, "builtin": False})
    return {"items": out, "total": total, "total_all": total_all,
            "page": page, "page_size": page_size}


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
