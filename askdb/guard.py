"""静态校验与强制改写 —— 本项目的核心模块。

设计约束（技术设计说明书 §4）：
  1. 全部判定基于 AST，**不做任何字符串匹配**。
     注释、大小写、编码变形都能绕过字符串匹配，但解析器会先行规范化。
  2. 强制改写在 AST 上完成后重新生成 SQL，模型无法通过任何提示词手段覆盖。
  3. 表引用收集必须遍历完整 AST：FROM / JOIN / 子查询 / CTE / IN(SELECT) / EXISTS / UNION。
     **漏掉任一分支即构成绕过路径。**

本模块实现 R-01～R-10、R-19 与 R-20；R-11～R-14 在 executor / graph，R-15～R-17 在 planner。
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
    # R-20 是"解析成本超预算"的拒绝：绝不能再走 referenced_tables 解析一次 ——
    # 那正是它要避开的那次昂贵解析（否则超长 SQL 在这里又被完整 parse 一遍）。
    if not r.tables and r.rejected_by != "R-20":
        r.tables = referenced_tables(sql, dialect)
    return r


def _check(sql: str, cfg: Config, org_id: int, dialect: str = "duckdb") -> GuardResult:
    fired: list[str] = []
    rewrites: list[str] = []

    # ---------- R-20 解析预算（在 sqlglot.parse 之前，纯字符扫描）----------
    # 护栏的 AST 解析成本随 SQL 体量**超线性**增长：实测一条 19KB 的合法
    # UNION ALL 仅解析就占 CPU ~4s，而 R-12 的语句超时只管 PG 执行阶段、
    # 管不到解析。0.5 核副本上十来个这种请求即可把它拖垮。所以在进解析器
    # 之前先用两个 O(n) 的廉价指标拒掉：文本长度、括号嵌套深度。
    #
    # 两个指标缺一不可：长度挡"长而浅"（UNION ALL ×N、超长算术链），
    # 深度挡"短而深"（4KB 文本嵌套两千层括号，长度过关但解析照样吃 CPU）。
    gcfg = cfg.raw.get("guard", {})
    max_chars = int(gcfg.get("max_sql_chars", 6000))
    if max_chars > 0 and len(sql) > max_chars:
        return GuardResult(
            ok=False, rejected_by="R-20",
            reason=f"SQL 文本 {len(sql)} 字符，超过上限 {max_chars}；"
                   "过长文本仅解析就可能占满 CPU，已在解析前拒绝。",
        )
    max_depth = int(gcfg.get("max_nesting_depth", 100))
    if max_depth > 0:
        depth = peak = 0
        in_str = False
        for ch in sql:
            # 跳过单引号字符串内的括号：WHERE note = '(a(b' 不该算嵌套。
            # 简单切换即可——这是解析前的粗筛，不需要处理 '' 转义的每个边角。
            if ch == "'":
                in_str = not in_str
            elif not in_str:
                if ch == "(":
                    depth += 1
                    if depth > peak:
                        peak = depth
                elif ch == ")":
                    if depth > 0:
                        depth -= 1
        if peak > max_depth:
            return GuardResult(
                ok=False, rejected_by="R-20",
                reason=f"SQL 括号嵌套深度 {peak}，超过上限 {max_depth}；"
                       "深层嵌套仅解析就可能占满 CPU，已在解析前拒绝。",
            )

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
            # CTE 别名遮蔽同名真实表时，这里引用的是 CTE，不是那张表 ——
            # 往它身上注入租户谓词会拼出一条引用不存在列的 SQL，
            # 干跑阶段直接 Binder Error。R-03 与 R-04 已经这么判了，
            # R-10 也必须一致：三条规则对"这个名字指谁"的认定不能各说各话。
            if name in ctes:
                continue
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
            # 幂等：完全相同的谓词已经在了就不再叠加。
            # 当前生产路径不会二次改写（每轮都对 sql_raw 重新校验），
            # 但只要有一处对改写结果再跑一次，SQL 就会持续膨胀成
            # `org_id = 65 AND org_id = 65 AND ...`。
            target = join.args.get("on") if (join is not None and _is_outer(join)) \
                else s.args.get("where")
            if target is not None and _has_condition(target, cond):
                continue

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

    # ---------- R-19 数据期限窗口注入 ----------
    # 与 R-10 是同一类动作：都往 SQL 里加一个行级条件。因此这里刻意照抄
    # 它的形状（外连接进 ON、幂等去重、未声明即拒），而不是另发明一套 ——
    # 两条规则对"怎么加条件"的做法不一致，是最容易长出 bug 的地方。
    window = cfg.window_days
    if window is not None and cfg.window_enforceable:
        from datetime import datetime, timedelta

        cutoff = (datetime.now().astimezone() - timedelta(days=int(window)))
        cutoff_lit = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        aged: list[str] = []
        no_time: set[str] = set()
        for sel in root.find_all(exp.Select):
            for t, join in _direct_tables(sel):
                name = (t.name or "").lower()
                if name in ctes:            # CTE 遮蔽，与 R-03/R-04/R-10 认定一致
                    continue
                spec = cfg.tables.get(name)
                if spec is None or spec.time_exempt:
                    continue
                col = spec.time_column
                if not col:
                    # 朝安全的方向失败。静默放行等于把"只能看 90 天"变成空话，
                    # 而那句话是写在权限页上给人看的。
                    no_time.add(name)
                    continue
                ref = t.alias_or_name
                text = f"{ref}.{col} >= '{cutoff_lit}'"
                try:
                    cond = exp.condition(text, dialect=dialect)
                except Exception:
                    return GuardResult(
                        ok=False, rejected_by="R-19",
                        reason=f"表 {name} 的时间窗口谓词无法解析：{text}",
                    )
                target = join.args.get("on") if (join is not None and _is_outer(join)) \
                    else sel.args.get("where")
                if target is not None and _has_condition(target, cond):
                    continue
                if join is not None and _is_outer(join):
                    join.on(cond, copy=False)
                else:
                    sel.where(cond, copy=False)
                aged.append(text)

        if no_time:
            return GuardResult(
                ok=False, rejected_by="R-19",
                reason=(f"当前角色只能查看最近 {window} 天的数据，"
                        f"但这些表没有声明时间列：{'、'.join(sorted(no_time))}。"
                        "请在表配置里给时间列标 time: true，"
                        "或对确无时间维度的维表标 time_exempt: true。"),
            )
        if aged:
            fired.append("R-19")
            rewrites.append(f"注入数据期限窗口（最近 {window} 天）："
                            + "、".join(dict.fromkeys(aged)))

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


def _has_condition(where_or_on: exp.Expression, cond: exp.Expression) -> bool:
    """已有条件里是否已包含语义相同的一条。

    按规范化后的 SQL 文本比对 AST 节点 —— 比字符串匹配可靠（大小写、
    空白、括号都已被解析器抹平），也比逐字段比较简单。
    """
    want = cond.sql()
    # WHERE 是 exp.Where 包一层；JOIN 的 on 在 sqlglot 30 里是**裸表达式**，
    # 没有 exp.On 这个节点类型（第一版按 exp.On 写，直接 AttributeError）。
    node = where_or_on.this if isinstance(where_or_on, exp.Where) else where_or_on
    if node is None:
        return False
    stack = [node]
    while stack:
        cur = stack.pop()
        # 括号只是分组，不改变合取语义 —— 不剥开它，
        # `((a AND t) AND t2)` 里的租户谓词就永远比对不上，
        # 把改写结果贴回直查框会看到谓词重复叠加（幂等失效）。
        if isinstance(cur, exp.Paren):
            if cur.this is not None:
                stack.append(cur.this)
            continue
        if cur.sql() == want:
            return True
        if isinstance(cur, exp.And):
            stack.extend([cur.left, cur.right])
    return False


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
            # CTE 别名遮蔽同名真实表时，这里引用的是 CTE 不是表 ——
            # R-03 已经这么判了（所以不报表白名单错），R-04 也必须一致，
            # 否则 WITH documents AS (SELECT 1 AS x) SELECT x FROM documents
            # 会被判「字段 x 不存在」，合法 SQL 被误杀且报错还在误导。
            if n in ctes:
                continue
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


# ---------------------------------------------------------------------------
# 脱敏落点解析（P03）
#
# executor 拿到结果时，手里只有**返回列名**。模型写 `phone AS 手机号`，
# 返回列名就是"手机号"，按列名匹配的脱敏一条也匹配不上 —— 2026-09-06 实测
# 匿名访客据此拿到了明文手机号。列名匹配不是"够不够严"的问题，是**别名一改
# 就整层失效**，属于静默失效，最坏的那一类。
#
# 因此改在 AST 上判：哪一个返回列是从敏感列算出来的，是解析器能回答的问题，
# 别名改不动它。落点仍在返回值上（不改 SQL），原因见 executor._mask 的注释：
# 改 SQL 会悄悄改变 COUNT(DISTINCT phone) 这类口径的语义。
# ---------------------------------------------------------------------------


def _sensitive_names(cfg: Config) -> set[str]:
    return {c.lower() for t in cfg.tables.values() for c in t.sensitive_columns}


def _table_flags(cfg: Config, table: str) -> dict[str, bool] | None:
    t = cfg.tables.get(table.lower())
    if t is None:
        return None
    return {c.name.lower(): c.sensitive for c in t.columns.values()}


def _is_email_domain(node: exp.Expression) -> bool:
    """是不是"取邮箱 @ 后段（域名）"这一种确定写法：SPLIT_PART(列, '@', 2)。

    只认这一个形状：分隔符必须是字符串 '@'、段号必须是数字 2、被切的必须是
    一个裸列。段号 1（本地名，含个人信息）、其它 substring/正则切法一律不认 ——
    脱敏放宽必须是精确的、可枚举的，不能给一个能提取任意子串的通用口子。
    """
    if not isinstance(node, exp.SplitPart):
        return False
    if not isinstance(node.this, exp.Column):
        return False
    delim = node.args.get("delimiter")
    part = node.args.get("part_index")
    return (isinstance(delim, exp.Literal) and delim.is_string and delim.this == "@"
            and isinstance(part, exp.Literal) and not part.is_string
            and str(part.this) == "2")


def _select_flags(select: exp.Select, cfg: Config,
                  outer: dict[str, dict[str, bool]] | None = None,
                  ) -> list[tuple[str, bool]] | None:
    """一层 SELECT 的每个输出列：(输出名, 是否由敏感列算出)。

    返回 None = **解析不了**。调用方必须按"不确定就当敏感"处理 ——
    这一层的每一次放行都是一次可能的泄露，静默放行是不能接受的失败方向。
    """
    # 先解出本层可见的来源：真实表、CTE、子查询，都归一成 列名→是否敏感。
    scope: dict[str, dict[str, bool]] = dict(outer or {})
    # 键名与 _from_node 同一个坑：sqlglot 30 把 "with" 改成了 "with_"。
    # 取不到时这里会退化成"整层解析不出"→ 全列脱敏，不是漏脱敏，
    # 但那等于把带 CTE 的查询全打成星号，照样得两个键都认。
    with_ = select.args.get("with") or select.args.get("with_")
    for cte in (with_.expressions if with_ else []):
        inner = cte.this
        if not isinstance(inner, exp.Select):
            return None
        flags = _select_flags(inner, cfg, scope)
        if flags is None:
            return None
        scope[cte.alias_or_name.lower()] = {n.lower(): s for n, s in flags}

    sources: list[dict[str, bool]] = []
    frm = _from_node(select)
    parts = []
    if frm is not None:
        parts.append(frm.this)
        parts += list(frm.args.get("expressions") or [])
    parts += [j.this for j in (select.args.get("joins") or [])]
    for node in parts:
        if isinstance(node, exp.Table):
            name = (node.name or "").lower()
            flags = scope.get(name) or _table_flags(cfg, name)
            if flags is None:
                return None                    # 未知来源：交给调用方保守处理
            scope[(node.alias or name).lower()] = flags
            sources.append(flags)
        elif isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select):
            sub = _select_flags(node.this, cfg, scope)
            if sub is None:
                return None
            flags = {n.lower(): s for n, s in sub}
            scope[(node.alias or "").lower()] = flags
            sources.append(flags)
        else:
            return None

    def _col_sensitive(col: exp.Column) -> bool:
        cname = (col.name or "").lower()
        qualifier = (col.table or "").lower()
        if qualifier:
            flags = scope.get(qualifier)
            # 限定名指向本层解不出的作用域（相关子查询引用外层等）：不确定，从严
            return True if flags is None else flags.get(cname, False)
        # 未限定：任一来源里同名列敏感就算敏感。多表作用域下无法判断这一列
        # 到底出自哪张表，而**猜错的方向必须是多脱敏**。
        return any(f.get(cname, False) for f in sources) if sources else True

    out: list[tuple[str, bool]] = []
    for i, proj in enumerate(select.expressions):
        if isinstance(proj, exp.Star) or (
                isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)):
            # R-05 会在执行前把 * 展开，正常路径到不了这里；真到了就说明
            # 输出列集合未知，无法逐列判定。
            return None
        inner = proj.unalias() if isinstance(proj, exp.Alias) else proj
        cols = list(inner.find_all(exp.Column))
        if isinstance(inner, exp.Count):
            # COUNT 只暴露"有多少个"，不暴露值本身；把它脱敏等于把数字毁掉。
            # MIN/MAX 不在此列 —— 它们原样吐出某一行的真值。
            flag = False
        elif _is_email_domain(inner):
            # 邮箱域名（@ 后段）不是个人标识信息：gmail.com / qq.com 指向的是
            # 服务商而非某个人。而按域名分组统计（SPLIT_PART(email,'@',2) 再
            # GROUP BY）是常见的合法分析，把域名整列打成星号会让聚合结果不可读。
            # **只豁免第 2 段这一种确定写法** —— 第 1 段是本地名（含个人信息）、
            # 其它 substring/切法一律不认，从严兜底，绕过面仅限"域名"本身。
            flag = False
        elif cols:
            flag = any(_col_sensitive(c) for c in cols)
        else:
            flag = False                       # 常量、CURRENT_DATE 之类
        name = proj.alias_or_name or (cols[0].name if cols else f"col{i}")
        out.append((str(name), flag))
    return out


def sensitive_output_columns(sql: str, cfg: Config,
                             dialect: str = "duckdb") -> set[int] | None:
    """这条 SQL 的哪几个返回列必须脱敏（按位置）。

    None = 解析不出。调用方退回按列名匹配，并且**额外把解析失败这件事记下来**
    —— 悄悄退化成一个更弱的判定，正是这次要修掉的那种失效。
    """
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except Exception:
        return None
    if len(stmts) != 1:
        return None
    root = stmts[0]

    if isinstance(root, exp.Union):
        # UNION 各分支按位置对齐，任一分支敏感则该位置敏感
        sides = [root.this, root.expression]
        per: list[list[tuple[str, bool]]] = []
        for s in sides:
            if not isinstance(s, exp.Select):
                return None
            f = _select_flags(s, cfg)
            if f is None:
                return None
            per.append(f)
        width = min(len(f) for f in per)
        return {i for i in range(width) if any(f[i][1] for f in per)}

    if not isinstance(root, exp.Select):
        return None
    flags = _select_flags(root, cfg)
    if flags is None:
        return None
    return {i for i, (_, s) in enumerate(flags) if s}
