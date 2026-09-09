"""askdb 自己那几份数据的 PostgreSQL 连接层。

**它连的不是被查询的库。** 用户的数据源在 askdb_sources 里登记、每次查询现连；
这里连的是 askdb 自己产生的东西：审计流水、审批流水、复核流水。两者的失败
取舍完全不同 —— 用户的库连不上只影响那一个数据源，这里连不上影响的是
"出事之后还查不查得到凭据"。

为什么单独一个模块，而不是复用 sources.py 里那套池子
----------------------------------------------------
sources 的池子是**查询路径上的池子**（每次问答都要读一次注册表），它的
max_size 与超时是按那条路径调的；审计写入是旁路、量大、可丢一条也不能拖慢
主链路。两者共用一个池，审计的写入尖峰会把查询路径的连接吃光 —— 那正是
最不该发生的方向。连接串三个环境变量的回落顺序仍与它们保持一致，所以
部署上不需要多配一把凭据。

连接串只从环境变量读，不进配置文件（与 sources / identity 同一条纪律）。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: 连接串。没配就依次回落到数据源注册表、成员名册用的那两把 —— 生产上
#: 这三者本来就是同一个库（askdb_meta），分开配等于多两把要轮转的凭据。
DSN_ENV = "ASKDB_STORE_DSN"
PASSWORD_ENV = "ASKDB_STORE_PASSWORD"
SCHEMA_ENV = "ASKDB_STORE_SCHEMA"

_SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class StoreUnavailable(RuntimeError):
    """凭据库不可用：没配连接串、连不上、认证失败。

    **必须与"查出来 0 行"分开。** 把"库挂了"显示成"没有审计记录"是这轮
    改造要消灭的那类静默失败 —— 看的人会以为系统很干净，实际上是瞎的。
    """


def raw_dsn() -> str:
    """连接串原文（不含口令）。没有任何一个环境变量时返回空串。"""
    return (os.environ.get(DSN_ENV)
            or os.environ.get("ASKDB_SOURCES_DSN")
            or os.environ.get("ASKDB_IDENTITY_DSN")
            or "").strip()


def configured() -> bool:
    """这台实例有没有凭据库可用。**只看环境变量，不建连** ——
    它会被健康检查与页面加载路径调用，不能在这里付一次 TCP 往返。"""
    return bool(raw_dsn())


def dsn() -> str:
    base = raw_dsn()
    if not base:
        raise StoreUnavailable(
            f"未配置凭据库：设置 {DSN_ENV}（或复用 ASKDB_SOURCES_DSN / "
            f"ASKDB_IDENTITY_DSN）。连接串只从环境变量读，不写进配置文件。")
    pwd = (os.environ.get(PASSWORD_ENV)
           or os.environ.get("ASKDB_SOURCES_PASSWORD") or "").strip()
    if pwd and "password=" not in base:
        base = f"{base} password={pwd}"
    return base


def schema() -> str:
    name = (os.environ.get(SCHEMA_ENV)
            or os.environ.get("ASKDB_SOURCES_SCHEMA") or "public").strip()
    if not _SCHEMA_RE.fullmatch(name):
        raise StoreUnavailable(f"{SCHEMA_ENV} 不是合法的 schema 名：{name}")
    return name


_pool: Any = None
_pool_key: tuple[str, str] | None = None
_lock = threading.Lock()


def _get_pool():
    """惰性建池，min_size=0 —— 进程启动时不连库。

    这条是硬要求：凭据库连不上时服务必须照样起得来，只是审计页报错。
    启动期建连等于把它变成启动依赖，而这套部署的既定取舍恰恰相反
    （见 deploy/k8s/askdb.yaml 里那两个 optional: true 的 Secret）。
    """
    global _pool, _pool_key
    key = (dsn(), schema())
    with _lock:
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
        conn_dsn, name = key
        _pool = ConnectionPool(
            conn_dsn, min_size=0, max_size=6, timeout=5, max_idle=300,
            kwargs={"autocommit": True, "connect_timeout": 5},
            configure=(None if name == "public"
                       else lambda con, s=name: con.execute(f"SET search_path TO {s}")),
            open=True, name="askdb-store",
        )
        _pool_key = key
        return _pool


def reset_pool() -> None:
    """丢弃当前连接池（两个都丢）。测试换库、换 schema 时调；生产用不到。"""
    global _pool, _pool_key, _dict_pool, _dict_key
    with _lock:
        if _pool is not None:
            _pool.close()
        if _dict_pool is not None:
            _dict_pool.close()
        _pool, _pool_key = None, None
        _dict_pool, _dict_key = None, None


@contextmanager
def connect():
    """借一条连接。连不上一律转成 StoreUnavailable —— 调用方只需要认得
    这一种异常，不必知道底下是 psycopg 还是别的什么。"""
    try:
        pool = _get_pool()
        with pool.connection() as con:
            yield con
    except StoreUnavailable:
        raise
    except Exception as e:                             # 建连失败、认证失败、超时
        raise StoreUnavailable(f"凭据库连接失败：{e}") from e


_dict_pool: Any = None
_dict_key: tuple[str, str] | None = None


def dict_pool():
    """行工厂为 dict 的连接池 —— **langgraph 的 PostgresSaver 只认这种**。

    单开一个池而不是把主池改成 dict_row：本模块自己的查询按元组读（列顺序
    与 SQL 一一对应），换成字典要改一圈调用点，而检查点是外部库在用，
    它的要求不该反过来决定我们自己的读法。

    检查点的写入频率比审计高一个量级（一次问答每个节点一次），所以单独给它
    池子还有一个好处：写检查点的尖峰不会把审计写入的连接吃光。
    """
    global _dict_pool, _dict_key
    key = (dsn(), schema())
    with _lock:
        if _dict_pool is not None and _dict_key == key:
            return _dict_pool
        if _dict_pool is not None:
            _dict_pool.close()
            _dict_pool, _dict_key = None, None
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as e:                       # pragma: no cover
            raise StoreUnavailable(
                '未安装 psycopg_pool：uv pip install "psycopg[binary,pool]"') from e
        conn_dsn, name = key
        _dict_pool = ConnectionPool(
            conn_dsn, min_size=0, max_size=6, timeout=5, max_idle=300,
            kwargs={"autocommit": True, "connect_timeout": 5, "row_factory": dict_row},
            configure=(None if name == "public"
                       else lambda con, s=name: con.execute(f"SET search_path TO {s}")),
            open=True, name="askdb-checkpoints",
        )
        _dict_key = key
        return _dict_pool


def _is_undefined_table(e: Exception) -> bool:
    """这个异常是不是"表还没建出来"。

    2026-09-09 修：原来 rows() 里写的是 `except psycopg.errors.UndefinedTable`，
    而那一句**永远不会命中** —— connect() 是个上下文管理器，with 体里抛出的
    异常会在它的 yield 处被接住、包成 StoreUnavailable 再抛，等到 rows() 这一层
    时类型已经变了。于是"表不存在按空处理"这条写着的行为一直没有生效，
    实际表现是审计接口 503。这里改成认包装之后的原因。
    """
    import psycopg

    cause = e.__cause__ if isinstance(e, StoreUnavailable) else e
    return isinstance(cause, psycopg.errors.UndefinedTable)


def rows(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """读一次库。**表还没建出来时按空处理** —— 那等于"还没有任何记录"，
    与查出来 0 行是同一回事；连不上、认证失败仍照实抛。"""
    try:
        with connect() as con:
            return con.execute(sql, params).fetchall()
    except Exception as e:
        if _is_undefined_table(e):
            return []
        raise


def iter_rows(sql: str, params: tuple[Any, ...] = (), *,
              batch_size: int = 500) -> Iterator[tuple[Any, ...]]:
    """流式读一次库 —— **结果不整份落进内存**，按批从服务端取回。

    与 rows() 的差别只在这一点，而这一点决定了审计那几页能不能开：
    rows() 走的是客户端游标，libpq 会先把整个结果集收进内存再交给
    Python，于是"一个月 45 万行审计"这种查询在拿到第一行之前就已经把
    Pod 撑爆了。命名游标让 PostgreSQL 保管结果集，客户端每次只取 batch_size 行。

    **调用方必须把它消费完，或者及时关掉。** 生成器活着的时候占着一条池子
    里的连接（池子只有 6 条），挂在那里不动会把连接耗光。for 循环正常跑完、
    break、以及生成器被回收，三种情况 psycopg 都会收尾，不必手工关。

    命名游标要在事务里 DECLARE，而池子开的是 autocommit —— 不显式开事务块
    的话，每条 FETCH 各自隐式提交，游标当场就没了。这就是 with con.transaction()
    在这里的作用，不是为了写入的原子性。
    """
    try:
        with connect() as con:
            with con.transaction():
                with con.cursor(name="askdb_stream") as cur:
                    cur.itersize = batch_size
                    cur.execute(sql, params)
                    yield from cur
    except Exception as e:
        if _is_undefined_table(e):
            return                                     # 与 rows() 同一条口径
        raise


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with connect() as con:
        con.execute(sql, params)
