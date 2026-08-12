"""静态校验与强制改写 —— 本项目的核心模块。

设计约束（技术设计说明书 §4）：
  1. 全部判定基于 AST，**不做任何字符串匹配**。
     注释、大小写、编码变形都能绕过字符串匹配，但解析器会先行规范化。
  2. 强制改写在 AST 上完成后重新生成 SQL，模型无法通过任何提示词手段覆盖。
  3. 表引用收集必须遍历完整 AST：FROM / JOIN / 子查询 / CTE / IN(SELECT) / EXISTS / UNION。
     **漏掉任一分支即构成绕过路径。**

本模块实现 R-01～R-10；R-11～R-14 在 executor / graph，R-15～R-17 在 planner。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .config import Config

# 本模块之外实现的规则 —— 显式列出，避免误以为护栏只有这里这些
ENFORCED_ELSEWHERE = {
    "R-11": "executor.explain（EXPLAIN 扫描行数阈值）",
    "R-12": "executor（语句超时）",
    "R-13": "executor.run（结果行上限）",
    "R-14": "graph 路由（重试次数上限）",
}
NOT_YET_ENFORCED: list[str] = []


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""                                  # 改写后的最终 SQL
    rejected_by: str | None = None                 # 规则编号
    reason: str = ""
    rules_fired: list[str] = field(default_factory=list)   # 触发的改写
    rewrites: list[str] = field(default_factory=list)      # 人类可读的改写说明
    tables: set[str] = field(default_factory=set)          # 这条 SQL 引用到的表（含被拒的）

    @property
    def out_of_scope(self) -> bool:
        """这次拒绝是"问题超出范围"，而不是"SQL 写错了"。

        两类拒绝的重试价值完全不同：
          · 写错了（R-04 字段不存在、执行报错）—— 模型据错误信息能改对
          · 超范围（R-02/R-03/R-06/R-07）—— 表不会因为再问一次就开放

        对第二类无脑重试，实测出现过一次危险后果：问"chunks 表有多少行"
        被 R-03 拦下后，模型改成了 SELECT COUNT(*) FROM documents 并成功执行
        —— 用户问 A、系统答 B，还返回一个看起来完全合理的数字。
        评测里表现为应拒拦截率从 100% 掉到 75%（trace 8fd3676f7e65）。
        """
        return self.rejected_by in OUT_OF_SCOPE


# 「问题超出范围」类拒绝。这几条不是 SQL 写法问题，改写法救不回来。
OUT_OF_SCOPE = frozenset({"R-02", "R-03", "R-06", "R-07"})


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


def _is_outer(join: exp.Join) -> bool:
    side = str(join.args.get("side") or "").upper()
    kind = str(join.args.get("kind") or "").upper()
    return side in ("LEFT", "RIGHT", "FULL") or kind == "OUTER"


def _direct_tables(select: exp.Select) -> list[tuple[exp.Table, exp.Join | None]]:
    """该 SELECT 自身 FROM/JOIN 上的表，不递归进子查询。

    同时带回该表所属的 JOIN 节点（FROM 上的表为 None）——
    外连接的租户谓词要注进 ON 而不是 WHERE，否则会改变连接语义。

    子查询里的 SELECT 会被 find_all(exp.Select) 单独遍历到，
    各自独立注入租户谓词 —— 这正是设计要求的"每一层都注入"。
    """
    out: list[tuple[exp.Table, exp.Join | None]] = []
    frm = _from_node(select)
    if frm is not None:
        if isinstance(frm.this, exp.Table):
            out.append((frm.this, None))
        for e in (frm.args.get("expressions") or []):   # 旧版 sqlglot 的多表 FROM 形式
            if isinstance(e, exp.Table):
                out.append((e, None))
    for j in select.args.get("joins") or []:
        if isinstance(j.this, exp.Table):
            out.append((j.this, j))
    return out


def referenced_tables(sql: str, dialect: str = "duckdb") -> set[str]:
    """这条 SQL 引用到的真实表（不含 CTE 别名）。

    独立于 check 之外，因为被拒的 SQL 也要能取到表集合 —— 反思重试要拿
    重试前后的表集合做比对，判断模型是"改写法"还是"换了个东西答"。
    """
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except Exception:
        return set()
    out: set[str] = set()
    for root in stmts:
        ctes = _cte_names(root)
        for tb in root.find_all(exp.Table):
            n = (tb.name or "").lower()
            if n and n not in ctes:
                out.add(n)
    return out


def check(sql: str, cfg: Config, org_id: int, dialect: str = "duckdb") -> GuardResult:
    """校验并改写。返回的 sql 才是允许执行的那条。"""
    r = _check(sql, cfg, org_id, dialect)
    if not r.tables:
        r.tables = referenced_tables(sql, dialect)
    return r


def _check(sql: str, cfg: Config, org_id: int, dialect: str = "duckdb") -> GuardResult:
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

    # ---------- R-06 禁止跨 schema / 跨库引用 ----------
    # 表白名单只按表名匹配，`other_schema.documents` 会照样过 R-03 ——
    # 不拦住限定名，白名单就形同虚设。
    default_schemas = {s.lower() for s in cfg.raw["guard"].get("allowed_schemas", ["public", "main"])}
    for t in root.find_all(exp.Table):
        if (t.name or "").lower() in ctes:
            continue
        catalog = (t.args.get("catalog").name if t.args.get("catalog") else "") or ""
        schema = (t.args.get("db").name if t.args.get("db") else "") or ""
        if catalog:
            return GuardResult(
                ok=False, rejected_by="R-06",
                reason=f"禁止跨库引用：{catalog}.{schema or '?'}.{t.name}",
            )
        if schema and schema.lower() not in default_schemas:
            return GuardResult(
                ok=False, rejected_by="R-06",
                reason=f"禁止跨 schema 引用：{schema}.{t.name}"
                       f"（仅允许 {'、'.join(sorted(default_schemas))}）",
            )

    # ---------- R-08 笛卡尔积检测 ----------
    err = _check_cartesian(root)
    if err:
        return GuardResult(ok=False, rejected_by="R-08", reason=err)

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

    # ---------- R-05 展开 SELECT *（改写而非阻断）----------
    if not cfg.allow_select_star:
        expanded, err = _expand_stars(root, cfg, ctes)
        if err:
            return GuardResult(ok=False, rejected_by="R-05", reason=err)
        if expanded:
            fired.append("R-05")
            rewrites.append(f"展开 SELECT * 为显式列（{expanded} 处）")

    # ---------- R-10 强制租户谓词注入 ----------
    # 两种归属方式：
    #   直接 —— 表上有租户列，注入 ref.col = ctx
    #   间接 —— 表上没有租户列（真实库里很常见），用 tenant_filter 声明的谓词
    # 两者都没有、又没显式豁免的表，在配置加载期就会被拒，走不到这里。
    tcol = cfg.tenant_column
    injected: list[str] = []
    unresolved: set[str] = set()
    for s in (root.find_all(exp.Select) if cfg.tenant_enabled else []):
        for t, join in _direct_tables(s):
            name = (t.name or "").lower()
            spec = cfg.tables.get(name)
            if spec is None or spec.tenant_exempt:
                continue
            ref = t.alias_or_name
            if spec.tenant_column:
                text = f"{ref}.{spec.tenant_column} = {int(org_id)}"
            elif spec.tenant_filter:
                text = spec.tenant_filter.format(ref=ref, ctx=int(org_id))
            else:
                unresolved.add(name)
                continue
            try:
                cond = exp.condition(text, dialect=dialect)
            except Exception:
                return GuardResult(
                    ok=False, rejected_by="R-10",
                    reason=f"表 {name} 的租户谓词无法解析：{text}",
                )
            # 外连接的谓词必须进 ON，不能进 WHERE ——
            # 放进 WHERE 会把 LEFT/RIGHT/FULL JOIN 悄悄降级成 INNER JOIN，
            # 结果少行且不报错，属于最难发现的一类改写事故。
            if join is not None and _is_outer(join):
                join.on(cond, copy=False)
            else:
                s.where(cond, copy=False)
            injected.append(text)

    if unresolved:
        # 失败要朝安全的方向失败（tenant.on_unresolved=reject）
        return GuardResult(
            ok=False, rejected_by="R-10",
            reason=f"无法确定租户归属，拒绝执行：{'、'.join(sorted(unresolved))}",
        )
    if injected:
        fired.append("R-10")
        rewrites.append("注入租户谓词：" + "、".join(dict.fromkeys(injected)))

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


def _is_tautology(cond: exp.Expression) -> bool:
    """恒真的连接条件等同于没有条件 —— `ON 1=1` 是最常见的绕过写法。"""
    if isinstance(cond, exp.Boolean) and cond.this is True:
        return True
    if isinstance(cond, exp.EQ):
        left, right = cond.this, cond.expression
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return left.name == right.name
    if isinstance(cond, exp.And):
        return all(_is_tautology(x) for x in (cond.this, cond.expression))
    return False


def _check_cartesian(root: exp.Expression) -> str | None:
    """R-08：拦截笛卡尔积。

    三种写法都要认：CROSS JOIN、JOIN 缺 ON、以及 `FROM a, b` 这种逗号连接
    （sqlglot 把它也解析成一个没有 ON 的 Join）。
    再加上 `ON 1=1` 这类恒真条件 —— 写了等于没写。

    笛卡尔积在小表上只是慢，在几十万行的表之间会直接把库压垮，
    所以这条是阻断而非改写。
    """
    for s in root.find_all(exp.Select):
        for j in s.args.get("joins") or []:
            if not isinstance(j.this, (exp.Table, exp.Subquery)):
                continue
            name = j.this.alias_or_name or "子查询"
            kind = str(j.args.get("kind") or "").upper()
            on = j.args.get("on")
            using = j.args.get("using")

            if kind == "CROSS":
                return f"禁止 CROSS JOIN（笛卡尔积）：{name}"
            if on is None and not using:
                return (f"连接 {name} 时缺少 ON 条件，会产生笛卡尔积；"
                        f"逗号分隔的多表 FROM 也属于此类，请改写成显式 JOIN … ON")
            if on is not None and _is_tautology(on):
                return f"连接 {name} 的 ON 条件恒真（{on.sql()}），等同于笛卡尔积"
    return None


def _expand_stars(root: exp.Expression, cfg: Config, ctes: set[str]) -> tuple[int, str | None]:
    """R-05：把 `*` / `t.*` 展开成显式列。

    设计要求这一条是**改写**而非阻断（§4.1，阻断=否）：目的是控制列暴露面，
    而不是给用户添堵。展开后列固定，schema 变更也不会悄悄多带出字段。

    无法安全展开时（涉及 CTE 等本模块看不到列定义的来源）才拒绝。
    返回 (展开处数, 错误信息)。
    """
    count = 0
    for s in root.find_all(exp.Select):
        scope = [t for t, _ in _direct_tables(s) if (t.name or "").lower() in cfg.tables]
        new_exprs: list[exp.Expression] = []
        changed = False

        for item in s.expressions:
            # `t.*`
            if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                qual = (item.table or "").lower()
                if qual in ctes:
                    return count, f"无法展开 {item.table}.*：该来源是 CTE，列由其自身 SELECT 决定"
                tbl = next((t for t in scope if t.alias_or_name.lower() == qual
                            or (t.name or "").lower() == qual), None)
                if tbl is None:
                    return count, f"无法展开 {item.table}.*：{item.table} 不在本层查询的表引用中"
                ref = tbl.alias_or_name
                for c in cfg.tables[(tbl.name or "").lower()].columns:
                    new_exprs.append(exp.column(c, table=ref))
                changed = True
                count += 1
                continue

            # 裸 `*`
            if isinstance(item, exp.Star):
                if not scope:
                    return count, "无法展开 SELECT *：本层查询没有可解析的表引用"
                if any((t.name or "").lower() in ctes for t in scope):
                    return count, "无法展开 SELECT *：查询引用了 CTE，列不可静态确定"
                for t in scope:
                    ref = t.alias_or_name
                    for c in cfg.tables[(t.name or "").lower()].columns:
                        new_exprs.append(exp.column(c, table=ref))
                changed = True
                count += 1
                continue

            new_exprs.append(item)

        if changed:
            s.set("expressions", new_exprs)
    return count, None


def _check_columns(root: exp.Expression, cfg: Config, ctes: set[str]) -> str | None:
    """R-04：拦截幻觉字段。返回错误信息，None 表示通过。"""
    for s in root.find_all(exp.Select):
        # 该作用域内 别名/表名 -> 表定义
        # SELECT 列表里定义的输出别名。ORDER BY / GROUP BY / HAVING 引用它们是
        # 合法 SQL，但它们并不是任何表的列 —— 不排除就会误杀正确查询。
        aliases = {
            (e.alias or "").lower()
            for e in s.expressions if isinstance(e, exp.Alias) and e.alias
        }

        scope: dict[str, str] = {}
        for t, _join in _direct_tables(s):
            n = (t.name or "").lower()
            if n in cfg.tables:
                scope[t.alias_or_name.lower()] = n
                scope[n] = n

        for col in s.find_all(exp.Column):
            cname = (col.name or "").lower()
            if not cname or cname == "*":
                continue
            qualifier = (col.table or "").lower()
            if not qualifier and cname in aliases:
                continue                       # 引用的是本层的输出别名，不是表列

            if qualifier:
                if qualifier in ctes:
                    continue                       # CTE 的列由其自身 SELECT 保证
                tbl = scope.get(qualifier)
                if tbl is None:
                    continue                       # 不在本作用域，交给外层处理
                if cname not in cfg.tables[tbl].columns:
                    return _no_column(cfg, tbl, f"{qualifier}.{col.name}")
            else:
                real = {v for k, v in scope.items() if v in cfg.tables}
                if len(real) != 1:
                    continue                       # 多表作用域，P1 再做完整解析
                tbl = next(iter(real))
                if cname not in cfg.tables[tbl].columns:
                    return _no_column(cfg, tbl, col.name)
    return None


def _no_column(cfg: Config, table: str, shown: str) -> str:
    """报错时把真实列名一并给出。

    只说"字段不存在"，人和模型都得再问一轮才知道该写什么；
    直接列出可用字段，重试一次就能改对。
    """
    cols = list(cfg.tables[table].columns)
    listed = "、".join(cols[:12]) + ("…" if len(cols) > 12 else "")
    return f"字段不存在：{shown}（表 {table} 无此列）。该表可用字段：{listed}"
