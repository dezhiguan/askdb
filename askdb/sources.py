"""运行时数据源注册表。

启动配置里的那个数据源是**内置源**：它定义了这套部署的护栏阈值、租户策略与
业务口径。本模块管的是在它之外、由界面在运行时添加的只读数据源。

记录存在 PostgreSQL 的 askdb_sources 表里（2026-09-06 前是 var/sources/*.yaml，
迁移见 scripts/migrate_sources_to_pg.py）。连接串只从环境变量来 —— 数据源
不进配置文件，因此改数据源不必重启，数据源出问题也影响不到启动。

内置源不可编辑，但**可以删除**（`drop_default_source`）—— 一套只用运行时数据源
的部署，不该被逼着在配置里留一个用不上的库。删除受与新增同一个开关约束，
且必须先有别的数据源可用：删到一个源都不剩，等于把实例变砖。

三条纪律，都是被"页面能改连接"这件事本身逼出来的：

- **默认关闭。** 服务端会按用户填的地址主动发起连接，而 askdb 不设账号体系。
  在公开实例上开放它，等于给出一个无鉴权的内网探测入口。因此由配置开关
  `datasources.allow_runtime_add` 控制，默认 false，对外实例显式写死为 false。

- **口令优先走环境变量。** 直接提交的密码用主密钥加密后落盘；主密钥自身只从
  环境变量来，没配主密钥就拒绝保存明文口令 —— 宁可这条路走不通，
  也不要在磁盘上留一份可读的数据库口令。

- **新源的表默认全不开放。** 扫描只负责"看得见"，开放与否是单独一步。
  白名单同时是安全边界与准确率边界，默认全开等于把两条边界一起取消。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


from .config import Column, Config, Table

# 只有这两种后端有真正的执行与护栏实现（见 executor 的 _DuckBackend/_PgBackend
# 与 Config.dialect）。列出别的类型就是在承诺不存在的能力。
SUPPORTED_TYPES = ("postgresql", "duckdb")

_ID_RE = re.compile(r"src_[0-9a-f]{12}")
_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


class SourceError(ValueError):
    """调用方应把它转成 400，并把 message 原样给用户看。"""


@dataclass
class Source:
    id: str
    name: str
    type: str
    dsn: str
    #: 环境归属。**只是界面标签，不参与鉴权**（2026-09-06 起）：角色与数据源
    #: 已解绑 —— askdb 是共享平台，大家用的是同一批源，按角色挡"能连哪个库"
    #: 与这个前提冲突。它仍然要如实显示：顶栏据此告诉人"当前连的是哪一档"，
    #: 同一台机器上同时跑多个实例时，说错一次就会有人拿着另一个库的结论下判断。
    env: str = "test"
    upstream: str = ""                # 经隧道时的真实库地址
    password_env: str = ""
    password_enc: str = ""
    created_at: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)
    # 最近一次连接检查的结果。**要落盘。** 卡片上的「状态」「延迟」「最后
    # 检查」三格原先只活在浏览器 state 里，刷新即失忆 —— 而"这个源上次还
    # 通不通、什么时候通的"恰恰是重新打开这一页时第一个要看的东西。
    last_checked_at: str = ""
    last_ok: bool | None = None
    last_latency_ms: float | None = None
    last_visible_count: int | None = None      # 检查那一刻库里实际可见的表数

    @property
    def credential(self) -> str:
        if self.password_env:
            return self.password_env
        return "已加密存储" if self.password_enc else ""


# --------------------------------------------------------------------------
# 口令
# --------------------------------------------------------------------------

def _fernet():
    """主密钥来自 ASKDB_SECRET_KEY，用 scrypt 拉伸成 Fernet 密钥。

    没配主密钥就没有这条路 —— 调用方据此拒绝保存明文口令。
    """
    secret = os.environ.get("ASKDB_SECRET_KEY", "").strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet

    # 固定盐：主密钥本身就是秘密，盐在这里只用于域分离，不承担强度
    key = hashlib.scrypt(secret.encode("utf-8"), salt=b"askdb.sources",
                         n=2 ** 14, r=8, p=1, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_password(plain: str) -> str:
    f = _fernet()
    if f is None:
        raise SourceError(
            "未配置 ASKDB_SECRET_KEY，不能保存明文口令。"
            "请改用「环境变量名」方式，或先在服务端配置主密钥后重启。"
        )
    return f.encrypt(plain.encode("utf-8")).decode("ascii")


def resolve_password(src: Source) -> str | None:
    """取出实际口令。取不到返回 None —— 由连接层报错，这里不猜。"""
    if src.password_env:
        return os.environ.get(src.password_env) or None
    if src.password_enc:
        f = _fernet()
        if f is None:
            return None
        try:
            return f.decrypt(src.password_enc.encode("ascii")).decode("utf-8")
        except Exception:
            # 主密钥换过了。返回 None 让连接按"认证失败"报，
            # 而不是抛一个看不懂的密码学异常
            return None
    return None


# --------------------------------------------------------------------------
# 存储 —— PostgreSQL
# --------------------------------------------------------------------------
#
# 2026-09-06：从 `var/sources/*.yaml` 一源一文件改为 PG 单表。
#
# 换掉文件的三个理由，都是文件方案在这套部署下**已经**踩着的：
#
# - **两副本共享同一个 hostPath，而 yaml 写入无锁。** record_probe 每次探活
#   都整文件重写（连白名单一起），两个 Pod 同时探同一个源就可能写坏。
#   进库之后探活是一条只动四列的 UPDATE，白名单在物理上就不可能被带脏 ——
#   原来那句"只动这四个字段"是靠调用方自觉，现在是靠 SQL。
# - **重启不该改变数据源，数据源也不该影响启动。** 连接信息进库之后，
#   启动配置里再没有任何一条数据源，load() 读的是纯策略；库连不上也只是
#   数据源页报错，服务照起。
# - 跨节点部署时文件方案必废，PG 不用再改一次。
#
# 连接串**只从环境变量来，不进配置文件** —— 与 identity 同一条纪律
# （tests/test_identity.py 有一条断言在守它）。默认回落到身份库：两张表
# 同属"askdb 自己的元数据"，默认同库省一套运维；要分开，设 ASKDB_SOURCES_DSN。

#: 元数据库连接串。缺省回落到身份库的连接串。
DSN_ENV = "ASKDB_SOURCES_DSN"
#: 口令。DSN 里已写 password= 时以 DSN 为准。
PASSWORD_ENV = "ASKDB_SOURCES_PASSWORD"
#: 建表落在哪个 schema。测试用它做隔离，生产一般不设。
SCHEMA_ENV = "ASKDB_SOURCES_SCHEMA"

_SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")

#: 建表语句。幂等，省掉一套迁移工具（与 identity.ensure_schema 同一套做法）。
#:
#: created_at / last_checked_at 存 text 而不是 timestamptz：这两个值全链路
#: 都是带偏移量的 ISO 串（前端直接 new Date() 吃它），换成时间戳类型就要在
#: 读写两侧各加一次转换，而这次改的是存储介质，不是数据契约。
#:
#: tables 用 jsonb 而不是拆两张子表：白名单永远整体读写，拆表只会凭空多出
#: 一个事务边界，换不来任何查询能力 —— 没有任何代码路径需要"按列查表"。
_DDL = """
CREATE TABLE IF NOT EXISTS askdb_sources (
    id                 text PRIMARY KEY,
    name               text NOT NULL,
    type               text NOT NULL,
    dsn                text NOT NULL,
    env                text NOT NULL DEFAULT 'test',
    upstream           text NOT NULL DEFAULT '',
    password_env       text NOT NULL DEFAULT '',
    password_enc       text NOT NULL DEFAULT '',
    tables             jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at         text NOT NULL DEFAULT '',
    last_checked_at    text NOT NULL DEFAULT '',
    last_ok            boolean,
    last_latency_ms    double precision,
    last_visible_count integer
)
"""

#: 列顺序在读写两侧共用一份，避免 SELECT 与 Source 字段错位。
_COLS = ("id", "name", "type", "dsn", "env", "upstream", "password_env",
         "password_enc", "tables", "created_at", "last_checked_at",
         "last_ok", "last_latency_ms", "last_visible_count")


class StoreUnavailable(RuntimeError):
    """元数据库不可用。**与 SourceError 分开**：那个是"用户填错了"（400），
    这个是"这台实例没配好或库挂了"（503）—— 混成一种，界面就没法区分
    "你填的地址不对"和"我这边存不下"。
    """


def _store_dsn() -> str:
    dsn = (os.environ.get(DSN_ENV) or os.environ.get("ASKDB_IDENTITY_DSN") or "").strip()
    if not dsn:
        raise StoreUnavailable(
            f"未配置数据源元数据库：设置 {DSN_ENV}（或复用 ASKDB_IDENTITY_DSN）。"
            f"连接串只从环境变量读，不写进配置文件。"
        )
    pwd = (os.environ.get(PASSWORD_ENV) or "").strip()
    if pwd and "password=" not in dsn:
        dsn = f"{dsn} password={pwd}"
    return dsn


def _schema() -> str:
    name = (os.environ.get(SCHEMA_ENV) or "public").strip()
    if not _SCHEMA_RE.fullmatch(name):
        raise StoreUnavailable(f"{SCHEMA_ENV} 不是合法的 schema 名：{name}")
    return name


_pool: Any = None
_pool_key: tuple[str, str] | None = None
_pool_lock = threading.Lock()


def _get_pool():
    """惰性建池。**进程启动时不连库** —— min_size=0，第一次真正用到才建连。

    这条是硬要求而不是优化：数据源存储连不上时，服务必须照样起得来，
    只是数据源页报错。启动期就建连等于把元数据库变成启动依赖。

    连接池而不是每次现连（identity 那种写法）：数据源在每条查询的路径上
    都要读一次，裸连接会让每次问答多一次 TCP + 认证往返。
    """
    global _pool, _pool_key
    key = (_store_dsn(), _schema())
    with _pool_lock:
        if _pool is not None and _pool_key == key:
            return _pool
        if _pool is not None:
            _pool.close()
            _pool, _pool_key = None, None
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as e:                       # pragma: no cover
            raise StoreUnavailable(
                '未安装 psycopg_pool：uv pip install "psycopg[binary,pool]"') from e
        dsn, schema = key
        _pool = ConnectionPool(
            dsn, min_size=0, max_size=4, timeout=5, max_idle=300,
            kwargs={"autocommit": True, "connect_timeout": 5},
            configure=(None if schema == "public"
                       else lambda con, s=schema: con.execute(f"SET search_path TO {s}")),
            open=True, name="askdb-sources",
        )
        _pool_key = key
        return _pool


def reset_pool() -> None:
    """丢弃当前连接池。测试换库/换 schema 时调；生产用不到。"""
    global _pool, _pool_key
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool, _pool_key = None, None


def _conn():
    try:
        return _get_pool().connection()
    except StoreUnavailable:
        raise
    except Exception as e:
        raise StoreUnavailable(
            f"连接数据源元数据库失败：{str(e).splitlines()[0]}") from e


def ensure_schema() -> None:
    """建表。幂等。

    **只在写路径调。** 与 identity 同一条口径：读路径调 DDL 意味着任何一个
    读请求都能让服务端对元数据库执行一次建表 —— 幂等归幂等，但"读不改库"
    这条得守住。读路径改为容忍表不存在，见 _rows()。
    """
    schema = _schema()
    with _conn() as con:
        if schema != "public":
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            con.execute(f"SET search_path TO {schema}")
        con.execute(_DDL)


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """读一次库，**表还没建出来时按空处理**。

    没有 askdb_sources 表 = 一个数据源都还没登记过，与查出来 0 行是同一回事。
    注意这里只吞 UndefinedTable：连不上、认证失败都要照实抛 ——
    把"库挂了"显示成"没有数据源"，正是这轮改造要消灭的那种静默失败。
    """
    import psycopg

    try:
        with _conn() as con:
            return con.execute(sql, params).fetchall()
    except psycopg.errors.UndefinedTable:
        return []


def enabled(cfg: Config) -> bool:
    return bool(cfg.raw.get("datasources", {}).get("allow_runtime_add", False))


def _check_id(sid: str) -> str:
    if not _ID_RE.fullmatch(sid):
        raise SourceError("数据源 id 非法")
    return sid


def _to_source(row: tuple[Any, ...]) -> Source:
    d = dict(zip(_COLS, row))
    d["tables"] = d["tables"] or []
    return Source(**d)


def list_sources(cfg: Config) -> list[Source]:
    cols = ", ".join(_COLS)
    return [_to_source(r) for r in
            _rows(f"SELECT {cols} FROM askdb_sources ORDER BY created_at, id")]


def get_source(cfg: Config, sid: str) -> Source | None:
    cols = ", ".join(_COLS)
    rows = _rows(f"SELECT {cols} FROM askdb_sources WHERE id = %s", (_check_id(sid),))
    return _to_source(rows[0]) if rows else None


def save_source(cfg: Config, src: Source) -> None:
    """整条写入（新增或改白名单）。**探活字段不走这里** —— 见 record_probe。"""
    from psycopg.types.json import Jsonb

    _check_id(src.id)
    ensure_schema()
    vals = []
    for c in _COLS:
        v = getattr(src, c)
        vals.append(Jsonb(v) if c == "tables" else v)
    cols = ", ".join(_COLS)
    ph = ", ".join(["%s"] * len(_COLS))
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS if c != "id")
    with _conn() as con:
        con.execute(
            f"INSERT INTO askdb_sources ({cols}) VALUES ({ph}) "
            f"ON CONFLICT (id) DO UPDATE SET {upd}", tuple(vals))


def record_probe(cfg: Config, src: Source, *, ok: bool,
                 latency_ms: float | None = None,
                 visible_count: int | None = None) -> None:
    """把一次连接检查的结果落到数据源记录上。

    只动这四列。原来是"整份 yaml 重写，但请调用方只改这四个字段"，靠自觉；
    现在 UPDATE 的列清单就是约束 —— 一次失败的探测在物理上碰不到白名单。
    """
    src.last_checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    src.last_ok = ok
    src.last_latency_ms = latency_ms
    src.last_visible_count = visible_count
    ensure_schema()
    with _conn() as con:
        con.execute(
            "UPDATE askdb_sources SET last_checked_at = %s, last_ok = %s, "
            "last_latency_ms = %s, last_visible_count = %s WHERE id = %s",
            (src.last_checked_at, src.last_ok, src.last_latency_ms,
             src.last_visible_count, _check_id(src.id)))


def drop_default_source(cfg: Config) -> None:
    """把配置文件里的 `datasource:` 段整段删掉，并同步内存里的这份配置。

    直接改文本而不是 yaml.safe_dump 回写：这份配置里每一段都带着解释性注释，
    dump 一次全没了 —— 配置文件的注释就是这套部署的决策记录，
    删一个数据源不该顺手把它烧掉。

    删除范围是「datasource: 行 + 其下所有缩进行」，紧贴在它上面的注释块
    （中间不隔空行的连续 # 行）一并带走 —— 那些注释讲的就是这个数据源，
    留着会变成指向不存在配置的说明。
    """
    path = (cfg.root / cfg.path) if not Path(cfg.path).is_absolute() else Path(cfg.path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    start = next((i for i, ln in enumerate(lines) if ln.startswith("datasource:")), None)
    if start is None:
        cfg.raw.pop("datasource", None)
        return

    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        end += 1
    # 尾随空行留一个就够，多的收掉，免得删几次配置文件就散成一片空白
    while end < len(lines) and not lines[end].strip():
        end += 1

    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1

    rest = lines[:start] + lines[end:]
    path.write_text("".join(rest), encoding="utf-8")
    cfg.raw.pop("datasource", None)


def delete_source(cfg: Config, sid: str) -> bool:
    """删掉返回 True，本来就没有返回 False —— 调用方据此给 404。

    表不存在与记录不存在是同一回事（一个源都没登记过），不建表也不报错：
    删除是收窄操作，为它执行一次 DDL 属于多余的副作用。
    """
    import psycopg

    _check_id(sid)
    try:
        with _conn() as con:
            return con.execute(
                "DELETE FROM askdb_sources WHERE id = %s", (sid,)).rowcount > 0
    except psycopg.errors.UndefinedTable:
        return False


# --------------------------------------------------------------------------
# 新建
# --------------------------------------------------------------------------

#: 环境档位，按"离生产的距离"从远到近排列。
#:
#: 三档而不是两档：角色 scope 区分了「开发及测试环境」与「仅测试环境」，
#: 而枚举只有 test/prod_ro 时这两者无法区分 —— 枚举撑不起角色已经宣称的粒度。
ENVS: tuple[str, ...] = ("dev", "test", "prod_ro")

ENV_LABEL = {"dev": "DEV", "test": "TEST", "prod_ro": "PROD-RO"}


def build(*, name: str, type_: str, dsn: str, env: str = "test",
          upstream: str = "", password_env: str = "", password: str = "") -> Source:
    """校验并构造一条数据源。任何一项不合规都直接抛，不做静默兜底。"""
    name = (name or "").strip()
    dsn = (dsn or "").strip()
    if not name:
        raise SourceError("数据源名称不能为空")
    if type_ not in SUPPORTED_TYPES:
        raise SourceError(
            f"不支持的数据库类型：{type_}。"
            f"当前只有 {'、'.join(SUPPORTED_TYPES)} 有完整的护栏与执行实现。"
        )
    if not dsn:
        raise SourceError("连接串不能为空")
    if password_env and not _ENV_RE.fullmatch(password_env):
        raise SourceError("环境变量名不合规：需为大写字母开头的 3-64 位大写字母/数字/下划线")
    if password_env and password:
        raise SourceError("环境变量名与明文口令只能二选一")

    return Source(
        id=f"src_{uuid.uuid4().hex[:12]}",
        name=name,
        type=type_,
        dsn=dsn,
        # 非法值一律回退到最保守的那一档，不是回退到"默认档"：
        # 拼错 env 的后果必须是"看得更少"，不能是"看得更多"。
        env=env if env in ENVS else "test",
        upstream=(upstream or "").strip(),
        password_env=password_env,
        password_enc=encrypt_password(password) if password else "",
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        tables=[],
    )


def whitelist_from_scan(columns: dict[str, list[dict[str, Any]]],
                        picked: list[str]) -> list[dict[str, Any]]:
    """把扫描到的字段落成白名单条目。

    带上字段名与类型是硬要求：R-04（字段真实性）与 R-05（展开 SELECT *）
    靠它判定，缺了会退化成放行。
    """
    out = []
    for name in picked:
        cols = columns.get(name)
        if not cols:
            raise SourceError(f"表 {name} 取不到字段信息，无法开放")
        # 库里的注释直接当白名单里的说明用。手工填是填不完的（一个源 33 张表、
        # 几百个列），而"没有说明"不是一件中性的事：Schema 召回靠它给中文提问
        # 打分，全空就等于召回失灵。
        out.append({
            "name": name,
            "desc": next((c.get("table_desc", "") for c in cols
                          if c.get("table_desc")), ""),
            "aliases": [],
            # 运行时添加的数据源按单租户处理，见 derive_config 的说明
            "tenant_exempt": True,
            # 取值（枚举）跟着一起存：模型猜错取值的大小写，得到的是一条
            # 语法正确、结果恒空的 SQL —— 页面上看不出任何异常。取值来自
            # 列注释或 pg_stats，两者都拿不到时是空列表，行为与从前一致。
            "columns": {c["name"]: {"type": c["type"], "desc": c.get("desc", ""),
                                    **({"enum": c["enum"]} if c.get("enum") else {})}
                        for c in cols},
        })
    return out


# --------------------------------------------------------------------------
# 派生 Config
# --------------------------------------------------------------------------

def derive_config(base: Config, src: Source) -> Config:
    """按注册表里的一条数据源，派生出一份可直接交给 Executor/guard 的 Config。

    护栏阈值、模型、观测等沿用内置配置 —— 它们是这套部署的策略，不随数据源变。

    **租户隔离一律关闭。** 一次结构扫描看不出哪一列代表租户，更看不出像
    documents 那种靠 kb_id 间接归属的情况。猜错的后果是越权，所以这里如实
    按单租户处理，并由界面明确标注；要做行级隔离，仍然得写配置文件。
    """
    raw = copy.deepcopy(base.raw)
    ds: dict[str, Any] = {"type": src.type, "read_only": True}
    if src.type == "duckdb":
        ds["path"] = src.dsn
    else:
        ds["dsn"] = src.dsn
        pwd = resolve_password(src)
        if pwd:
            # 走内存，不回写配置文件
            ds["dsn"] = f"{src.dsn} password={pwd}"
    if src.upstream:
        ds["upstream"] = src.upstream
    raw["datasource"] = ds
    raw["tenant"] = {**raw.get("tenant", {}), "enabled": False}
    # **数据期限同理关闭。** 与上面那段是同一个问题的同一个答案：一次结构扫描
    # 看不出哪一列该用来算新旧（created_at？updated_at？还是某个业务日期），
    # 猜错的后果是**悄悄给出错误的数据** —— 比越权更难发现，因为结果看着正常。
    #
    # 这里不选"拒绝执行"：这套部署的每个源都是运行时源，拒绝等于把功能整个关掉；
    # 也不选"假装在拦" —— 那正是这轮改造要消灭的东西。所以如实关闭并标注，
    # 要真正按期限收窄，仍然得写配置文件把时间列声明出来。
    raw["_window_enforceable"] = False

    tables: dict[str, Table] = {}
    for t in src.tables:
        tables[t["name"]] = Table(
            name=t["name"],
            desc=t.get("desc", ""),
            aliases=t.get("aliases", []) or [],
            columns={
                cname: Column(name=cname, type=spec.get("type", ""),
                              desc=spec.get("desc", ""),
                              # 扫描时存下来的取值优先；没有就从注释里现解析，
                              # 这样**已经注册好的源不必重新扫描**也能享受到。
                              enum=list(spec.get("enum") or [])
                                   or enum_from_desc(spec.get("desc", "")))
                for cname, spec in (t.get("columns") or {}).items()
            },
            tenant_exempt=True,
        )

    return Config(root=base.root, raw=raw, tables=tables, metrics=[],
                  path=f"{base.path}#{src.id}", role=base.role,
                  source_id=src.id, source_name=src.name)


#: 列注释里枚举取值的写法：`状态：ON_SALE 在售 / OFF_SHELF 已下架`、
#: `审核结果：APPROVED 通过 / REJECTED 驳回 / PENDING 待审`。
#: 取值一律是全大写标识符，中文说明跟在后面 —— 这批库的注释统一是这个格式。
_ENUM_TOKEN = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
#: 这些全大写词是类型名/单位/表名缩写，不是取值，混进来会污染归一。
_ENUM_STOP = frozenset({
    "ID", "SKU", "SPU", "GMV", "SQL", "URL", "API", "JSON", "HTML", "CSV",
    "PDF", "UUID", "MD5", "IP", "SLA", "ROI", "NULL", "TRUE", "FALSE",
    "EAN", "UPC", "CNY", "USD", "KB", "MB", "AI", "JD", "RAG", "QA",
})


def enum_from_desc(desc: str) -> list[str]:
    """从列注释里把枚举取值抠出来。

    存在的理由：运行时数据源的 Column.enum 一直是空的，模型于是只能猜取值 ——
    猜错大小写（`'failed'` 而库里是 `'FAILED'`）语法完全正确、结果恒为空，
    解析失败率因此报 0%，而真值是 4.02%。报错会被看见，这种错不会。
    注释里其实写着取值，只是从来没人把它解析出来。

    宁可少认不可错认：只收全大写标识符，且要求至少两个 —— 单独一个大写词
    多半是缩写（"SKU ID"）而不是枚举。
    """
    vals = [v for v in _ENUM_TOKEN.findall(str(desc or "")) if v not in _ENUM_STOP]
    seen = list(dict.fromkeys(vals))
    return seen if len(seen) >= 2 else []


def to_public(src: Source, *, table_count: int | None = None) -> dict[str, Any]:
    """给接口用的形状。**连接串与口令都不出接口** —— dsn 里常常带用户名与
    主机，是内网拓扑信息；界面展示用 upstream 或脱敏后的主机名就够了。"""
    return {
        "id": src.id,
        "name": src.name,
        "type": src.type,
        "env": src.env,
        "host": src.upstream or _host_of(src.dsn, src.type),
        "credential": src.credential,
        "created_at": src.created_at,
        # **白名单张数，不是库里的表数。** 列表接口不去连库：一次列表请求
        # 要为每个源建一条出站连接，其中一个库挂了整页就跟着转圈。库里此刻
        # 实际有多少张，由最近一次连接检查落下的 last_visible_count 给出。
        "table_count": len(src.tables) if table_count is None else table_count,
        "last_checked_at": src.last_checked_at,
        "last_ok": src.last_ok,
        "last_latency_ms": src.last_latency_ms,
        "last_visible_count": src.last_visible_count,
        "builtin": False,
    }


def _host_of(dsn: str, type_: str) -> str:
    if type_ == "duckdb":
        return Path(dsn).name
    m = re.search(r"host=(\S+)", dsn)
    port = re.search(r"port=(\d+)", dsn)
    if not m:
        return ""
    return f"{m.group(1)}:{port.group(1)}" if port else m.group(1)
