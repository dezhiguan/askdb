"""只读执行 —— 护栏的最后一层，也是唯一真正碰数据的一层。

设计要点（技术设计说明书 §3.1、§4.1）：
  * 护栏优先做在**引擎层**而非应用层。应用层校验可能被绕过，引擎权限不会。
    DuckDB 以 read_only 打开；PostgreSQL 走独立只读角色 + 会话级只读事务。
  * R-11 扫描行数阈值：执行前先 EXPLAIN 估算，超阈值直接打回。
  * R-12 语句超时：PostgreSQL 用原生 statement_timeout；
    DuckDB 没有该设置，用看门狗线程调 interrupt() 实现。
  * R-13 结果行上限：即便 R-09 已注入 LIMIT，取数时仍再截断一次（纵深防御）。

两种后端的差异全部收在本模块，上层链路对数据源无感。
"""

from __future__ import annotations

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
    """数据源不可用 —— 带可执行的修复建议，直接透传给用户。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


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


# ==========================================================================
# 后端
# ==========================================================================

class _Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.con: Any = None

    def connect(self) -> Any: ...          # pragma: no cover
    def explain_rows(self, sql: str) -> tuple[int | None, str]: ...   # pragma: no cover
    def fetch(self, sql: str, cap: int) -> tuple[list[str], list[list[Any]]]: ...  # pragma: no cover
    def env_checks(self) -> list[tuple[str, bool, str]]: ...          # pragma: no cover

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None


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
        try:
            self.con = duckdb.connect(str(path), read_only=True)
        except Exception as e:  # pragma: no cover - 依赖具体环境
            raise DataSourceError(f"无法打开数据库：{e}", hint="确认文件未被其他进程以写模式占用。") from e
        return self.con

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
                ) from e
            raise
        finally:
            done.set()
        return columns, [list(r) for r in rows]

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
        try:
            self.con = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
        except Exception as e:
            raise DataSourceError(
                f"无法连接 PostgreSQL：{str(e).splitlines()[0]}",
                hint="确认 Postgres.app 在运行、库名与账号正确、该账号已被授权。",
            ) from e

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
        except Exception as e:
            msg = str(e)
            if "statement timeout" in msg or "canceling statement" in msg:
                ms = self.cfg.raw["guard"]["statement_timeout_ms"]
                raise DataSourceError(
                    f"查询超时（超过 {ms} ms，已由 statement_timeout 取消）",
                    hint="缩小时间范围或增加筛选条件；这是 R-12 语句超时护栏。",
                ) from e
            raise
        return columns, [list(r) for r in rows]

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
        else:
            raise DataSourceError(
                f"暂不支持的数据源类型：{t}",
                hint="当前支持 duckdb 与 postgresql。",
            )

    # ---------- 生命周期 ----------

    def connect(self):
        return self.backend.connect()

    def close(self) -> None:
        self.backend.close()

    def introspect(self) -> list[dict[str, Any]]:
        """列出数据源里**全部**表，不限于白名单 —— 供接入向导选表。"""
        return self.backend.introspect()

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

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        t0 = time.perf_counter()
        try:
            self.connect()
            add("网络可达与认证", True,
                f"{self.cfg.db_type} · {int((time.perf_counter() - t0) * 1000)} ms")
        except DataSourceError as e:
            add("网络可达与认证", False, f"{e}｜{e.hint}")
            return checks

        for name, ok, detail in self.backend.env_checks():
            add(name, ok, detail)

        try:
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

        # 写操作实探 —— 必须被拒绝
        probe = next(iter(sorted(allow)), None)
        if probe is None:
            add("写操作实探", False, "白名单为空，无法探测")
        else:
            try:
                self.backend.fetch(f"DELETE FROM {probe} WHERE 1=0", 1)
                add("写操作实探", False, "写操作未被拒绝，连接并非只读")
            except Exception:
                add("写操作实探", True, "写操作已被引擎拒绝 ✓ 符合预期")
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

    def run(self, sql: str) -> QueryResult:
        cap = self.cfg.max_rows
        t0 = time.perf_counter()
        columns, rows = self.backend.fetch(sql, cap)
        elapsed = int((time.perf_counter() - t0) * 1000)

        truncated = len(rows) > cap
        if truncated:
            rows = rows[:cap]
        return QueryResult(
            columns=[str(c) for c in columns],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed,
        )
