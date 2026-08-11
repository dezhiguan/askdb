"""静态校验与强制改写 —— 本项目的核心模块。

设计约束（技术设计说明书 §4）：
  1. 全部判定基于 AST，**不做任何字符串匹配**。
     注释、大小写、编码变形都能绕过字符串匹配，但解析器会先行规范化。
  2. 强制改写在 AST 上完成后重新生成 SQL，模型无法通过任何提示词手段覆盖。
  3. 表引用收集必须遍历完整 AST：FROM / JOIN / 子查询 / CTE / IN(SELECT) / EXISTS / UNION。
     **漏掉任一分支即构成绕过路径。**

P0 已实现：R-01 R-02 R-03 R-04 R-05 R-07 R-09 R-10
P1 待补：  R-06（跨 schema）R-08（笛卡尔积）R-11（EXPLAIN 扫描行数）
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .config import Config

# P1 待补规则 —— 显式列出，避免"看起来全都实现了"的错觉
NOT_YET_ENFORCED = ["R-06", "R-08", "R-11"]


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""                                  # 改写后的最终 SQL
    rejected_by: str | None = None                 # 规则编号
    reason: str = ""
    rules_fired: list[str] = field(default_factory=list)   # 触发的改写
    rewrites: list[str] = field(default_factory=list)      # 人类可读的改写说明


def _normalize(name: str) -> str:
    return str(name).replace("_", "").lower()


def _cte_names(root: exp.Expression) -> set[str]:
    out: set[str] = set()
    for cte in root.find_all(exp.CTE):
        if cte.alias:
            out.add(cte.alias.lower())
    return out


def _from_node(select: exp.Select) -> exp.From | None:
    """取该 SELECT 自身的 FROM 节点。

    sqlglot 30 把 args 键从 "from" 改成了 "from_"（破坏性变更），
    这里同时兼容两种，避免升级依赖时静默失效 ——
    一旦取不到 FROM，R-04 与 R-10 会一起失效，属于高危静默故障。
    """
    for key in ("from_", "from"):
        node = select.args.get(key)
        if isinstance(node, exp.From):
            return node
    return None


def _direct_tables(select: exp.Select) -> list[exp.Table]:
    """该 SELECT 自身 FROM/JOIN 上的表，不递归进子查询。

    子查询里的 SELECT 会被 find_all(exp.Select) 单独遍历到，
    各自独立注入租户谓词 —— 这正是设计要求的"每一层都注入"。
    """
    out: list[exp.Table] = []
    frm = _from_node(select)
    if frm is not None:
        if isinstance(frm.this, exp.Table):
            out.append(frm.this)
        for e in (frm.args.get("expressions") or []):   # 旧版 sqlglot 的多表 FROM 形式
            if isinstance(e, exp.Table):
                out.append(e)
    for j in select.args.get("joins") or []:
        if isinstance(j.this, exp.Table):
            out.append(j.this)
    return out


def check(sql: str, cfg: Config, org_id: int, dialect: str = "duckdb") -> GuardResult:
    """校验并改写。返回的 sql 才是允许执行的那条。"""
    fired: list[str] = []
    rewrites: list[str] = []

    # ---------- R-01 单语句限制 ----------
    try:
        stmts = sqlglot.parse(sql, dialect=dialect)
    except Exception as e:  # 解析失败本身就是拒绝理由
        return GuardResult(ok=False, rejected_by="R-01", reason=f"SQL 无法解析：{e}")

    stmts = [s for s in stmts if s is not None]
    if len(stmts) != 1:
        return GuardResult(
            ok=False, rejected_by="R-01",
            reason=f"只允许单条语句，实际解析出 {len(stmts)} 条（多语句夹带）",
        )
    root = stmts[0]

    # ---------- R-02 语句类型白名单 ----------
    if not isinstance(root, (exp.Select, exp.Union)):
        return GuardResult(
            ok=False, rejected_by="R-02",
            reason=f"只允许 SELECT / WITH…SELECT，实际是 {type(root).__name__.upper()}",
        )

    ctes = _cte_names(root)
    allow = set(cfg.tables)

    # ---------- R-03 表白名单 ----------
    referenced: set[str] = set()
    for t in root.find_all(exp.Table):
        n = (t.name or "").lower()
        if n and n not in ctes:
            referenced.add(n)
    unknown = referenced - allow
    if unknown:
        return GuardResult(
            ok=False, rejected_by="R-03",
            reason=f"引用了不在白名单内的表：{', '.join(sorted(unknown))}",
        )

    # ---------- R-07 危险函数黑名单 ----------
    deny = {_normalize(f) for f in cfg.deny_functions}
    for fn in root.find_all(exp.Func):
        name = fn.this if isinstance(fn, exp.Anonymous) else fn.key
        if _normalize(name) in deny:
            return GuardResult(
                ok=False, rejected_by="R-07",
                reason=f"使用了禁用函数：{name}",
            )

    # ---------- R-04 字段真实性 ----------
    # P0 覆盖：带表限定的字段，以及作用域内只有一张表时的裸字段。
    # 多表 JOIN 下的裸字段留待 P1 用 sqlglot.qualify 做完整解析。
    err = _check_columns(root, cfg, ctes)
    if err:
        return GuardResult(ok=False, rejected_by="R-04", reason=err)

    # ---------- R-05 禁止 SELECT * ----------
    if not cfg.allow_select_star:
        for s in root.find_all(exp.Select):
            if any(isinstance(e, exp.Star) for e in s.expressions):
                return GuardResult(
                    ok=False, rejected_by="R-05",
                    reason="禁止 SELECT *，请显式列出需要的字段（控制列暴露面）",
                )

    # ---------- R-10 强制租户谓词注入 ----------
    tenant_tables = cfg.tenant_tables()
    tcol = cfg.tenant_column
    injected: list[str] = []
    for s in root.find_all(exp.Select):
        for t in _direct_tables(s):
            if (t.name or "").lower() in tenant_tables:
                ref = t.alias_or_name
                cond = exp.condition(f"{ref}.{tcol} = {int(org_id)}", dialect=dialect)
                s.where(cond, copy=False)
                injected.append(f"{ref}.{tcol} = {org_id}")
    if injected:
        fired.append("R-10")
        rewrites.append("注入租户谓词：" + "、".join(dict.fromkeys(injected)))
    elif referenced & tenant_tables:
        # 引用了带租户列的表却没能注入 —— 按 on_unresolved=reject 处理
        return GuardResult(
            ok=False, rejected_by="R-10",
            reason="无法确定租户归属，拒绝执行（tenant.on_unresolved=reject）",
        )

    # ---------- R-09 强制 LIMIT 注入 ----------
    cap = cfg.max_rows
    outer = root
    cur = outer.args.get("limit")
    if cur is None:
        outer.limit(cap, copy=False)
        fired.append("R-09")
        rewrites.append(f"注入 LIMIT {cap}")
    else:
        try:
            n = int(cur.expression.name)
            if n > cap:
                outer.limit(cap, copy=False)
                fired.append("R-09")
                rewrites.append(f"LIMIT {n} 超过上限，下调为 {cap}")
        except (AttributeError, ValueError):
            outer.limit(cap, copy=False)
            fired.append("R-09")
            rewrites.append(f"LIMIT 表达式不可静态求值，改写为 {cap}")

    return GuardResult(
        ok=True,
        sql=root.sql(dialect=dialect, pretty=True),
        rules_fired=fired,
        rewrites=rewrites,
    )


def _check_columns(root: exp.Expression, cfg: Config, ctes: set[str]) -> str | None:
    """R-04：拦截幻觉字段。返回错误信息，None 表示通过。"""
    for s in root.find_all(exp.Select):
        # 该作用域内 别名/表名 -> 表定义
        scope: dict[str, str] = {}
        for t in _direct_tables(s):
            n = (t.name or "").lower()
            if n in cfg.tables:
                scope[t.alias_or_name.lower()] = n
                scope[n] = n

        for col in s.find_all(exp.Column):
            cname = (col.name or "").lower()
            if not cname or cname == "*":
                continue
            qualifier = (col.table or "").lower()

            if qualifier:
                if qualifier in ctes:
                    continue                       # CTE 的列由其自身 SELECT 保证
                tbl = scope.get(qualifier)
                if tbl is None:
                    continue                       # 不在本作用域，交给外层处理
                if cname not in cfg.tables[tbl].columns:
                    return f"字段不存在：{qualifier}.{col.name}（表 {tbl} 无此列）"
            else:
                real = {v for k, v in scope.items() if v in cfg.tables}
                if len(real) != 1:
                    continue                       # 多表作用域，P1 再做完整解析
                tbl = next(iter(real))
                if cname not in cfg.tables[tbl].columns:
                    return f"字段不存在：{col.name}（表 {tbl} 无此列）"
    return None
