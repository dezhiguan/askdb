"""只读执行 —— 护栏的最后一层，也是唯一真正碰数据的一层。

设计要点（技术设计说明书 §3.1、§4.1）：
  * 护栏优先做在**引擎层**而非应用层。应用层校验可能被绕过，引擎权限不会。
    DuckDB 以 read_only 打开；PostgreSQL 走独立只读角色 + 会话级只读事务；
    MySQL 没有账号级只读开关，走会话级只读事务，并在自检里另看一眼授权。
  * R-11 扫描行数阈值：执行前先 EXPLAIN 估算，超阈值直接打回。
  * R-12 语句超时：PostgreSQL 用原生 statement_timeout；MySQL 用
    max_execution_time（MariaDB 是 max_statement_time）；
    DuckDB 没有该设置，用看门狗线程调 interrupt() 实现。
  * R-13 结果行上限：即便 R-09 已注入 LIMIT，取数时仍再截断一次（纵深防御）。

三种后端的差异全部收在本模块，上层链路对数据源无感。
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config

# DuckDB EXPLAIN 计划里的基数估计。
#   1.5+ 渲染成 "~34,656 rows"；更早的版本用 "EC: 34656"。两种都认。
_EST_PATTERNS = (
    re.compile(r"~\s*([\d,]+)\s+rows?"),
    re.compile(r"EC:\s*([\d,]+)"),
)


class DataSourceError(RuntimeError):
    """数据源不可用 —— 带可执行的修复建议，直接透传给用户。

    retryable 区分两类错，它们的重试价值相反：

      · 连接断了、库不可达、认证失败 —— 重试纯属浪费，还会白烧模型 token
      · 语句超时 —— **可重试**：模型缩小时间范围或加筛选条件就可能过，
        与 R-11 干跑超限完全同理，而干跑超限是会进反思的。
        两者都不重试才是不一致。

    设计文档 §5 的节点表只写「execute 报错且 attempt < 2 → reflect」，
    未区分这两类；未提及的部分按上述判断处理。
    """

    def __init__(self, message: str, hint: str = "", retryable: bool = False):
        super().__init__(message)
        self.hint = hint
        self.retryable = retryable


class MaskUnresolved(RuntimeError):
    """脱敏判定不出投影来源 —— 这条结果不返回。

    与 DataSourceError 分开是因为处置完全不同：数据源报错可以重试（模型换个
    写法可能就通了），而这一条**也可以重试**，但重试的方向是"把 SQL 写得能被
    解析"，给模型的提示不一样。更重要的是它不能被当成"执行失败"记进健康度 ——
    库是好的，是我们自己不敢返回。
    """

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _fmt_ts(v: Any) -> str:
    """时间戳统一为带时区偏移的 ISO 文本，秒级即可 —— 界面上是给人看新鲜度的。"""
    try:
        return v.replace(microsecond=0).isoformat()
    except AttributeError:
        return str(v)


def _local_now() -> str:
    from datetime import datetime

    return _fmt_ts(datetime.now().astimezone())


def _elapsed_ms(t0: float) -> float:
    """自 t0 起的毫秒数。亚毫秒保留一位小数 —— int() 截断会把 0.4ms 写成
    0ms，而 0ms 在界面上读起来是「没测出来」而不是「很快」。"""
    ms = (time.perf_counter() - t0) * 1000
    # 10ms 以上取整：小数位在这个量级上是噪声，"68.0ms" 读起来还更假
    return round(ms, 1) if ms < 10 else round(ms)


def parse_kv_dsn(dsn: str, *, keep_password: bool = False) -> dict[str, str]:
    """把 keyword/value 连接串拆成字典。

    askdb 全链路只认这一种写法（`host=… port=… dbname=… user=…`）—— 界面拼的是
    它，注册表存的是它，`_host_of` / `_dsn_label` / 出处标识也都按它解析。
    MySQL 这一路要是再认一种 URI 写法，上面那几处就会对同一个源给出不同的
    显示，而"这组数字出自哪个库"正是它们存在的全部意义。
    """
    out: dict[str, str] = {}
    for kv in str(dsn or "").split():
        if "=" not in kv:
            continue
        key, _, val = kv.partition("=")
        key = key.strip().lower()
        if key == "password" and not keep_password:
            continue
        out[key] = val
    return out


#: **警示项** —— 不过也允许接入，但必须一直红着。
#:
#: 判据是"这一项证明的是什么"：
#:
#:   · 「写操作实探」证明的是**这条连接现在写不了** —— 真发一条 DELETE，
#:     由引擎拒掉。它是实证，所以它阻断。
#:   · 下面这几项证明的是**这个账号本来就不该写**（授权、连接数上限）。
#:     那是姿态，不是当下的能力。姿态不合格的账号仍然写不进去（会话级只读
#:     事务 + 实探把着），只是万一那层被绕开时没有第二道兜底。
#:
#: 2026-09-10 按 @guandezhi 的决定从阻断降为警示：拿 root 接一个库这件事
#: 要能做成。**但它绝不静默** —— 自检行照旧显示 ✕，接口另出一份 warnings，
#: 卡片上是「有告警」而不是「正常」。要恢复成阻断，配置里打开
#: datasources.strict_account_check。
ADVISORY_CHECKS: frozenset[str] = frozenset({
    "账号为只读",
    "连接数上限已设置",
    "非超级账号且无写权限",     # MySQL
    "非超级用户且不绕过 RLS",    # PostgreSQL
    "授权表集合",               # 白名单与库不一致：该改白名单，不是拒绝这个库
})


def blocking_failures(checks: list[dict[str, Any]]) -> list[str]:
    """没过的**阻断项**名字。空列表 = 可以接入（可能仍有警示）。"""
    return [c["name"] for c in checks if not c["ok"] and c.get("blocking", True)]


def advisory_failures(checks: list[dict[str, Any]]) -> list[str]:
    """没过的警示项名字。接入放行，但界面必须把它们摆出来。"""
    return [c["name"] for c in checks if not c["ok"] and not c.get("blocking", True)]


@dataclass
class ExplainResult:
    est_rows: int | None
    plan: str = ""
    ok: bool = True
    reason: str = ""


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    as_of: str = ""     # 数据时间（§8 准入条件 #7）
    #: 脱敏判定退化过：SQL 解析不出，本次结果按整行脱敏返回。
    #: 必须能传到界面上 —— 一屏星号而不说明为什么，看的人只会以为库里就是这样。
    mask_degraded: bool = False
    #: 本次被脱敏的返回列名。审计要记"哪几列脱了"，否则事后无从判断
    #: 某一次结果到底有没有真的脱敏。
    masked_columns: list[str] = field(default_factory=list)


# ==========================================================================
# 后端
# ==========================================================================

class _Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.con: Any = None
        # 建连耗时，由各后端在**真正建立连接的那一次**写入。
        #
        # 放在这里而不是让调用方掐表：connect() 对已有连接是缓存早退，谁先
        # 调到谁就把这段时间吃掉了。`with Executor(cfg) as ex:` 的 __enter__
        # 已经连过一次，随后 self_check 再掐表只能掐出 0 —— 数据源卡上那个
        # 恒为 0ms 的「延迟」就是这么来的。
        self.connect_ms: float | None = None

    def connect(self) -> Any: ...          # pragma: no cover
    def describe(self, names: list[str]) -> dict[str, list[dict[str, Any]]]: ...  # pragma: no cover
    def explain_rows(self, sql: str) -> tuple[int | None, str]: ...   # pragma: no cover
    def fetch(self, sql: str, cap: int) -> tuple[list[str], list[list[Any]], str]: ...  # pragma: no cover
    def env_checks(self) -> list[tuple[str, bool, str]]: ...          # pragma: no cover

    @contextlib.contextmanager
    def metadata_window(self):
        """元数据窗口：接入向导扫表期间放宽超时预算。

        R-12 的 statement_timeout 是给**用户查询**定的（生产 3 秒）。元数据
        扫描不是用户查询：它一次要问清整个库有哪些表、每张表多少列，代价由
        库的规模决定，与提问快慢无关。两者共用一个预算的结果是——库一大，
        接入向导必然超时，而超时的表现是「扫描失败 500」，看不出是超时。

        默认不做任何事（DuckDB 是本机文件，没有服务端超时可调）。
        PostgreSQL 与 MySQL 各自覆盖它。
        """
        yield

    def quote_ident(self, name: str) -> str:
        """把标识符引起来，供自检的写操作实探拼 SQL 用。

        默认原样返回 —— DuckDB 与 PostgreSQL 这条路径上一直是裸名，
        改成加引号会顺带改掉大小写敏感性，那是另一件事。MySQL 覆盖它：
        反引号在那边是唯一的引法，而表名撞上保留字并不罕见。
        """
        return str(name)

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None
        # 连接没了，上一次的建连耗时也就不再描述任何现存连接
        self.connect_ms = None


class _DuckBackend(_Backend):
    def connect(self):
        if self.con is not None:
            return self.con
        import duckdb

        path = self.cfg.db_path
        if not path.exists():
            raise DataSourceError(
                f"样例库不存在：{path}",
                hint="先运行 `python -m data.seed` 生成本机样例库（约需几秒）。",
            )
        t0 = time.perf_counter()
        try:
            self.con = duckdb.connect(str(path), read_only=True)
        except Exception as e:  # pragma: no cover - 依赖具体环境
            raise DataSourceError(f"无法打开数据库：{e}", hint="确认文件未被其他进程以写模式占用。") from e
        self.connect_ms = _elapsed_ms(t0)
        return self.con

    def describe(self, names: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not names:
            return {}
        holes = ", ".join("?" for _ in names)
        rows = self.connect().execute(f"""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name IN ({holes})
            ORDER BY table_name, ordinal_position
        """, names).fetchall()
        return _group_columns(rows)

    def explain_rows(self, sql: str) -> tuple[int | None, str]:
        rows = self.connect().execute(f"EXPLAIN {sql}").fetchall()
        plan = "\n".join(str(c) for r in rows for c in r if c is not None)
        nums: list[int] = []
        for pat in _EST_PATTERNS:
            nums = [int(m.replace(",", "")) for m in pat.findall(plan)]
            if nums:
                break
        # 取全计划的最大值 —— 关心的是最宽的那一层扫了多少，不是最终返回多少
        return (max(nums) if nums else None), plan

    def fetch(self, sql: str, cap: int):
        con = self.connect()
        timeout_ms = int(self.cfg.raw["guard"]["statement_timeout_ms"])

        # R-12：DuckDB 无 statement_timeout，用看门狗线程中断
        fired = threading.Event()
        done = threading.Event()

        def watchdog() -> None:
            if not done.wait(timeout_ms / 1000):
                fired.set()
                try:
                    con.interrupt()
                except Exception:  # pragma: no cover
                    pass

        threading.Thread(target=watchdog, daemon=True).start()
        try:
            cur = con.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(cap + 1)
        except Exception as e:
            if fired.is_set():
                raise DataSourceError(
                    f"查询超时（超过 {timeout_ms} ms 已中断）",
                    hint="缩小时间范围或增加筛选条件；这是 R-12 语句超时护栏。",
                    retryable=True,
                ) from e
            raise
        finally:
            done.set()
        # 本机文件库，进程时钟即数据源时钟
        return columns, [list(r) for r in rows], _local_now()

    def env_checks(self):
        ms = self.cfg.raw["guard"]["statement_timeout_ms"]
        return [
            ("账号为只读", True, "连接以 read_only=True 打开"),
            ("语句超时已设置", True, f"看门狗 {ms} ms"),
            ("连接数上限已设置", True, "单进程单连接"),
        ]

    def all_tables(self) -> set[str]:
        return {r[0] for r in self.connect().execute("SHOW TABLES").fetchall()}

    def introspect(self) -> list[dict[str, Any]]:
        rows = self.connect().execute("""
            SELECT t.table_name,
                   COALESCE(d.estimated_size, 0) AS est_rows,
                   COUNT(c.column_name)          AS n_cols,
                   BOOL_OR(c.column_name IN ('org_id','organization_id','tenant_id')) AS has_tenant
            FROM information_schema.tables t
            JOIN information_schema.columns c ON c.table_name = t.table_name
            LEFT JOIN duckdb_tables() d ON d.table_name = t.table_name
            WHERE t.table_schema = 'main'
            GROUP BY t.table_name, d.estimated_size
            ORDER BY est_rows DESC
        """).fetchall()
        return [{"name": r[0], "rows": int(r[1] or 0), "cols": int(r[2]),
                 "tenant": bool(r[3])} for r in rows]


#: 一眼能看出"类型选错了"的端口。填错类型时驱动给的报错毫无指向性
#: （PG 驱动打 3306 报的是 SSL 协商失败），这张表把它翻译成人话。
_OTHER_DEFAULT_PORTS = {"3306": "MySQL", "5432": "PostgreSQL"}


def _pg_connect_hint(dsn: str, upstream: str) -> str:
    """连不上 PostgreSQL 时，该去查哪一头。

    dsn 里写的永远是本机端点。经 SSH 隧道连接时那是个转发端口，本机上
    确实没有库在监听 —— 这时候提示"确认 Postgres.app 在运行"会把人支去
    查一个根本不该存在的本地服务，而真正断掉的是隧道。同一台机器上往往
    还真跑着一个本地 Postgres（在 5432），照着查只会更确信方向没错。

    upstream 与本机端点相同时不算隧道：本机直连也可以声明 upstream，
    只是当出处标注用（与 server._dsn_label 同一套判断）。
    """
    parts = parse_kv_dsn(dsn)
    db = parts.get("dbname", "?")
    local = f"{parts.get('host', '?')}:{parts.get('port', '5432')}"
    tunneled = bool(upstream) and upstream.strip().rstrip("/").removesuffix(f"/{db}") != local
    if tunneled:
        return (f"dsn 指向的 {local} 是隧道本地端口，真实库在 {upstream}；"
                f"本机该端口没有服务是正常的。先确认到 {upstream} 的 SSH 隧道已建立，"
                f"再查库名与账号。")
    # 端口是另一种库的默认端口时，先说这一条 —— 十有八九是「数据库类型」
    # 选错了。PG 驱动打到 3306 会报 "invalid response to SSL negotiation"，
    # 那句话没有任何一个字指向真正的原因，照着它查会绕很远。
    #
    # **只提示，不拒绝**：把 PostgreSQL 跑在 3306 完全合法，替用户否掉一个
    # 能连通的配置，比多给一句提示糟得多。
    port = parts.get("port", "5432")
    if port in _OTHER_DEFAULT_PORTS and _OTHER_DEFAULT_PORTS[port] != "PostgreSQL":
        return (f"{port} 是 {_OTHER_DEFAULT_PORTS[port]} 的默认端口，"
                f"而这里选的类型是 PostgreSQL —— 先确认「数据库类型」选对了。"
                f"（若这个库确实是跑在 {port} 上的 PostgreSQL，再查库名与账号。）")
    return ("确认目标机器上的 PostgreSQL 在运行、库名与账号正确、该账号已被授权；"
            "本机调试时确认 Postgres.app 已启动。")


class _PgBackend(_Backend):
    """PostgreSQL —— 护栏做在引擎层，比应用层可靠。"""

    def connect(self):
        if self.con is not None:
            return self.con
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover
            raise DataSourceError(
                "缺少 PostgreSQL 驱动。",
                hint='安装：uv pip install "psycopg[binary]"，或 pip install ".[postgres]"',
            ) from e

        dsn = self.cfg.dsn
        if not dsn:
            raise DataSourceError(
                "未配置 PostgreSQL 连接串（datasource.dsn）。",
                hint="在 config 中填写 dsn，密码用 password_env 指向环境变量。",
            )
        t0 = time.perf_counter()
        try:
            self.con = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        except Exception as e:
            raise DataSourceError(
                f"无法连接 PostgreSQL：{str(e).splitlines()[0]}",
                hint=_pg_connect_hint(dsn, self.cfg.upstream),
            ) from e
        # 计到这里为止 —— 自检里那一项叫「网络可达与认证」，量的就该是握手
        # 加认证。下面几条 SET 是护栏配置，算进去会让这个数字变成另一件事。
        self.connect_ms = _elapsed_ms(t0)

        # 会话级硬护栏。角色级也应配同样的设置，这里是第二道保险。
        ms = int(self.cfg.raw["guard"]["statement_timeout_ms"])
        with self.con.cursor() as cur:
            cur.execute(f"SET statement_timeout = {ms}")
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET idle_in_transaction_session_timeout = 10000")
            # 供行级安全策略读取的租户上下文（§4.4 第二层）
            cur.execute("SELECT set_config('app.org_id', %s, false)",
                        (str(self.cfg.default_org),))
        return self.con

    @contextlib.contextmanager
    def metadata_window(self):
        """扫表期间把 statement_timeout 放宽到元数据预算，出窗口即还原。

        pg_class / pg_attribute 是内存里的系统目录，通常几十毫秒就回来；
        放宽是为了库特别大时不至于卡在 R-12 上，而不是默认就该松。
        """
        ms = self.cfg.metadata_timeout_ms
        back = int(self.cfg.raw["guard"]["statement_timeout_ms"])
        con = self.connect()
        with con.cursor() as cur:
            cur.execute(f"SET statement_timeout = {ms}")
        try:
            yield
        finally:
            # 还原**一定要发生**：漏了就等于给后面每一条用户查询都松了绑。
            try:
                with con.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {back}")
            except Exception:      # pragma: no cover - 连接已断时无所谓还原
                self.close()

    def set_org(self, org_id: int) -> None:
        """每次查询前刷新 RLS 用的租户上下文。"""
        with self.connect().cursor() as cur:
            cur.execute("SELECT set_config('app.org_id', %s, false)", (str(org_id),))

    def explain_rows(self, sql: str) -> tuple[int | None, str]:
        import json

        with self.connect().cursor() as cur:
            cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
            plan = cur.fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        root = plan[0]["Plan"] if isinstance(plan, list) else plan["Plan"]

        best = 0
        stack = [root]
        while stack:
            node = stack.pop()
            best = max(best, int(node.get("Plan Rows", 0) or 0))
            stack.extend(node.get("Plans", []) or [])
        return best, json.dumps(plan, ensure_ascii=False)[:4000]

    def fetch(self, sql: str, cap: int):
        try:
            with self.connect().cursor() as cur:
                cur.execute(sql)
                columns = [d.name for d in (cur.description or [])]
                rows = cur.fetchmany(cap + 1)
                # 数据时间取**库上的**事务时间，不取本机时钟：跨时区、跨主机的
                # 时钟偏移会让"数据截至"标错，而这个标注是给人判断新鲜度用的。
                # 与查询同事务，READ COMMITTED 下即本次读事务的起始时刻。
                cur.execute("SELECT now()")
                as_of = _fmt_ts(cur.fetchone()[0])
        except Exception as e:
            msg = str(e)
            if "statement timeout" in msg or "canceling statement" in msg:
                ms = self.cfg.raw["guard"]["statement_timeout_ms"]
                raise DataSourceError(
                    f"查询超时（超过 {ms} ms，已由 statement_timeout 取消）",
                    hint="缩小时间范围或增加筛选条件；这是 R-12 语句超时护栏。",
                    retryable=True,
                ) from e
            raise
        return columns, [list(r) for r in rows], as_of

    def env_checks(self):
        out: list[tuple[str, bool, str]] = []
        with self.connect().cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            ro = cur.fetchone()[0]
            out.append(("账号为只读", ro == "on", f"default_transaction_read_only = {ro}"))


            cur.execute("SHOW statement_timeout")
            st = cur.fetchone()[0]
            out.append(("语句超时已设置", st not in ("0", "0ms"), f"statement_timeout = {st}"))

            cur.execute("SELECT current_user, rolconnlimit FROM pg_roles WHERE rolname = current_user")
            user, limit = cur.fetchone()
            out.append(("连接数上限已设置", (limit or -1) > 0,
                        f"{user} · CONNECTION LIMIT = {limit}"))

            cur.execute("""SELECT rolsuper, rolbypassrls FROM pg_roles
                           WHERE rolname = current_user""")
            sup, bypass = cur.fetchone()
            out.append(("非超级用户且不绕过 RLS", not sup and not bypass,
                        f"rolsuper={sup} · rolbypassrls={bypass}"))
        return out

    def all_tables(self) -> set[str]:
        with self.connect().cursor() as cur:
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema = 'public'""")
            return {r[0] for r in cur.fetchall()}

    def introspect(self) -> list[dict[str, Any]]:
        with self.connect().cursor() as cur:
            cur.execute("""
                SELECT c.relname,
                       GREATEST(c.reltuples::bigint, 0) AS est_rows,
                       COUNT(a.attname)                 AS n_cols,
                       BOOL_OR(a.attname IN ('org_id','organization_id','tenant_id')) AS has_tenant
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                GROUP BY c.relname, c.reltuples
                ORDER BY est_rows DESC
            """)
            return [{"name": r[0], "rows": int(r[1] or 0), "cols": int(r[2]),
                     "tenant": bool(r[3])} for r in cur.fetchall()]

    def describe(self, names: list[str]) -> dict[str, list[dict[str, Any]]]:
        """字段清单，**连库里的注释一起取**。

        注释是这套系统里最便宜也最准的一份语义：中文注释与中文提问同语种，
        召回命中它比任何中英词典都可靠。不取它，运行时数据源的白名单就永远
        是一堆没有语义的英文标识符 —— 而那正是中文提问召回失灵的根。
        """
        if not names:
            return {}
        with self.connect().cursor() as cur:
            cur.execute("""
                SELECT c.table_name,
                       c.column_name,
                       c.data_type,
                       COALESCE(col_description(k.oid, c.ordinal_position), ''),
                       COALESCE(obj_description(k.oid, 'pg_class'), '')
                FROM information_schema.columns c
                LEFT JOIN pg_class k
                       ON k.relname = c.table_name
                      AND k.relnamespace = 'public'::regnamespace
                WHERE c.table_schema = 'public' AND c.table_name = ANY(%s)
                ORDER BY c.table_name, c.ordinal_position
            """, (names,))
            grouped = _group_columns(cur.fetchall())
        return self._attach_enums(names, grouped)

    def _attach_enums(self, names: list[str], grouped: dict[str, list[dict[str, Any]]],
                      ) -> dict[str, list[dict[str, Any]]]:
        """给低基数文本列补上真实取值，取自 pg_stats。

        为什么非要有这一步：注释里写了取值的库（如这批电商库）靠解析注释就够，
        但**没有注释的库**（如 ragforge 生产库，整库零 COMMENT）模型只能猜取值，
        猜错大小写就是一条语法正确、结果恒为空的 SQL —— 解析失败率因此报 0%，
        真值 4.02%，页面上看不出任何异常。

        pg_stats 是 ANALYZE 留下的统计视图，**读它不扫表**，代价可以忽略；
        视图本身按表权限过滤，只读账号看得到的正是它能查的那些表。
        整段是尽力而为：任何异常都退回"没有取值"，绝不因此让加数据源失败。
        """
        try:
            with self.connect().cursor() as cur:
                cur.execute("""
                    SELECT tablename, attname, n_distinct, most_common_vals::text
                    FROM pg_stats
                    WHERE schemaname = 'public' AND tablename = ANY(%s)
                      AND most_common_vals IS NOT NULL
                      AND n_distinct > 0 AND n_distinct <= %s
                """, (names, _ENUM_MAX_DISTINCT))
                stats = {(r[0], r[1]): _parse_pg_array(r[3]) for r in cur.fetchall()}
        except Exception:
            return grouped
        for table, cols in grouped.items():
            for col in cols:
                if col.get("enum"):
                    continue
                vals = stats.get((table, col["name"]))
                # 只给文本列补：数值/时间列的高频值是数据不是取值集合，
                # 拿去做"枚举归一"毫无意义，还会把提示词撑大。
                if vals and _is_texty(col.get("type", "")):
                    col["enum"] = vals[:_ENUM_MAX_DISTINCT]
        return grouped


#: 认定为"枚举列"的取值上限。再多就不是取值集合，而是数据本身。
_ENUM_MAX_DISTINCT = 32


def _is_texty(dtype: str) -> bool:
    d = str(dtype or "").lower()
    return any(k in d for k in ("char", "text", "enum"))


def _parse_pg_array(literal: str | None) -> list[str]:
    """把 `{A,B,"C D"}` 这种数组字面量拆成元素。

    most_common_vals 是 anyarray，psycopg 取不到具体类型，只能转 text 再拆。
    带引号的元素按引号取，其余按逗号切 —— 取值里出现逗号的场景本就不该被
    当成枚举，拆错了也只是少认一个取值，不会认错。
    """
    t = str(literal or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return []
    body = t[1:-1]
    out, buf, in_q = [], [], False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return [v.strip() for v in out if v.strip()]


def _group_columns(rows: Any) -> dict[str, list[dict[str, Any]]]:
    """(表, 列, 类型) 三元组按表归组。

    这是新增数据源时构造白名单的原料：白名单必须带上字段名与类型，
    否则 R-04（字段真实性）与 R-05（展开 SELECT *）没有判定依据 ——
    它们会退化成放行，而放行是最不该出现的失败方向。
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        # 后端各取各的：DuckDB 只有三元组，PostgreSQL 还带列注释与表注释。
        # 用长度分支而不是要求两边对齐，是因为 DuckDB 根本没有 COMMENT ON
        # 这套元数据可取 —— 硬凑两个空字符串出来，读的人会以为它有而恰好是空。
        table, column, dtype = row[0], row[1], row[2]
        item: dict[str, Any] = {"name": str(column), "type": str(dtype).upper()}
        if len(row) >= 4:
            item["desc"] = str(row[3] or "")
        if len(row) >= 5:
            item["table_desc"] = str(row[4] or "")
        out.setdefault(str(table), []).append(item)
    return out

def _masked(value) -> str:
    """脱敏成「首字符 + 星号 + 末字符」。

    保留首尾而不是整列打星：排查问题时经常需要确认"是不是同一个人"，
    全星会让审计与对账彻底做不了；保留首尾既够用，也不足以还原。
    短值（≤2 字符）整体打星 —— 保留首尾等于把它原样交出去。

    统一转成字符串：手机号在库里可能是数值类型，按数值处理会得到
    一个仍然可读的数字。脱敏必须与列的存储类型无关。
    """
    text = str(value)
    if len(text) <= 2:
        return "*" * len(text)
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"



# ==========================================================================
# MySQL / MariaDB
# ==========================================================================
#
# 连接串解析在上面的 parse_kv_dsn —— 它不是 MySQL 专用的，
# PostgreSQL 那条路径的连接提示也用它，两边解析出的主机必须是同一个。

def _mysql_connect_hint(kv: dict[str, str], upstream: str) -> str:
    """连不上 MySQL 时该去查哪一头。与 _pg_connect_hint 同一套判断。"""
    db = kv.get("dbname", "?")
    port = kv.get("port", "3306")
    if port in _OTHER_DEFAULT_PORTS and _OTHER_DEFAULT_PORTS[port] != "MySQL":
        return (f"{port} 是 {_OTHER_DEFAULT_PORTS[port]} 的默认端口，"
                f"而这里选的类型是 MySQL —— 先确认「数据库类型」选对了。")
    local = f"{kv.get('host', '?')}:{port}"
    tunneled = bool(upstream) and upstream.strip().rstrip("/").removesuffix(f"/{db}") != local
    if tunneled:
        return (f"dsn 指向的 {local} 是隧道本地端口，真实库在 {upstream}；"
                f"本机该端口没有服务是正常的。先确认到 {upstream} 的 SSH 隧道已建立，"
                f"再查库名与账号。")
    return ("确认 MySQL 在运行、库名与账号正确、该账号已被授权，"
            "且该账号允许从这台机器连入（MySQL 的授权带主机段，"
            "'askdb_ro'@'localhost' 连不上远端）。")


#: 超时被取消时服务端给的错误码。
#:   3024 / MySQL 8：max_execution_time 到点
#:   1969 / MariaDB：max_statement_time 到点
#:   2013 / 驱动侧：read_timeout 到点，连接已断（兜底那一层，见 connect）
_MYSQL_TIMEOUT_CODES = frozenset({3024, 1969, 2013})
_MYSQL_TIMEOUT_WORDS = (
    "max_execution_time", "maximum statement execution time",
    "max_statement_time", "query execution was interrupted",
    "lost connection to mysql server during query",
)

#: SHOW GRANTS 里出现即视为有写权限。ALL PRIVILEGES 与 SUPER 单列，
#: 因为它们不是"某一种写"，而是"什么都能做"。
_MYSQL_WRITE_PRIVS = (
    "ALL PRIVILEGES", "SUPER", "INSERT", "UPDATE", "DELETE", "REPLACE",
    "DROP", "CREATE", "ALTER", "TRUNCATE", "INDEX", "GRANT OPTION",
    "LOCK TABLES", "FILE", "RELOAD", "SHUTDOWN", "PROCESS",
)


def _mysql_error_code(e: Exception) -> int:
    """驱动异常里的 MySQL 错误码。取不到返回 0 —— 调用方再看消息文本。"""
    args = getattr(e, "args", ())
    if args and isinstance(args[0], int):
        return int(args[0])
    return 0


def _is_mysql_timeout(e: Exception) -> bool:
    if _mysql_error_code(e) in _MYSQL_TIMEOUT_CODES:
        return True
    msg = str(e).lower()
    return any(w in msg for w in _MYSQL_TIMEOUT_WORDS)


def _mysql_enum_values(column_type: str) -> list[str]:
    """`enum('ON_SALE','OFF_SHELF')` → ['ON_SALE', 'OFF_SHELF']。

    MySQL 的枚举是**类型的一部分**，不像 PostgreSQL 那样要靠 pg_stats 猜 ——
    这是这条路径上唯一一处比 PG 更准的元数据，不取白不取。取值猜错大小写
    （`'failed'` 而库里是 `'FAILED'`）得到的是一条语法正确、结果恒空的 SQL，
    页面上看不出任何异常。
    """
    t = str(column_type or "")
    low = t.lower()
    if not (low.startswith("enum(") or low.startswith("set(")):
        return []
    body = t[t.index("(") + 1: t.rindex(")")]
    # 元素一律带单引号，内部的引号按 SQL 规矩双写
    return [m.group(1).replace("''", "'")
            for m in re.finditer(r"'((?:[^']|'')*)'", body)]


class _MySqlBackend(_Backend):
    """MySQL / MariaDB —— 护栏落点与 PostgreSQL 一一对应，实现方式不同。

    三处差异必须写下来，否则读的人会默认两边等价：

      * **只读是会话属性，不是账号属性。** MySQL 没有 PostgreSQL 那种
        `ALTER ROLE … SET default_transaction_read_only`，只能在每条连接上
        `SET SESSION TRANSACTION READ ONLY`。所以自检里「账号为只读」这一项
        额外看一眼 SHOW GRANTS：会话开关证明这条连接写不了，授权才证明
        这个账号本来就不该写。两个证据分量不同，都要。
      * **语句超时有两个变量名。** MySQL 5.7.8+ 是 max_execution_time（毫秒，
        只管 SELECT），MariaDB 是 max_statement_time（秒）。互不认识，按序试，
        最后**如实报出用上的是哪一个** —— 一个都没设上时自检必须红，
        而不是显示"已设置"。
      * **没有行级安全。** 租户隔离只剩应用层 AST 改写那一层。配置里选
        rls / rls_and_predicate 会在 config._validate 直接被拒（那条校验按
        db_type != postgresql 判，MySQL 自然落进去），不会静默降级成单层。
    """

    #: 实际生效的超时变量名，connect 时探出来；env_checks 与超时报错都用它。
    _timeout_var: str = ""

    def connect(self):
        if self.con is not None:
            return self.con
        try:
            import pymysql
        except ImportError as e:  # pragma: no cover - 依赖具体环境
            raise DataSourceError(
                "缺少 MySQL 驱动。",
                hint='安装：uv pip install "PyMySQL>=1.1"',
            ) from e

        dsn = self.cfg.dsn
        if not dsn:
            raise DataSourceError(
                "未配置 MySQL 连接串（datasource.dsn）。",
                hint="在 config 中填写 dsn，密码用 password_env 指向环境变量。",
            )
        kv = parse_kv_dsn(dsn, keep_password=True)
        if not kv.get("host"):
            raise DataSourceError(
                f"MySQL 连接串里没有 host=：{dsn.split('password=')[0].strip() or '(空)'}",
                hint="连接串与 PostgreSQL 同一种写法："
                     "host=db.internal port=3306 dbname=orders user=askdb_ro；"
                     "口令走环境变量或主密钥加密，不要写进连接串。",
            )

        ms = int(self.cfg.raw["guard"]["statement_timeout_ms"])
        params: dict[str, Any] = {
            "host": kv["host"],
            "port": int(kv.get("port") or 3306),
            "user": kv.get("user") or None,
            "password": kv.get("password") or "",
            "database": kv.get("dbname") or kv.get("database") or None,
            "charset": "utf8mb4",
            "autocommit": True,
            "connect_timeout": 5,
            # 驱动侧兜底：服务端的 max_execution_time 只管 SELECT，
            # 而 MariaDB 上它可能压根没设上。多给 5 秒余量，让服务端的
            # 超时先说话 —— 两者同时到点的话，服务端那条消息才是准的。
            "read_timeout": ms / 1000 + 5,
            "write_timeout": 5,
        }
        params.update(_mysql_ssl_params(kv))

        t0 = time.perf_counter()
        try:
            self.con = pymysql.connect(**params)
        except Exception as e:
            raise DataSourceError(
                f"无法连接 MySQL：{str(e).splitlines()[0]}",
                hint=_mysql_connect_hint(kv, self.cfg.upstream),
            ) from e
        # 与 PostgreSQL 一致：只计到握手加认证为止，下面几条 SET 是护栏配置，
        # 算进去会让自检里那个「网络可达与认证」变成另一件事。
        self.connect_ms = _elapsed_ms(t0)
        self._apply_session_guards(ms)
        return self.con

    def _apply_session_guards(self, ms: int) -> None:
        """会话级硬护栏。只读那一条**失败就炸** —— 连不上不如连不成。"""
        with self.con.cursor() as cur:
            try:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            except Exception as e:
                self.close()
                raise DataSourceError(
                    f"该服务端不支持只读事务，askdb 不接受可写连接：{str(e).splitlines()[0]}",
                    hint="只读事务需要 MySQL 5.6+ 或 MariaDB 10.0+。"
                         "旧版本请改用别的库，或在库前放一层只读副本。",
                ) from e
            self._timeout_var = ""
            for stmt, var in (
                (f"SET SESSION max_execution_time = {ms}", "max_execution_time"),
                (f"SET SESSION max_statement_time = {ms / 1000}", "max_statement_time"),
            ):
                try:
                    cur.execute(stmt)
                except Exception:
                    continue          # 换一个变量名再试，两个都不认由自检报红
                self._timeout_var = var
                break

    def quote_ident(self, name: str) -> str:
        return "`" + str(name).replace("`", "``") + "`"

    def _set_timeout(self, ms: int) -> None:
        """把会话超时改成 ms。用的是建连时探到的那个变量名 —— MariaDB 上叫
        max_statement_time 且单位是秒，两边不能混。"""
        if not self._timeout_var or self.con is None:
            return                     # 两个变量都不认；自检里已经报红了
        val = ms if self._timeout_var == "max_execution_time" else ms / 1000
        with self.con.cursor() as cur:
            cur.execute(f"SET SESSION {self._timeout_var} = {val}")

    @contextlib.contextmanager
    def metadata_window(self):
        """扫表期间放宽两处超时，出窗口即还原。

        两处都要放宽，只改一处等于没改：

          · 服务端的 max_execution_time —— 到点报 3024，"Query execution
            was interrupted"；
          · 驱动侧的 read_timeout —— 到点报 2013，"Lost connection"，
            而且**连接就此报废**，后面每条语句都跟着失败。

        这不是把 R-12 放松了：窗口只罩 information_schema 的那几条查询，
        用户查询走的仍是 guard.statement_timeout_ms。
        """
        ms = self.cfg.metadata_timeout_ms
        back = int(self.cfg.raw["guard"]["statement_timeout_ms"])
        con = self.connect()
        prev_read = getattr(con, "_read_timeout", None)
        self._set_timeout(ms)
        # pymysql 每次读包前按这个值 settimeout，改它当场生效。私有属性，
        # 所以 getattr/setattr 都留了退路：拿不到就只放宽服务端那一侧。
        if prev_read is not None:
            con._read_timeout = ms / 1000 + 5
        try:
            yield
        finally:
            try:
                if prev_read is not None:
                    con._read_timeout = prev_read
                self._set_timeout(back)
            except Exception:      # pragma: no cover - 连接已断时无所谓还原
                self.close()

    # ---------- 元数据 ----------

    def _meta_query(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """跑一条 information_schema 查询，**把驱动的异常翻译成 DataSourceError**。

        不翻译的后果这次真出过：扫一个 96 张表的 5.7 库，超时以
        pymysql.OperationalError 原样冒到 FastAPI，界面上只剩一句
        「扫描失败 500」—— 既看不出是超时，也不知道该调什么。
        """
        try:
            with self.connect().cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        except DataSourceError:
            raise
        except Exception as e:
            budget = self.cfg.metadata_timeout_ms
            if _is_mysql_timeout(e):
                # 超时会把这条连接打废（2013 之后再发什么都失败），扔掉重来
                self.close()
                raise DataSourceError(
                    f"读取元数据超时（超过 {budget} ms）：这个库的 "
                    f"information_schema 响应太慢。",
                    hint="库里表很多、或服务端正忙。可以调大配置里的 "
                         "guard.metadata_timeout_ms 后重试；这条超时与查询超时"
                         "（R-12）是两个值，放宽它不会放宽用户查询。",
                    retryable=True,
                ) from e
            raise DataSourceError(
                f"读取元数据失败：{str(e).splitlines()[0]}",
                hint="确认这个账号能读 information_schema，且连接仍然可用。",
            ) from e

    def describe(self, names: list[str]) -> dict[str, list[dict[str, Any]]]:
        """字段清单，**连注释与枚举取值一起取**。

        MySQL 的列注释与 PostgreSQL 的 col_description 是同一份东西：中文注释
        与中文提问同语种，Schema 召回命中它比任何中英词典都可靠。
        枚举比 PG 那条路还准 —— 取值写在类型里，不必靠统计视图去猜。

        **列和表注释分两条查，不 JOIN。** information_schema 在 5.7 上不是真表，
        把 COLUMNS 与 TABLES 连起来会退化成对每一行去开一次表定义；实测同一个
        96 张表的库，分开查各几百毫秒，连起来查超过 8 秒还没回来。
        """
        if not names:
            return {}
        holes = ", ".join(["%s"] * len(names))
        rows = self._meta_query(f"""
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                   COALESCE(COLUMN_COMMENT, ''), COLUMN_TYPE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({holes})
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """, tuple(names))
        table_desc = {r[0]: r[1] for r in self._meta_query(f"""
            SELECT TABLE_NAME, COALESCE(TABLE_COMMENT, '')
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({holes})
        """, tuple(names))}
        # InnoDB 的 TABLE_COMMENT 里会带上 "; InnoDB free: …" 之类的运维文本，
        # 那不是表说明，进召回只会稀释语义
        table_desc = {t: re.sub(r"\s*;?\s*InnoDB free:.*$", "", d or "").strip()
                      for t, d in table_desc.items()}
        grouped = _group_columns([(r[0], r[1], r[2], r[3], table_desc.get(r[0], ""))
                                  for r in rows])
        enums = {(r[0], r[1]): _mysql_enum_values(r[4]) for r in rows}
        for table, cols in grouped.items():
            for col in cols:
                if vals := enums.get((table, col["name"])):
                    col["enum"] = vals[:_ENUM_MAX_DISTINCT]
        return grouped

    def all_tables(self) -> set[str]:
        return {r[0] for r in self._meta_query(
            """SELECT TABLE_NAME FROM information_schema.TABLES
               WHERE TABLE_SCHEMA = DATABASE()""")}

    def introspect(self) -> list[dict[str, Any]]:
        """库里全部**基表**。行数是 InnoDB 的估算值，与 PG 的 reltuples 同性质
        —— 用来给接入向导排序，不作为答案。

        与 describe 同一个理由：TABLES 与 COLUMNS 分两条查，在应用侧合。
        原来那条 JOIN + GROUP BY 是这次「扫描失败 500」的直接原因。
        """
        tables = self._meta_query("""
            SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0)
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
        """)
        stats = {r[0]: (int(r[1] or 0), bool(r[2])) for r in self._meta_query("""
            SELECT TABLE_NAME, COUNT(*),
                   MAX(COLUMN_NAME IN ('org_id','organization_id','tenant_id'))
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            GROUP BY TABLE_NAME
        """)}
        out = [{"name": name, "rows": int(rows or 0),
                "cols": stats.get(name, (0, False))[0],
                "tenant": stats.get(name, (0, False))[1]}
               for name, rows in tables]
        out.sort(key=lambda t: t["rows"], reverse=True)
        return out

    # ---------- R-11 干跑 ----------

    def explain_rows(self, sql: str) -> tuple[int | None, str]:
        """EXPLAIN 的行数估算。

        取全计划的最大值，与 DuckDB / PostgreSQL 两条路径同一个口径：关心的是
        **最宽的那一层扫了多少**，不是最终返回多少 —— R-11 拦的是扫描量。

        FORMAT=JSON 优先（MySQL 5.6+ / MariaDB 10.1+），认不出就退回经典
        EXPLAIN 的 rows 列。退回时计划文本照样带上，排查时看得见。
        """
        import json

        with self.connect().cursor() as cur:
            try:
                cur.execute(f"EXPLAIN FORMAT=JSON {sql}")
                raw = cur.fetchone()[0]
            except Exception:
                return self._explain_rows_classic(cur, sql)
        plan = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        best: int | None = None
        stack: list[Any] = [plan]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, val in node.items():
                    # MySQL 8 是 rows_examined_per_scan / rows_produced_per_join，
                    # MariaDB 的 JSON 里就叫 rows。三个都认。
                    if key in ("rows_examined_per_scan", "rows_produced_per_join",
                               "rows") and isinstance(val, (int, float)):
                        best = max(best or 0, int(val))
                    else:
                        stack.append(val)
            elif isinstance(node, list):
                stack.extend(node)
        return best, json.dumps(plan, ensure_ascii=False)[:4000]

    def _explain_rows_classic(self, cur: Any, sql: str) -> tuple[int | None, str]:
        cur.execute(f"EXPLAIN {sql}")
        cols = [d[0].lower() for d in (cur.description or [])]
        rows = cur.fetchall()
        plan = "\n".join(" | ".join("" if v is None else str(v) for v in r) for r in rows)
        if "rows" not in cols:
            return None, plan[:4000]
        i = cols.index("rows")
        nums = [int(r[i]) for r in rows if isinstance(r[i], (int, float))]
        return (max(nums) if nums else None), plan[:4000]

    # ---------- 执行 ----------

    def fetch(self, sql: str, cap: int):
        try:
            with self.connect().cursor() as cur:
                cur.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                rows = cur.fetchmany(cap + 1)
                as_of = self._db_now(cur)
        except Exception as e:
            if _is_mysql_timeout(e):
                ms = self.cfg.raw["guard"]["statement_timeout_ms"]
                by = self._timeout_var or "驱动读超时"
                # 连接可能已经被驱动侧的 read_timeout 打断，留着它只会让下一条
                # 查询报一个看不懂的错。丢掉，下次 connect() 重建。
                self.close()
                raise DataSourceError(
                    f"查询超时（超过 {ms} ms，已由 {by} 中断）",
                    hint="缩小时间范围或增加筛选条件；这是 R-12 语句超时护栏。",
                    retryable=True,
                ) from e
            raise
        return columns, [list(r) for r in rows], as_of

    @staticmethod
    def _db_now(cur: Any) -> str:
        """数据时间取**库上的**时钟，并带上库的时区偏移。

        与 PostgreSQL 那条路径同一个理由：跨时区、跨主机的时钟偏移会让
        "数据截至"标错，而这个标注是给人判断新鲜度用的。MySQL 的 NOW()
        不带时区，所以另取一次与 UTC 的差值补上 —— 少了它，界面上那个时间
        看着像本机时间，实际是库所在时区的时间，差几个小时无从发现。
        """
        from datetime import datetime, timedelta, timezone

        cur.execute("SELECT NOW(), TIMEDIFF(NOW(), UTC_TIMESTAMP())")
        now, offset = cur.fetchone()
        if not isinstance(now, datetime):                  # pragma: no cover - 驱动兜底
            return str(now)
        if isinstance(offset, timedelta):
            now = now.replace(tzinfo=timezone(offset))
        return _fmt_ts(now)

    # ---------- 自检 ----------

    def env_checks(self):
        out: list[tuple[str, bool, str]] = []
        with self.connect().cursor() as cur:
            grants = self._grants(cur)
            writable = _mysql_write_grants(grants)

            ro_var, ro_on = self._read_only_var(cur)
            # 两个证据：会话开关证明这条连接写不了，授权证明这个账号本来就不该写。
            # 会话开关读不出来（MariaDB 老版本没有这个变量）时，退到授权判断 ——
            # 退不到就报红，而不是显示"已只读"。
            if ro_var:
                detail = f"{ro_var} = {'ON' if ro_on else 'OFF'}"
                if ro_on and writable:
                    detail += f"；但账号仍有写权限（{'、'.join(writable)}）"
                out.append(("账号为只读", ro_on and not writable, detail))
            elif grants:
                out.append(("账号为只读", not writable,
                            "服务端不报告只读会话变量，按授权判定："
                            + ("仅读权限" if not writable else f"含写权限 {'、'.join(writable)}")))
            else:
                out.append(("账号为只读", False,
                            "既读不到只读会话变量，也读不到 SHOW GRANTS，无法确认此连接不可写"))

            var, val = self._timeout_setting(cur)
            out.append(("语句超时已设置", bool(var) and val > 0,
                        f"{var} = {val}" if var else
                        "服务端不认 max_execution_time / max_statement_time，"
                        "语句超时无处落地（仅剩驱动侧读超时兜底）"))

            limit = self._user_conn_limit(cur)
            who = self._current_user(cur)
            out.append(("连接数上限已设置", limit > 0,
                        f"{who} · max_user_connections = {limit}"
                        + ("" if limit > 0 else
                           "；请执行 ALTER USER … WITH MAX_USER_CONNECTIONS 5")))

            out.append(("非超级账号且无写权限", bool(grants) and not writable,
                        "SHOW GRANTS：" + (
                            "仅 SELECT / USAGE" if grants and not writable else
                            f"含 {'、'.join(writable)}" if writable else
                            "读不到授权（账号可能通过角色授权）——"
                            "请为 askdb 单独建一个只授 SELECT 的账号")))
        return out

    @staticmethod
    def _grants(cur: Any) -> list[str]:
        try:
            cur.execute("SHOW GRANTS FOR CURRENT_USER()")
            return [str(r[0]) for r in cur.fetchall()]
        except Exception:                                  # pragma: no cover - 权限兜底
            return []

    @staticmethod
    def _current_user(cur: Any) -> str:
        try:
            cur.execute("SELECT CURRENT_USER()")
            return str(cur.fetchone()[0])
        except Exception:                                  # pragma: no cover
            return "?"

    @staticmethod
    def _read_only_var(cur: Any) -> tuple[str, bool]:
        """只读会话变量。名字换过两次，按新到旧试。取不到返回空名。"""
        for var in ("transaction_read_only", "tx_read_only"):
            try:
                cur.execute(f"SELECT @@session.{var}")
                return var, bool(int(cur.fetchone()[0]))
            except Exception:
                continue
        return "", False

    @staticmethod
    def _timeout_setting(cur: Any) -> tuple[str, float]:
        """实际生效的超时。0 = 没设上（MySQL 里 0 就是"不限"）。"""
        for var, scale in (("max_execution_time", 1), ("max_statement_time", 1000)):
            try:
                cur.execute(f"SELECT @@session.{var}")
                return var, float(cur.fetchone()[0]) * scale
            except Exception:
                continue
        return "", 0.0

    @staticmethod
    def _user_conn_limit(cur: Any) -> int:
        """账号的并发连接上限。

        MySQL 的 session 值就是这个账号的 MAX_USER_CONNECTIONS（账号没设时
        取全局值），所以直接读 session 即可 —— 只读账号读不到 mysql.user 表。
        """
        try:
            cur.execute("SELECT @@session.max_user_connections")
            return int(cur.fetchone()[0] or 0)
        except Exception:                                  # pragma: no cover
            return 0


def _mysql_write_grants(grants: list[str]) -> list[str]:
    """SHOW GRANTS 里出现的写权限。空列表 = 这个账号只读。

    只看 `GRANT … ON` 之间那一段（权限清单），不看后面的对象与账号名 ——
    库名里带 "update" 的话，整串扫描会把一个只读账号判成可写。
    """
    hit: list[str] = []
    for line in grants:
        text = line.upper()
        if not text.startswith("GRANT "):
            continue
        head = text[len("GRANT "):].split(" ON ")[0]
        for priv in _MYSQL_WRITE_PRIVS:
            if re.search(rf"(?:^|,\s*){re.escape(priv)}(?:\s*[,(]|$)", head) \
                    and priv not in hit:
                hit.append(priv)
    return hit


def _mysql_ssl_params(kv: dict[str, str]) -> dict[str, Any]:
    """TLS 参数。写法沿用 PostgreSQL 的 sslmode / sslrootcert 两个键 ——
    连接串在两种库上是同一套语法，界面与文档就只有一份。

    require 只加密不验证；verify-ca / verify-full 才验证书，此时必须给
    sslrootcert，缺了直接报，不静默降级成不验证。
    """
    mode = (kv.get("sslmode") or "").strip().lower()
    if not mode or mode in ("disable", "disabled", "allow", "prefer"):
        return {}
    ca = (kv.get("sslrootcert") or "").strip()
    if mode in ("verify-ca", "verify-full", "verify_identity"):
        if not ca:
            raise DataSourceError(
                f"sslmode={mode} 要求验证服务端证书，但连接串里没有 sslrootcert=。",
                hint="给出 CA 证书路径，或改用 sslmode=require（只加密、不验证）。",
            )
        return {"ssl": {"ca": ca, "check_hostname": mode == "verify-full"}}
    # require：加密但不验证。写清楚是因为它**挡不住中间人**，
    # 只是让明文口令与结果不再裸奔在网络上。
    return {"ssl": {"check_hostname": False}}


# ==========================================================================
# 对外
# ==========================================================================

class Executor:
    """只读查询执行器。用完记得 close()，或用 with 语句。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        t = cfg.db_type
        if t == "duckdb":
            self.backend: _Backend = _DuckBackend(cfg)
        elif t == "postgresql":
            self.backend = _PgBackend(cfg)
        elif t == "mysql":
            self.backend = _MySqlBackend(cfg)
        else:
            raise DataSourceError(
                f"暂不支持的数据源类型：{t}",
                hint="当前支持 duckdb、postgresql 与 mysql。",
            )

    # ---------- 生命周期 ----------

    def connect(self):
        return self.backend.connect()

    @property
    def connect_ms(self) -> float | None:
        """建立当前这条连接真正花了多久。还没连过时为 None。"""
        return self.backend.connect_ms

    def close(self) -> None:
        self.backend.close()

    def introspect(self) -> list[dict[str, Any]]:
        """列出数据源里**全部**表，不限于白名单 —— 供接入向导选表。

        走元数据窗口：这条路径的代价由库的规模决定，不该受 R-12 的查询超时
        约束（见 _Backend.metadata_window）。
        """
        with self.backend.metadata_window():
            return self.backend.introspect()

    def describe(self, names: list[str]) -> dict[str, list[dict[str, Any]]]:
        """取指定表的字段名与类型，用于构造白名单。同样走元数据窗口。

        一张表都没点时**连都不连** —— 元数据窗口本身要先建连才能改会话超时，
        而"没有要问的东西"不该换来一条出站连接。
        """
        if not names:
            return {}
        with self.backend.metadata_window():
            return self.backend.describe(names)

    def set_org(self, org_id: int) -> None:
        """把租户上下文同步给引擎（PostgreSQL 的 RLS 依赖它）。"""
        fn = getattr(self.backend, "set_org", None)
        if fn:
            fn(org_id)

    def __enter__(self) -> "Executor":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 自检（对应原型「① 接入数据」第 1 步）----------

    def self_check(self) -> list[dict[str, Any]]:
        """连接自检。任一项不过，调用方应拒绝进入可用状态。

        最后一项是**写操作实探**：前面几项检查的是"配置声称什么"，
        这一项检查的是"实际拦不拦"。
        """
        checks: list[dict[str, Any]] = []

        strict = self.cfg.strict_account_check

        def add(name: str, ok: bool, detail: str, **extra: Any) -> None:
            # blocking 跟着**每一项**走，不是全局开关：调用方据此决定"能不能存"，
            # 而界面照旧按 ok 显示 ✓/✕ —— 两件事分开，才不会出现
            # "允许接入" 被渲染成 "检查通过"。
            checks.append({"name": name, "ok": ok, "detail": detail,
                           "blocking": strict or name not in ADVISORY_CHECKS,
                           **extra})

        t0 = time.perf_counter()
        try:
            self.connect()
            # 连接耗时同时给结构化字段：界面要在数据源卡上单独显示「延迟」，
            # 从 detail 字符串里正则抠数字迟早会随文案改动而悄悄失效。
            #
            # 取后端记下的那个数，不在这里掐表：调用方几乎都是
            # `with Executor(cfg) as ex:`，__enter__ 里已经建好连接，上面这次
            # connect() 只是一次缓存命中，掐出来恒为 0。
            ms = self.connect_ms
            if ms is None:            # 后端没记（自定义后端），退回本地掐表
                ms = _elapsed_ms(t0)
            add("网络可达与认证", True, f"{self.cfg.db_type} · {ms} ms", ms=ms)
        except DataSourceError as e:
            add("网络可达与认证", False, f"{e}｜{e.hint}")
            return checks

        for name, ok, detail in self.backend.env_checks():
            add(name, ok, detail)

        try:
            with self.backend.metadata_window():
                actual = self.backend.all_tables()
        except Exception as e:  # pragma: no cover
            add("授权表集合", False, str(e))
            return checks
        allow = set(self.cfg.tables)
        missing = allow - actual
        if missing:
            add("授权表集合", False, f"白名单中的表在库里不可见：{', '.join(sorted(missing))}")
        else:
            add("授权表集合", True, f"白名单 {len(allow)} 张 · 可见 {len(actual)} 张")

        # 写操作实探 —— 必须被拒绝。
        # 白名单为空时退到任意可见表：新接入的数据源还没勾选开放表，
        # 而"这个连接到底拦不拦写"恰恰是那一刻最该问的问题。
        # 原来直接判失败，等于在最需要它的时候把这项检查关掉了。
        probe = next(iter(sorted(allow)), None) or next(iter(sorted(actual)), None)
        if probe is None:
            add("写操作实探", False, "库里没有可见的表，无法探测")
        else:
            via = "" if probe in allow else f"（白名单为空，用可见表 {probe} 探测）"
            try:
                self.backend.fetch(
                    f"DELETE FROM {self.backend.quote_ident(probe)} WHERE 1=0", 1)
                add("写操作实探", False, f"写操作未被拒绝，连接并非只读{via}")
            except Exception:
                add("写操作实探", True, f"写操作已被引擎拒绝 ✓ 符合预期{via}")
        return checks

    # ---------- R-11 干跑 ----------

    def explain(self, sql: str) -> ExplainResult:
        try:
            est, plan = self.backend.explain_rows(sql)
        except DataSourceError:
            raise
        except Exception as e:
            return ExplainResult(est_rows=None, ok=False,
                                 reason=f"执行计划生成失败：{str(e).splitlines()[0]}")

        cap = int(self.cfg.raw["guard"]["max_scan_rows"])
        if est is not None and est > cap:
            return ExplainResult(
                est_rows=est, plan=plan, ok=False,
                reason=f"预估扫描 {est:,} 行，超过阈值 {cap:,}",
            )
        return ExplainResult(est_rows=est, plan=plan)

    # ---------- 执行 ----------

    def run(self, sql: str, limit_capped: bool | None = None) -> QueryResult:
        """执行并按行上限截断。

        `limit_capped` = 这条 SQL 的外层 LIMIT 是不是**护栏强加的上限**（R-09 注入
        或下调），而不是用户/口径自己写的更小上限。只有前者才需要探测"是否还有更多"
        并据此标 truncated；后者是用户主动要少拿，多探一行反而会超出其预期。
        调用方（graph / server）拿得到 guard 的 rules_fired，应显式传 `"R-09" in fired`。
        传 None 时（直连 SQL、单测等无护栏上下文）退回看 SQL 自身：外层 LIMIT 缺失
        或 > cap 才认为 cap 会绑定。
        """
        cap = self.cfg.max_rows
        if limit_capped is None:
            limit_capped = self._outer_limit_binds(sql, cap)
        # R-13 纵深防御：R-09 把外层 LIMIT 注入成 cap 后，DB 恰好只吐 cap 行，
        # backend.fetch 里 fetchmany(cap+1) 想多取的那一行被 SQL 的 LIMIT 掐死，
        # len(rows) > cap 永远不成立，truncated 就永远是 False —— 命中上限的结果被当
        # 成"全量"。cap 绑定时把执行用的外层 LIMIT 抬到 cap+1 探一行：真有第 cap+1 行
        # 才判 truncated，随后砍回 cap。对外展示的 sql_final 仍是 R-09 那条 LIMIT cap
        # （不改对外契约），差的这一行只用于判断有没有更多。
        probe_sql = self._probe_limit(sql, cap) if limit_capped else sql
        t0 = time.perf_counter()
        columns, rows, as_of = self.backend.fetch(probe_sql, cap)
        elapsed = int((time.perf_counter() - t0) * 1000)

        truncated = len(rows) > cap
        if truncated:
            rows = rows[:cap]
        names = [str(c) for c in columns]
        masked, hit, degraded = self._mask(names, rows, sql)
        return QueryResult(
            columns=names,
            rows=masked,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed,
            as_of=as_of,
            mask_degraded=degraded,
            masked_columns=[names[i] for i in hit],
        )

    def _outer_limit_binds(self, sql: str, cap: int) -> bool:
        """无护栏信号时的兜底判断：外层 LIMIT 缺失或 > cap → cap 会绑定（要探测）。

        用户自己写了 <= cap 的 LIMIT（含恰好等于 cap）算用户主动限量，不探。
        解析失败从严当作 cap 绑定 —— 宁可多探一行，也不要漏标截断。
        """
        try:
            import sqlglot
            root = sqlglot.parse_one(sql, dialect=self.cfg.dialect)
            lim = root.args.get("limit")
            if lim is None:
                return True
            return int(lim.expression.name) > cap
        except Exception:                              # pragma: no cover - 解析器兜底
            return True

    def _probe_limit(self, sql: str, cap: int) -> str:
        """只有当外层 LIMIT 恰好被 cap 卡住时，才把它抬到 cap+1 探一行。

        用户/口径自己写了比 cap 更小的 LIMIT（如 LIMIT 5）时**保持不动** ——
        那是用户主动要少拿，不是被上限截断，多探一行会返回超出用户预期的行数，
        truncated 也不该为真。只有 LIMIT 缺失或 ≥ cap（即 R-09 注入的那个上限在
        真正兜底）时，才需要探测第 cap+1 行来判断是否还有更多。
        解析失败原样返回 —— 探不到截断也不能让查询本身跑不起来。
        """
        try:
            import sqlglot
            root = sqlglot.parse_one(sql, dialect=self.cfg.dialect)
            lim = root.args.get("limit")
            if lim is not None:
                try:
                    n = int(lim.expression.name)
                    if n < cap:                        # 用户自己的更小上限，保持
                        return sql
                except (AttributeError, ValueError):
                    pass                               # 不可静态求值，按 cap 绑定处理
            return root.limit(cap + 1).sql(dialect=self.cfg.dialect)
        except Exception:                              # pragma: no cover - 解析器兜底
            return sql

    def _mask(self, columns: list[str], rows: list[list], sql: str = "",
              ) -> tuple[list[list], list[int], bool]:
        """脱敏个人信息（P03）。

        **落点在返回值上而不是在 SQL 里**：改 SQL 会悄悄改变查询语义 ——
        把 phone 换成 substr(...) 会让 COUNT(DISTINCT phone) 这类口径变样，
        那是比看到原值更难发现的错误。

        **但"哪几列要脱敏"必须在 AST 上判，不能按返回列名判。**
        这里原来写的是"拿到的是最终真正返回的列名，没有歧义"—— 那句话错了：
        模型写 `SELECT phone AS 手机号`，返回列名就是"手机号"，与敏感列名对不上，
        整层脱敏静默失效。2026-09-06 的实测里匿名访客据此拿到了明文手机号。
        改为由 guard 解析投影到底出自哪一列，别名改不动它。

        解析不出时（未知语法、来源不明的作用域）**从严：抛 MaskUnresolved，
        整条查询拒答**。

        这里原来的做法是"把所有值都当敏感处理"，理由是"宁可整屏星号让人来问"。
        2026-09-10 的生产跑测证明那句话在实际使用中不成立：问「这两天数据正常吗」
        拿回来的是

            统计日期      模型调用总次数   总成本
            2*******0     8*7             1**5

        —— 全是聚合指标，一列个人信息都没有，而结果已经完全不可读。没有人会
        "来问"，只会把 `8*7` 当成数据的样子接受下来，或者以为库里存的就是这样。
        从严的方向是对的，退化的落点错了：解析不出投影来源时，我们**不知道**
        这些列是什么，那就不该把不知道的东西涂成星号递出去，而该说"这条查询
        我保证不了，换个写法"。拒答是更严的那一侧，也是唯一诚实的那一侧。

        代价仍要说清楚：模型与护栏看得到真实列名，脱敏只作用于返回值。
        它防的是"人看到了不该看的内容"，防不住"按敏感列做筛选"
        （WHERE phone = '138...' 仍能试探）。后者要靠表白名单收窄，
        不是靠这一层 —— 两件事别混。
        """
        # 2026-09-06 起**没有任何角色能关掉脱敏**：原来这里有一个
        # `if self.cfg.unmask: return rows` 的出口，供 DEV / DATA_OWNER 看原值。
        # 产品决定所有角色可见面一致后，那个位失去了承载它的角色差别，
        # 与其留一个恒为假的分支，不如让脱敏成为无条件的。
        from . import guard

        sensitive = {c.lower()
                     for t in self.cfg.tables.values() for c in t.sensitive_columns}
        if not sensitive:
            return rows, [], False

        # 列名匹配保留下来，但只作为**补充**：模型把某列 `AS phone` 时，
        # 它出自哪儿不重要，叫这个名字就该按这个名字对待。
        by_name = {i for i, name in enumerate(columns) if name.lower() in sensitive}

        by_ast: set[int] | None = None
        degraded = False
        if sql:
            try:
                by_ast = guard.sensitive_output_columns(sql, self.cfg, self.cfg.dialect)
            except Exception:                          # pragma: no cover - 解析器兜底
                by_ast = None
        if by_ast is None:
            # 解析不出投影来源：这条结果我们保证不了，直接拒答。见上方 docstring。
            # 只在真有 SQL 上下文时成立 —— 无 SQL（单测、内部调用）仍退回列名匹配。
            if sql:
                raise MaskUnresolved(
                    "无法解析这条 SQL 的投影来源，判定不出哪些列含个人信息，"
                    "因此不返回结果。",
                    hint="把子查询/CTE 拆开或显式写出列的来源表，再试一次；"
                         "结构复杂的统计建议改用预聚合汇总表。",
                )
            hit = sorted(by_name)
        else:
            hit = sorted(by_ast | by_name)
        if not hit:
            return rows, [], degraded
        out = []
        for row in rows:
            r = list(row)
            for i in hit:
                if r[i] is not None:
                    r[i] = _masked(r[i])
            out.append(r)
        return out, hit, degraded
