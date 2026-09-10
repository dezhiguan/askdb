"""配置加载与启动期一致性校验。

启动期就把配置错误暴露出来，而不是等到运行时生成 SQL 才发现
——例如租户列在表定义里根本不存在。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


#: 按列名判定个人信息列的内置模式。
#:
#: **这是脱敏的下限，不是它的全部。** 手写白名单（config/*-tables.yaml）可以
#: 逐列标 sensitive，但运行时数据源的白名单是结构扫描自动生成的 —— 那条路径上
#: 没有人来标，2026-09-06 的实测里 careermate 源把 users.phone 原样返回给了
#: 匿名访客。靠"记得标"来兜个人信息，就等于没兜。
#:
#: 判定按**词**而不是按子串。`phone_number` / `phoneNumber` / `PhoneNo` 会被
#: 切成同一组词，都能命中；而 `platform_role`（含 "lat"）、`telemetry`（含
#: "tel"）不会 —— 子串匹配在真实 schema 上误伤太多，误伤多了就会有人来关它，
#: 关掉的那一刻这层防护就没了。
SENSITIVE_WORDS: frozenset[str] = frozenset({
    "phone", "mobile", "tel", "telephone",
    "email", "mail",
    "passport", "ssn", "idcard", "idno",
    "password", "passwd", "pwd", "secret", "token", "apikey", "accesskey",
    "address", "addr",
    "realname", "fullname",
    "wechat", "openid", "unionid",
    "birthday", "birthdate", "dob",
    "lat", "lng", "latitude", "longitude",
})

#: 拼起来才成立的模式：单看任何一个词都不敏感（`card` / `id` / `no` 到处都是），
#: 连在一起才是身份证号、银行卡号。对**去下划线后的整列名**做子串匹配。
SENSITIVE_COMPOUNDS: tuple[str, ...] = (
    "idcard", "identitycard", "cardno", "bankcard", "creditcard",
    "realname", "apikey", "accesskey", "openid", "unionid",
)


def _words(name: str) -> set[str]:
    """把列名切成词：下划线、大小写边界、数字边界都算。"""
    parts = re.split(r"[^0-9A-Za-z]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])",
                     str(name))
    return {p.lower() for p in parts if p}


def looks_sensitive(column_name: str) -> bool:
    """列名看着像个人信息 / 凭据吗。

    单独成函数是为了让"哪些列被自动判成敏感"这件事可测、可 grep，
    而不是埋在 dataclass 里的一行推导式。
    """
    flat = str(column_name).replace("_", "").lower()
    return bool(_words(column_name) & SENSITIVE_WORDS) or any(
        c in flat for c in SENSITIVE_COMPOUNDS)


@dataclass
class Column:
    name: str
    type: str
    desc: str = ""
    enum: list[str] = field(default_factory=list)
    tenant: bool = False
    #: 个人信息列（P03）。**敏感性是数据的属性，不是角色的属性** ——
    #: 声明在这里，角色只决定看不看得到原值，不需要每个角色各列一份清单。
    #: 清单分散到角色上，加一张表就要改 N 处，漏掉一处就是一次泄露。
    sensitive: bool = False
    #: 数据期限所依据的时间列。语义与 tenant 完全对称：
    #: 那个标"按谁隔离"，这个标"按哪一列算新旧"。
    time: bool = False
    #: 缓存/派生计数列 —— 值由别处维护，与真实计数会漂移。
    #:
    #: 典型是 knowledge_bases.doc_count 这类给列表页排序用的计数器。
    #: 2026-09-10 生产实测：同一天同一分钟，「文档数最多的 5 个知识库」走
    #: COUNT(documents) 得 7,901，「哪个知识库文档量最大」走 doc_count 得 7,539，
    #: 差 362 篇，两次都是 100 分可信度、都没提口径差异。
    #:
    #: 标了不等于禁用 —— 列表页排序本来就该用它。标的作用是让链路认得出
    #: "这个数来自缓存计数器"，从而在口径声明里写明、在可信度上扣一分。
    #: 靠列 desc 里写"⚠️ 可能漂移"是不够的：那句话只有模型看得见，
    #: 而恰恰是模型没当回事。
    cached_counter: bool = False

    def __post_init__(self) -> None:
        # 内置模式命中即敏感，**配置只能往上加、不能往下摘**（`sensitive: false`
        # 摘不掉 phone）。理由与 identity.narrow 的"只能收窄不能放宽"是同一条：
        # 配错的方向必须是偏严的那一侧。要放开某一列，得改这里的模式并留下记录，
        # 那是一次带评审的改动，不是一次点击。
        #
        # 落点选 __post_init__ 而不是各个解析函数，是因为 Column 有三条构造路径
        # （手写白名单 parse_tables、运行时源 sources.derive_config、测试固件），
        # 分开写就迟早漏掉一条 —— 而漏掉的那条正是这次出事的那条。
        if not self.sensitive and looks_sensitive(self.name):
            self.sensitive = True


@dataclass
class Table:
    name: str
    desc: str
    aliases: list[str]
    columns: dict[str, Column]
    # 表本身没有租户列时，用一段谓词表达间接归属。
    # 支持 {ref}（表名或别名）与 {ctx}（当前租户 ID）两个占位符。
    tenant_filter: str | None = None
    # 显式声明该表与租户无关（如全局字典表）。必须是有意为之，不能靠遗漏。
    tenant_exempt: bool = False
    # 显式声明该表没有时间维度（维表、字典表）。与 tenant_exempt 同一个理由：
    # 必须是有意为之。漏标的表在有数据期限的角色下会被 R-19 拒掉，
    # 那是**故意的** —— 静默放行等于把"只能看 90 天"这句话变成一句空话。
    time_exempt: bool = False

    @property
    def tenant_column(self) -> str | None:
        for c in self.columns.values():
            if c.tenant:
                return c.name
        return None

    @property
    def time_column(self) -> str | None:
        for c in self.columns.values():
            if c.time:
                return c.name
        return None

    @property
    def sensitive_columns(self) -> list[str]:
        return [c.name for c in self.columns.values() if c.sensitive]

    @property
    def has_tenancy(self) -> bool:
        return bool(self.tenant_column or self.tenant_filter or self.tenant_exempt)


@dataclass
class Metric:
    name: str
    aliases: list[str]
    scope: list[str]
    expr: str | None = None
    predicate: str | None = None
    note: str = ""
    owner: str = ""
    # 这条表达式只在什么聚合语境下成立。
    #
    # expr 是**片段注入**：口径保证了表达式本身，保证不了它被放进什么查询里。
    # 「日均成本」= SUM(cost)/COUNT(DISTINCT stat_date)，模型若再 GROUP BY model，
    # 分母就从"全期天数"变成"该模型有记录的天数"，两个数都合法、都跑得出来、
    # 护栏一条都不会触发。粒度必须显式写下来并进提示词，否则它只活在 note 的
    # 自然语言里，靠模型自己读懂。
    grain: str = ""
    # 与之对照的"凭直觉写法"。用来算区分度：两种写法结果相同的口径，
    # 当前检验不出模型有没有真的用它 —— 这件事原来靠人工在配置里写注释标注。
    naive: str = ""

    def matches(self, question: str) -> bool:
        return any(k and k in question for k in [self.name, *self.aliases])


@dataclass
class Config:
    root: Path
    raw: dict[str, Any]
    tables: dict[str, Table]
    metrics: list[Metric]
    # 自己是从哪份配置加载的。检查点库、审计日志都跟着配置走，
    # 复现一条 trace 必须用同一份配置 —— 少了它，replay 命令给不全。
    path: str = ""
    # 这份配置是以谁的身份收窄出来的（identity.narrow 设置）。
    # 挂在配置上而不是层层传参：收窄后的配置本来就是"某个角色眼里的配置"，
    # 审计要记的也正是这一条 —— 结果出自谁的可见范围。
    role: str = ""
    # 调用方账号（匿名为空）。角色决定**能看什么**，账号决定**任务归谁** ——
    # 中断任务只能由发起人自己续跑，没有这一条就无从判断归属。
    user: str = ""
    # 这份配置对应哪个运行时数据源（内置源为空）。
    # 与 role 同理挂在配置上：多源之后，审计不记数据源就说不清
    # "这条 SQL 到底打的哪个库" —— 而那是审计存在的全部意义。
    source_id: str = ""
    source_name: str = ""

    # --- 常用快捷访问 ---
    @property
    def has_default_source(self) -> bool:
        """配置里是否声明了默认数据源。

        `datasource:` 段是可选的：一套只用运行时数据源的部署，不该被逼着
        在配置里先写一个用不上的库。没有它时，不带数据源的查询直接被拒 ——
        而不是悄悄落到某个"碰巧还在配置里"的库上。
        """
        return bool(self.raw.get("datasource"))

    @property
    def default_source_ref(self) -> str:
        """部署方指定的**默认运行时数据源**（写 id 或写名字都认），没写就是空串。

        为什么需要这一行：`datasource:` 段撤掉之后，"不带 source 的调用落到哪个
        库"实际上由注册顺序决定（界面回落到列表第一个）—— 而注册顺序不表达
        任何意图。线上就出现过这个后果：启动配置里的白名单与业务口径全是
        ragforge 的表，界面默认选中的却是先注册的 careermate 库，于是口径页
        整页标"不可查"，看的人只会以为口径配错了。

        它**不是**内置数据源的替身：连接串仍然只在注册表里，这里写的只是
        "同样这几个源，默认用哪一个"。指到一个不存在或重名的源上，
        不会悄悄回落到别的库 —— 由调用处如实报出来（server._default_source）。
        """
        return str(self.raw.get("datasources", {}).get("default", "") or "").strip()

    def _ds(self) -> dict[str, Any]:
        if not self.has_default_source:
            raise ValueError(
                "本实例未配置默认数据源（config 里没有 datasource 段）。"
                "发起查询时必须显式指定数据源。"
            )
        return self.raw["datasource"]

    @property
    def db_path(self) -> Path:
        return (self.root / self._ds()["path"]).resolve()

    @property
    def db_type(self) -> str:
        """未配置默认数据源时返回空串 —— 调用方据此判断有没有库可连。"""
        return str(self.raw.get("datasource", {}).get("type", ""))

    @property
    def upstream(self) -> str:
        """连接串背后真正的库地址。

        经 SSH 隧道连接时，dsn 里写的是本地转发端口（127.0.0.1:15432），
        那是**运维细节，不是数据源身份**。界面与评测出处若照搬它，
        读的人会以为数据来自本机 —— 而出处这一栏存在的全部意义，
        就是说清"这组数字到底出自哪个库"。
        配置里显式声明，不做猜测。
        """
        return str(self.raw.get("datasource", {}).get("upstream", "") or "").strip()

    @property
    def dsn(self) -> str:
        """PostgreSQL / MySQL 连接串。密码只走环境变量，不落配置文件。"""
        import os

        ds = self.raw.get("datasource", {})
        base = str(ds.get("dsn", ""))
        env = ds.get("password_env")
        pwd = os.environ.get(env) if env else None
        if pwd and "password=" not in base:
            base = f"{base} password={pwd}"
        return base.strip()

    @property
    def dialect(self) -> str:
        """sqlglot 方言名 —— 与数据源类型对应，改写后的 SQL 才能正确回写。"""
        # 没有默认数据源就没有方言可言 —— 交给 _ds() 报那句能看懂的话，
        # 而不是让调用方吃一个裸 KeyError('')。
        if not self.has_default_source:
            self._ds()
        return {"duckdb": "duckdb", "postgresql": "postgres",
                "mysql": "mysql"}[self.db_type]

    @property
    def strict_account_check(self) -> bool:
        """接入自检里的**账号姿态**项要不要阻断接入。

        默认 false：拿一个高权账号（root / superuser）接库这件事要能做成，
        由界面把没过的项一直摆着。设为 true 恢复成"一项不过就不许接入"。

        无论开关取什么值，**实证那一项（写操作实探）永远阻断** ——
        它真发一条写语句、由引擎拒掉，是这条链路上唯一不靠声明的证据。
        """
        return bool(self.raw.get("datasources", {}).get("strict_account_check", False))

    @property
    def tenant_enabled(self) -> bool:
        """是否做租户隔离。

        单租户库（整库属于同一主体）设为 false —— 大多数数据库其实是单租户，
        强行要求每张表交代归属只会逼人乱填。关掉必须是有意为之：
        自检与界面都会显著标出当前处于单租户模式。
        """
        return bool(self.raw["tenant"].get("enabled", True))

    @property
    def window_days(self) -> int | None:
        """当前角色的数据时间窗口（天）。None = 不限。

        由 identity.narrow 写进 raw。护栏从这里取值，因此它不需要知道
        "角色"是什么 —— 与 max_rows / tables 完全同一条路径。
        """
        v = self.raw.get("_role_window_days")
        return None if v is None else int(v)

    @property
    def window_enforceable(self) -> bool:
        """时间窗口在这个数据源上能不能落地。

        运行时数据源的表结构来自扫描，扫描看不出哪一列该用来算新旧，
        因此那里一律关闭（见 sources.derive_config），并由界面标注。
        """
        return bool(self.raw.get("_window_enforceable", True))

    @property
    def scan_waiver(self) -> bool:
        """本次调用是否已获批准，可以跳过 R-11 扫描阈值一次。

        由 server 在校验完审批单后写进 raw —— 与 role/tables 一样，
        执行链路只从配置取值，不需要知道"审批"这件事的存在。
        """
        return bool(self.raw.get("_scan_waiver", False))

    @property
    def tenant_column(self) -> str:
        return self.raw["tenant"]["column"]

    @property
    def default_org(self) -> int:
        return int(self.raw["tenant"]["default_ctx"])

    @property
    def max_rows(self) -> int:
        return int(self.raw["guard"]["max_rows"])

    @property
    def metadata_timeout_ms(self) -> int:
        """元数据扫描（接入向导列表、取字段）的时间预算。

        与 guard.statement_timeout_ms 是两件事：那一条是 R-12，管**用户查询**
        能跑多久；这一条管的是"问清这个库有哪些表"能等多久，代价跟库的规模
        走。共用一个值的后果见 executor 里 metadata_window 的说明。

        不配就是 20 秒 —— 老配置文件照旧能起，不必为了升级去改每一份。
        20 而不是 30：入口 nginx 对这条路径的 proxy_read_timeout 是 30 秒，
        配得比它大只会把一次可解释的超时换成一个 504。
        """
        return int(self.raw["guard"].get("metadata_timeout_ms", 20_000))

    @property
    def max_retry(self) -> int:
        return int(self.raw["guard"]["max_retry"])

    @property
    def deny_functions(self) -> set[str]:
        return {f.lower() for f in self.raw["guard"].get("deny_functions", [])}

    @property
    def allow_select_star(self) -> bool:
        return bool(self.raw["guard"].get("allow_select_star", False))

    @property
    def audit_log(self) -> Path:
        return (self.root / self.raw["observability"]["audit_log"]).resolve()

    @property
    def checkpoint_db(self) -> Path:
        obs = self.raw["observability"]
        return (self.root / obs.get("checkpoint_db", "./data/checkpoints.sqlite")).resolve()

    @property
    def daily_quota(self) -> int:
        """0 或负数表示不限量。"""
        return int(self.raw["observability"].get("daily_quota", 0) or 0)

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw["llm"]

    def tenant_tables(self) -> set[str]:
        """需要注入租户约束的表 —— 强制改写 R-10 的作用范围。

        含两类：表上直接有租户列的，以及靠 tenant_filter 声明间接归属的。
        单租户模式下为空集，R-10 整体不参与。
        """
        if not self.tenant_enabled:
            return set()
        return {t.name for t in self.tables.values()
                if (t.tenant_column or t.tenant_filter) and not t.tenant_exempt}

    def api_key(self) -> str | None:
        return os.environ.get(self.llm["api_key_env"]) or None


def _load_yaml(p: Path) -> dict[str, Any]:
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_dotenv(root: Path) -> None:
    """把项目根目录的 .env 读进环境变量。

    刻意自己实现而不引入 python-dotenv：只需要 KEY=VALUE 这一种语法，
    多一个依赖不值得。**已存在的环境变量优先**，便于用 export 临时覆盖。
    """
    p = root / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def parse_tables(spec: list[dict[str, Any]]) -> dict[str, Table]:
    """把白名单 YAML 的表段解析成 Table。

    单独拎出来是给"换一份白名单"的调用方用的（测试固件就得这么干）——
    否则只能整份配置重新 load，白名单就被迫跟着开发配置一起漂。
    """
    tables: dict[str, Table] = {}
    for t in spec:
        cols = {
            name: Column(
                name=name,
                type=col.get("type", ""),
                desc=col.get("desc", ""),
                enum=col.get("enum", []) or [],
                tenant=bool(col.get("tenant", False)),
                sensitive=bool(col.get("sensitive", False)),
                time=bool(col.get("time", False)),
                cached_counter=bool(col.get("cached_counter", False)),
            )
            for name, col in t["columns"].items()
        }
        tables[t["name"]] = Table(
            name=t["name"], desc=t.get("desc", ""), aliases=t.get("aliases", []) or [],
            columns=cols,
            tenant_filter=t.get("tenant_filter"),
            tenant_exempt=bool(t.get("tenant_exempt", False)),
            time_exempt=bool(t.get("time_exempt", False)),
        )
    return tables


def load(config_path: str | Path = "config/askdb.yaml") -> Config:
    cfg_path = Path(config_path).resolve()
    root = cfg_path.parent.parent
    _load_dotenv(root)
    raw = _load_yaml(cfg_path)

    tables = parse_tables(_load_yaml(root / raw["tables_file"])["tables"])

    metrics = [Metric(**m) for m in (_load_yaml(root / raw["metrics_file"])["metrics"] or [])]

    cfg = Config(root=root, raw=raw, tables=tables, metrics=metrics,
                 path=str(cfg_path.relative_to(root) if cfg_path.is_relative_to(root) else cfg_path))
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """启动期校验 —— 配置错误必须在这里炸，不能拖到生成 SQL 时。"""
    errs: list[str] = []

    tcol = cfg.tenant_column
    if cfg.tenant_enabled:
        if not cfg.tenant_tables():
            errs.append(
                "开启了租户隔离，但没有任何表声明归属。"
                "若这是单租户库，请显式设置 tenant.enabled: false"
            )
        for t in cfg.tables.values():
            col = t.tenant_column
            if col and col != tcol:
                errs.append(f"表 {t.name} 的租户列是 {col}，与全局配置 tenant.column={tcol} 不一致")
            # 白名单里的表必须对租户归属有明确交代 —— 失败要朝安全的方向失败。
            # 漏配一张表就是一条越权路径，所以这里不给默认值。
            if not t.has_tenancy:
                errs.append(
                    f"表 {t.name} 未声明租户归属：需要标记租户列 tenant:true、"
                    f"或给出 tenant_filter、或显式 tenant_exempt:true；"
                    f"若整库单租户，改用 tenant.enabled: false"
                )
            if t.tenant_filter and "{ctx}" not in t.tenant_filter:
                errs.append(f"表 {t.name} 的 tenant_filter 缺少 {{ctx}} 占位符，租户不会被代入")
            # {ref} 同样必需：谓词要绑到**具体的表实例**上。
            # 缺了它，单表查询恰好还对，但自连接
            # （FROM documents a JOIN documents b）下两个别名会共用一条无限定
            # 谓词，行为不可预期 —— 配置看着是对的，只在特定语法下才出错，
            # 属于最难发现的一类越权路径。
            if t.tenant_filter and "{ref}" not in t.tenant_filter:
                errs.append(
                    f"表 {t.name} 的 tenant_filter 缺少 {{ref}} 占位符，"
                    f"谓词无法绑定到具体表实例（自连接场景会失效）")
    else:
        # 单租户模式下仍然禁止半吊子配置：既然说了整库同属一个主体，
        # 就不该再有表声明自己的租户归属，否则两套语义并存。
        declared = [t.name for t in cfg.tables.values() if t.tenant_column or t.tenant_filter]
        if declared:
            errs.append(
                f"tenant.enabled=false（单租户）与表级租户声明冲突：{'、'.join(declared)}。"
                f"要么开启租户隔离，要么去掉这些声明"
            )

    known = set(cfg.tables)
    for m in cfg.metrics:
        for s in m.scope:
            if s not in known:
                errs.append(f"口径「{m.name}」的 scope 引用了不在白名单里的表：{s}")
        if not (m.expr or m.predicate):
            errs.append(f"口径「{m.name}」既没有 expr 也没有 predicate")

    # 行级安全只有 PostgreSQL 有。MySQL 落到这条上是**故意**的：那边没有 RLS，
    # 选了 rls 却静默降级成单层谓词，就等于把配置里写着的第二道防线变成一句空话。
    if (cfg.has_default_source
            and cfg.raw["tenant"]["mode"] in ("rls", "rls_and_predicate")
            and cfg.db_type != "postgresql"):
        errs.append(f"tenant.mode={cfg.raw['tenant']['mode']} 需要 PostgreSQL，当前是 {cfg.db_type}"
                    f"（该数据源没有行级安全，请改用 tenant.mode: predicate）")

    if errs:
        raise ValueError("配置校验未通过：\n  - " + "\n  - ".join(errs))
