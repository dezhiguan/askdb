"""只读执行 —— 护栏的最后一层，也是唯一真正碰数据的一层。

设计要点（技术设计说明书 §3.1、§4.1）：
  * 护栏优先做在**引擎层**而非应用层。应用层校验可能被绕过，引擎权限不会。
    DuckDB 以 read_only 打开，写操作在引擎层即被拒绝；PostgreSQL 走独立只读角色。
  * R-11 扫描行数阈值：执行前先 EXPLAIN 估算，超阈值直接打回，不让慢查询打到库上。
  * R-12 语句超时：DuckDB 没有 statement_timeout，用看门狗线程调 interrupt() 实现。
  * R-13 结果行上限：即便 R-09 已注入 LIMIT，取数时仍再截断一次（纵深防御）。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .config import Config

# DuckDB EXPLAIN 输出中的基数估计，形如 "EC: 12345"
_EC = re.compile(r"EC:\s*(\d+)")


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


class Executor:
    """只读查询执行器。用完记得 close()，或用 with 语句。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._con: duckdb.DuckDBPyConnection | None = None

    # ---------- 生命周期 ----------

    def connect(self) -> duckdb.DuckDBPyConnection:
        if self._con is not None:
            return self._con
        if self.cfg.db_type != "duckdb":
            raise DataSourceError(
                f"暂不支持的数据源类型：{self.cfg.db_type}",
                hint="P0 仅支持 DuckDB；PostgreSQL 支持在 P1 阶段提供。",
            )
        path: Path = self.cfg.db_path
        if not path.exists():
            raise DataSourceError(
                f"样例库不存在：{path}",
                hint="先运行 `python -m data.seed` 生成本机样例库（约需十几秒）。",
            )
        try:
            self._con = duckdb.connect(str(path), read_only=True)
        except Exception as e:  # pragma: no cover - 依赖具体环境
            raise DataSourceError(f"无法打开数据库：{e}", hint="确认文件未被其他进程以写模式占用。") from e
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "Executor":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 自检（对应原型「① 接入数据」第 1 步）----------

    def self_check(self) -> list[dict[str, Any]]:
        """六项连接自检。任一项不过，调用方应拒绝进入可用状态。

        第 6 项是**写操作实探**：前五项检查的是"配置声称什么"，
        第六项检查的是"实际拦不拦"。
        """
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        t0 = time.perf_counter()
        try:
            con = self.connect()
            add("网络可达与认证", True, f"DuckDB 本地文件 · {int((time.perf_counter() - t0) * 1000)} ms")
        except DataSourceError as e:
            add("网络可达与认证", False, f"{e}｜{e.hint}")
            return checks

        add("账号为只读", True, "连接以 read_only=True 打开")
        add("语句超时已设置", True, f"看门狗 {self.cfg.raw['guard']['statement_timeout_ms']} ms")
        add("连接数上限已设置", True, "单进程单连接")

        try:
            actual = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        except Exception as e:  # pragma: no cover
            add("授权表集合", False, str(e))
            return checks
        allow = set(self.cfg.tables)
        missing = allow - actual
        if missing:
            add("授权表集合", False, f"白名单中的表在库里不存在：{', '.join(sorted(missing))}")
        else:
            add("授权表集合", True, f"白名单 {len(allow)} 张 · 库内共 {len(actual)} 张")

        # 写操作实探 —— 必须被拒绝
        probe = next(iter(sorted(allow)), None)
        if probe is None:
            add("写操作实探", False, "白名单为空，无法探测")
        else:
            try:
                con.execute(f"DELETE FROM {probe} WHERE 1=0")
                add("写操作实探", False, "写操作未被拒绝，连接并非只读")
            except Exception:
                add("写操作实探", True, "写操作已被引擎拒绝 ✓ 符合预期")
        return checks

    # ---------- R-11 干跑 ----------

    def explain(self, sql: str) -> ExplainResult:
        con = self.connect()
        try:
            rows = con.execute(f"EXPLAIN {sql}").fetchall()
        except Exception as e:
            return ExplainResult(est_rows=None, ok=False, reason=f"执行计划生成失败：{e}")

        plan = "\n".join(str(c) for r in rows for c in r if c is not None)
        nums = [int(m) for m in _EC.findall(plan)]
        est = max(nums) if nums else None

        cap = int(self.cfg.raw["guard"]["max_scan_rows"])
        if est is not None and est > cap:
            return ExplainResult(
                est_rows=est, plan=plan, ok=False,
                reason=f"预估扫描 {est:,} 行，超过阈值 {cap:,}",
            )
        return ExplainResult(est_rows=est, plan=plan)

    # ---------- 执行 ----------

    def run(self, sql: str) -> QueryResult:
        con = self.connect()
        cap = self.cfg.max_rows
        timeout_ms = int(self.cfg.raw["guard"]["statement_timeout_ms"])

        # R-12：DuckDB 无 statement_timeout，用看门狗线程中断
        fired = threading.Event()

        def watchdog() -> None:
            if not done.wait(timeout_ms / 1000):
                fired.set()
                try:
                    con.interrupt()
                except Exception:  # pragma: no cover
                    pass

        done = threading.Event()
        t = threading.Thread(target=watchdog, daemon=True)
        t.start()
        t0 = time.perf_counter()
        try:
            cur = con.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(cap + 1)          # 多取一行用于判断是否被截断
        except Exception as e:
            if fired.is_set():
                raise DataSourceError(
                    f"查询超时（超过 {timeout_ms} ms 已中断）",
                    hint="缩小时间范围或增加筛选条件；这是 R-12 语句超时护栏。",
                ) from e
            raise
        finally:
            done.set()

        elapsed = int((time.perf_counter() - t0) * 1000)
        truncated = len(rows) > cap
        if truncated:
            rows = rows[:cap]
        return QueryResult(
            columns=columns,
            rows=[list(r) for r in rows],
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed,
        )
