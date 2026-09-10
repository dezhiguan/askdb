"""静态校验与强制改写 —— 本项目的核心模块。

设计约束（技术设计说明书 §4）：
  1. 全部判定基于 AST，**不做任何字符串匹配**。
     注释、大小写、编码变形都能绕过字符串匹配，但解析器会先行规范化。
  2. 强制改写在 AST 上完成后重新生成 SQL，模型无法通过任何提示词手段覆盖。
  3. 表引用收集必须遍历完整 AST：FROM / JOIN / 子查询 / CTE / IN(SELECT) / EXISTS / UNION。
     **漏掉任一分支即构成绕过路径。**

本模块实现 R-01～R-10、R-19～R-23；R-11～R-14 在 executor / graph，R-15～R-17 在 planner。
"""

from __future__ import annotations

import re
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
    #: 放行了、但读结果的人必须知道的话。
    #:
    #: 与 rewrites 分开：rewrites 说的是"SQL 被系统改成了什么"，notes 说的是
    #: "这条 SQL 本身有个不该忽略的地方"。混在一起会让"我们动了手"和
    #: "你得自己核对"变成同一句话，而这两件事的责任方不同。
    notes: list[str] = field(default_factory=list)

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

#: 抽样子句。让 EXPLAIN 的扫描估算失真，且结果是**抽样值**而不是真值 ——
#: 模型曾用 `COUNT(*) FROM t TABLESAMPLE SYSTEM (1)` 绕过 R-11 的扫描阈值，
#: 把 1% 的行数当成总行数返回，误差两个数量级且响应上毫无痕迹。
#: 这条不进 OUT_OF_SCOPE：去掉抽样就是一条正常 SQL，值得让模型重试一次。
_SAMPLE_NODES: tuple[type, ...] = tuple(
    n for n in (getattr(exp, "TableSample", None),) if n is not None
)


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


def _iter_samples(root: exp.Expression):
    """AST 里的抽样子句。sqlglot 各版本节点名不同，按可用类型遍历。"""
    for node_type in _SAMPLE_NODES:
        yield from root.find_all(node_type)


def _sample_label(node: exp.Expression) -> str:
    try:
        return node.sql()[:60]
    except Exception:
        return type(node).__name__


def check(sql: str, cfg: Config, org_id: int, dialect: str = "duckdb",
          question: str = "") -> GuardResult:
    """校验并改写。返回的 sql 才是允许执行的那条。

    question 只被 R-24 用到：判断"用户问的是相对时间吗"必须看原问题，
    SQL 本身看不出来。直查模式没有问题文本，传空即跳过该规则。
    """
    r = _check(sql, cfg, org_id, dialect, question)
    # R-20 是"解析成本超预算"的拒绝：绝不能再走 referenced_tables 解析一次 ——
    # 那正是它要避开的那次昂贵解析（否则超长 SQL 在这里又被完整 parse 一遍）。
    if not r.tables and r.rejected_by != "R-20":
        r.tables = referenced_tables(sql, dialect)
    return r


def _check(sql: str, cfg: Config, org_id: int, dialect: str = "duckdb",
           question: str = "") -> GuardResult:
    fired: list[str] = []
    rewrites: list[str] = []
    notes: list[str] = []

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


    # ---------- R-21 禁止抽样 ----------
    # 抽样让 EXPLAIN 的扫描估算失真（R-11 因此被绕开），返回的又是抽样值而非真值。
    # 两件事叠起来就是：一个被放大了几十上百倍的错数，且链路上处处显示正常。
    for ts in _iter_samples(root):
        return GuardResult(
            ok=False, rejected_by="R-21",
            reason=f"禁止抽样查询（{_sample_label(ts)}）："
                   "抽样结果不是真值，不能作为答案返回",
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

    # ---------- R-23 答案必须来自数据 ----------
    # 一条不引用任何表的 SELECT，结果与库里的数据毫无关系。这不是理论风险：
    # 召回没给到 carriers 时，模型生成过 `SELECT 1`，护栏放行、界面显示「1」，
    # 而真值是 13 —— 一个凭空捏造的数字，页面上看不出任何异常。
    #
    # 判据是**有没有表引用**，不是"有没有白名单内的表"：CTE 遮蔽同名真实表时
    # referenced 会刻意排掉那个名字（见 R-03），拿它判就会误杀合法的 CTE 查询。
    #
    # 位置在 R-07 之后：`SELECT pg_read_file('/etc/passwd')` 同样一张表都不引用，
    # 但它是一次读文件尝试，必须归因到 R-07。归因错了，安全告警就查错方向。
    if not any(True for _ in root.find_all(exp.Table)):
        return GuardResult(
            ok=False, rejected_by="R-23",
            reason="这条 SQL 没有引用任何表，结果不来自库里的数据",
        )

    # ---------- R-04 字段真实性 ----------
    # P0 覆盖：带表限定的字段，以及作用域内只有一张表时的裸字段。
    # 多表 JOIN 下的裸字段留待 P1 用 sqlglot.qualify 做完整解析。
    err = _check_columns(root, cfg, ctes)
    if err:
        return GuardResult(ok=False, rejected_by="R-04", reason=err)

    # ---------- R-24 相对时间锚点 ----------
    anchor = _time_anchor(root, cfg, question, dialect)
    if anchor.rejected:
        return GuardResult(ok=False, rejected_by="R-24", reason=anchor.reason)
    if anchor.note:
        fired.append("R-24")
        notes.append(anchor.note)

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

    # ---------- R-22 枚举取值大小写归一（改写而非阻断）----------
    hit = _normalize_enums(root, cfg, ctes)
    if hit:
        fired.append("R-22")
        rewrites.append("枚举取值按库中声明归一：" + "、".join(hit))

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
        notes=notes,
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


def _enum_scope(select: exp.Select, cfg: Config, ctes: set[str]) -> dict[str, str]:
    """本作用域内 别名/表名 -> 真实表名。与 R-04 的解析口径逐条对齐。"""
    scope: dict[str, str] = {}
    for t, _join in _direct_tables(select):
        n = (t.name or "").lower()
        if n in ctes or n not in cfg.tables:
            continue
        scope[t.alias_or_name.lower()] = n
        scope[n] = n
    return scope


def _column_enum(col: exp.Column, scope: dict[str, str], cfg: Config,
                 ctes: set[str]) -> list[str]:
    """这个列引用如果指向一个有声明取值的列，返回那份取值，否则空。"""
    cname = (col.name or "").lower()
    if not cname:
        return []
    qualifier = (col.table or "").lower()
    if qualifier:
        if qualifier in ctes:
            return []
        tbl = scope.get(qualifier)
    else:
        real = set(scope.values())
        tbl = next(iter(real)) if len(real) == 1 else None
    if not tbl:
        return []
    c = cfg.tables[tbl].columns.get(cname)
    return list(c.enum) if c and c.enum else []


def _normalize_enums(root: exp.Expression, cfg: Config, ctes: set[str]) -> list[str]:
    """R-22：枚举列的等值比较，按库里声明的取值把字面量的大小写改回来。

    这修的是一类**语法完全正确、结果恒为空**的错，也是最难被发现的一类：
    实测模型写 `parse_status = 'failed'`（库里是 `'FAILED'`），解析失败率于是
    报 0%，读的人得到「链路完全健康」的结论，而真值是 4.02%。报错会被看见，
    这种错不会。

    只在**忽略大小写能对上某个声明取值**时改写，且只改字面量本身：
      · 对不上任何取值 —— 不动。取值清单可能来自统计抽样，并不保证完备，
        据此拒绝会误杀合法查询。
      · 大小写已经对上 —— 不动，也不记规则，避免每条 SQL 都报一次改写。
    """
    changed: list[str] = []
    for select in root.find_all(exp.Select):
        scope = _enum_scope(select, cfg, ctes)
        if not scope:
            continue
        for node in select.find_all(exp.EQ, exp.NEQ, exp.In):
            if isinstance(node, exp.In):
                col = node.this
                literals = list(node.expressions)
            else:
                col, other = node.this, node.expression
                if not isinstance(col, exp.Column) and isinstance(other, exp.Column):
                    col, other = other, col
                literals = [other]
            if not isinstance(col, exp.Column):
                continue
            allowed = _column_enum(col, scope, cfg, ctes)
            if not allowed:
                continue
            folded = {str(v).lower(): str(v) for v in allowed}
            for lit in literals:
                if not isinstance(lit, exp.Literal) or not lit.is_string:
                    continue
                val = str(lit.this)
                if val in allowed:
                    continue
                want = folded.get(val.lower())
                if want is None or want == val:
                    continue
                lit.set("this", want)
                changed.append(f"{col.sql()} '{val}' → '{want}'")
    return changed


# ---------------------------------------------------------------------------
# R-24 相对时间锚点
#
# 2026-09-09 的十二源回归里，"昨天的 GMV 是多少"两次问出两个不同的错答案：
#   · 一次把「昨天」解释成 `MAX(stat_date)`，返回**前天**的数，列名还叫"昨日GMV"
#   · 一次是模型凭空写下 `shipped_at >= '2025-07-01'` 并称之为"最近一个月"
#     （真实数据到 2026-09，差了一年零两个月）
# 两条的共同点是**模型不知道今天是几号**：提示词从不告诉它当前日期，于是它
# 要么拿库里最新那天顶替，要么编一个看起来合理的字面量。llm.SYSTEM 现在会
# 把当前日期喂进去；这条规则是它的确定性兜底 —— 提示词是软的，护栏是硬的。
#
# 分两档，因为两类错的确定性不同：
#   · MAX(时间列) 冒充"今天/昨天" —— **确定错**，拦下重试。库里最新有数据的
#     那天不是昨天，这个等式在任何数据集上都不成立。
#   · 全是写死的日期字面量 —— **可能对**（"今年8月"解析成 2026-08 就是对的），
#     所以只记一句提醒，不拦。拦下去会误杀一大批合法查询。

#: 会把答案锚到"此刻"的时间词。刻意不收「最近一次」「最近的」这类 ——
#: 那是"排序取头一条"的意思，与当前日期无关，收进来就是误报。
_REL_TIME = re.compile(
    r"今天|今日|当天|昨天|昨日|前天|明天|本周|这周|上周|本月|这个月|上个月|上月"
    r"|本季度|今年|去年|前年|年初至今|至今|迄今"
    r"|(?:最近|近|过去|过往)\s*(?:\d+|一|两|三|四|五|六|七|八|九|十|半)\s*"
    r"(?:天|日|周|礼拜|个?月|季度|年)"
)

#: 问题里提到的**具体**时间。有它就说明区间是用户自己划的，写死日期理所当然。
_ABS_TIME = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*[号日]|\d{4}-\d{1,2}"
    r"|[Qq][1-4]|季度|上半年|下半年|全年")

#: 取当前时间的函数。写全是因为漏一个就等于放过一整类正确写法，
#: 而 R-24 的软提醒一旦误报，读的人下次就不看了。
_NOW_FUNCS = (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime)
_NOW_NAMES = frozenset({"now", "today", "current_date", "current_timestamp",
                        "localtimestamp", "localtime", "getdate", "sysdate",
                        "statement_timestamp", "transaction_timestamp",
                        "clock_timestamp"})


#: 「昨天」在各方言里的写法。这句话是**回灌给模型的**，给一个在当前库上
#: 跑不通的示例（MySQL 不认 INTERVAL '1 day' 那种带引号的写法），
#: 只会让它下一轮改成写死日期 —— 而写死日期正是这条规则要拦的东西。
#: 与 llm.INTERVAL_EXAMPLE 是同一件事的两处落点：那边是首轮提示词，
#: 这边是被拦下之后的纠正话术，**改一处必须改另一处**。
_INTERVAL_EXAMPLE = {
    "mysql": "CURDATE() - INTERVAL 1 DAY",
    "postgres": "CURRENT_DATE - INTERVAL '1 day'",
    "duckdb": "CURRENT_DATE - INTERVAL '1 day'",
}


@dataclass
class _Anchor:
    rejected: bool = False
    reason: str = ""
    note: str = ""


def _uses_now(root: exp.Expression) -> bool:
    if any(True for _ in root.find_all(*_NOW_FUNCS)):
        return True
    for fn in root.find_all(exp.Anonymous, exp.Func):
        name = (getattr(fn, "name", "") or fn.sql_name() if hasattr(fn, "sql_name")
                else getattr(fn, "name", ""))
        if str(name).lower() in _NOW_NAMES:
            return True
    return False


def _is_time_column(cfg: Config, name: str) -> bool:
    """库里有没有一列叫这个名字、且它是时间列。

    不解析列归属：R-24 只需要知道"这个 MAX 是不是套在时间列上"，
    而同名列在不同表里是不是时间列，这个库里没有反例。
    """
    n = (name or "").lower()
    for t in cfg.tables.values():
        c = t.columns.get(n)
        if c is None:
            continue
        if c.time or any(k in (c.type or "").lower() for k in ("date", "time")):
            return True
    return False


def _date_literals(root: exp.Expression) -> list[str]:
    return [e.name for e in root.find_all(exp.Literal)
            if e.is_string and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?.*", e.name or "")]


def _time_anchor(root: exp.Expression, cfg: Config, question: str,
                 dialect: str = "duckdb") -> _Anchor:
    if not question:
        return _Anchor()                     # 直查模式：没有问题文本可比对
    rel = _REL_TIME.search(question)
    if not rel:
        # 问题里**一个时间都没提**，SQL 却把结果限定在某个区间 —— 那个区间
        # 是模型自己加的。R-11 拦下后回灌"缩小范围"，模型照做加一个时间窗，
        # 第二轮通过，最终 rejected_by 是 null：页面与全量结果毫无区别。
        # scope_narrowed 只在"被 R-11 拦过"这条路径上留痕，而模型第一轮就
        # 自作主张的那种它接不住 —— 这里补上。
        if _ABS_TIME.search(question):
            return _Anchor()                 # 用户自己就问了某年某月，写死日期天经地义
        lits = _date_literals(root)
        if lits:
            return _Anchor(note=(
                f"你没有指定时间范围，这条查询却把结果限定在了 "
                f"{'、'.join(sorted(set(lits))[:3])} 一带。"
                "请确认这是不是你要的口径 —— 全量与某个区间的数字可能差很多。"))
        return _Anchor()
    if _uses_now(root):
        return _Anchor()                     # 用了当前时间函数，锚点是对的

    # 档一：拿"库里最新那天"冒充当前时间。只认落在子查询里的 MAX ——
    # 裸的 `SELECT MAX(stat_date)` 是在问"数据到哪天"，那是合法问题。
    for sub in root.find_all(exp.Subquery):
        for m in sub.find_all(exp.Max):
            col = m.find(exp.Column)
            if col is not None and _is_time_column(cfg, col.name):
                return _Anchor(
                    rejected=True,
                    reason=(f"问题问的是相对时间（{_REL_TIME.search(question).group()}），"
                            f"SQL 却用 MAX({col.name}) 当作时间锚点。"
                            "库里最新有数据的那一天不等于今天/昨天 —— 这样算出来的数"
                            "会被当成用户问的那一天。请改用当前时间函数"
                            f"（如 {_INTERVAL_EXAMPLE.get(dialect, _INTERVAL_EXAMPLE['duckdb'])}）"
                            "表达相对时间；"
                            "若那一天确实没有数据，如实返回空结果，不要顺延到别的日子。"),
                )

    # 档二：全是写死的日期。可能对也可能错，只提醒。
    lits = _date_literals(root)
    if lits:
        return _Anchor(note=(
            f"问题里的时间是相对的（{_REL_TIME.search(question).group()}），"
            f"SQL 里却是写死的日期（{'、'.join(sorted(set(lits))[:3])}）。"
            "请核对这个区间是不是你要的那一段。"))
    return _Anchor()


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


# ---------------------------------------------------------------------------
# 2026-09-10 生产跑测补的分析函数。都只读 AST、不改写，供 graph 判定用。
# ---------------------------------------------------------------------------

def _predicates(sql: str, dialect: str) -> set[str]:
    """SQL 里所有**带字面量的比较谓词**，规范化成文本用于做差集。

    只收带字面量的：`a.id = b.kb_id` 这种连接条件不是过滤，收进来会让
    "加了哪些过滤条件"这件事被 JOIN 噪声淹掉。
    """
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except Exception:
        return set()
    out: set[str] = set()
    kinds = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
             exp.Like, exp.ILike, exp.In, exp.Between)
    for root in stmts:
        for node in root.find_all(*kinds):
            if not list(node.find_all(exp.Literal)):
                continue
            try:
                out.add(re.sub(r"\s+", " ", node.sql(dialect=dialect)).strip().lower())
            except Exception:
                continue
    return out


def _literals_of(pred: str) -> list[str]:
    """谓词里出现的字面量值（去引号）。用来判断这个过滤条件是不是用户提的。"""
    return [m.strip("'\"") for m in re.findall(r"'[^']*'|\"[^\"]*\"|\b\d[\d.]*\b", pred)]


def added_filters(blocked_sql: str, new_sql: str, dialect: str = "duckdb") -> list[str]:
    """重试版比被拦版**多出来的**过滤条件。

    R-11 把一条查询拦下之后，模型有两条路可走：换成预聚合汇总表（换 FROM、
    不加过滤 —— 这条路是我们希望它走的），或者随手加一个过滤条件把扫描量压下去。
    走第二条路时，跑出来的数不是用户问的那个范围，而链路上每一步都是绿的。
    """
    return sorted(_predicates(new_sql, dialect) - _predicates(blocked_sql, dialect))


#: 问句里表示"我给了时间范围"的说法。用户自己划了时间窗时，模型沿**同一个
#: 维度**把范围收得更紧，仍然是在回答他问的那件事 —— 那种收窄可以放行（留痕即可）。
#: 凭空补一个用户没提过的时间窗则不行，两者的区别全在这张表上。
_TIME_WORDS = (
    "今天", "昨天", "前天", "今日", "昨日", "本周", "上周", "本月", "上月", "上个月",
    "本季", "季度", "今年", "去年", "最近", "近期", "近一", "近三", "近七", "近30",
    "以来", "至今", "期间", "年", "月", "日", "号", "周", "天",
)


def _predicate_column(pred: str) -> str:
    """谓词左侧那一列的列名（去掉表别名前缀）。取不到返回空串。"""
    m = re.match(r"\s*(?:\w+\.)?(\w+)", pred or "")
    return (m.group(1) if m else "").lower()


def _time_columns(cfg: Config) -> set[str]:
    return {c.name.lower()
            for t in cfg.tables.values() for c in t.columns.values() if c.time}


def arbitrary_narrowing(blocked_sql: str, new_sql: str, question: str,
                        cfg: Config | None = None,
                        dialect: str = "duckdb") -> list[str]:
    """这些新加的过滤条件，用户**根本没提过**。

    判据很朴素：谓词里的字面量在问句里找不到，就说明这个范围是模型自己挑的。
    2026-09-10 实测最典型的一条 —— 问「一共有多少个分块」，扫描超阈值后模型重试成

        SELECT COUNT(c.id) FROM document_chunks AS c WHERE c.kb_id = 1

    `1` 是随手挑的一个知识库，而那个库恰好没有分块。屏幕上于是出现一个大大的
    「0」，旁边配着一句"此结果只是单个知识库的分块数"的说明。说明是对的，
    但没人会拿「0」当"我没答上来"读。**告知不能替代拒答**：覆盖不了用户问的
    那个范围时，正确动作是不给数。

    凭空补的时间窗同样算随手挑的：用户问全量、模型自己塞一个"最近 30 天"，
    答的就不是他问的那件事。但**用户自己划了时间范围**时（"2026 年 8 月的问答量"），
    模型沿同一个时间维度把范围收紧仍然是在回答他问的那件事 —— 那种放行，留痕即可。
    这条区分靠 cfg 里标了 `time: true` 的列 + 问句里的时间说法来判；
    不传 cfg 时退回只看字面量，偏严。
    """
    q = (question or "").lower()
    tcols = _time_columns(cfg) if cfg is not None else set()
    q_has_time = any(w in q for w in _TIME_WORDS)
    out: list[str] = []
    for pred in added_filters(blocked_sql, new_sql, dialect):
        lits = _literals_of(pred)
        if not lits:
            continue
        # 只要有一个字面量是用户提过的，就认为这个过滤条件源自问题本身
        if any(l and l.lower() in q for l in lits):
            continue
        # 用户给了时间范围，模型沿时间列收窄 —— 忠实于问题，放行
        if q_has_time and _predicate_column(pred) in tcols:
            continue
        out.append(pred)
    return out


def derived_columns_used(sql: str, cfg: Config, dialect: str = "duckdb") -> list[str]:
    """这条 SQL 用到的**缓存/派生计数列**。

    这类列（knowledge_bases.doc_count 之类）是给列表页做排序用的计数器，
    会与真实计数漂移。2026-09-10 实测同一天里 doc_count 比 COUNT(documents)
    少 362 篇，两种问法都答得理直气壮、都是 100 分。

    它不该被一律禁用（列表页排序就该用它），但用到了必须让读的人知道 ——
    这里只做识别，扣分与提示交给上层。
    """
    flagged = {c.name.lower()
               for t in cfg.tables.values() for c in t.columns.values()
               if getattr(c, "cached_counter", False)}
    if not flagged:
        return []
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except Exception:
        return []
    hit: set[str] = set()
    for root in stmts:
        for col in root.find_all(exp.Column):
            if (col.name or "").lower() in flagged:
                hit.add(col.name.lower())
    return sorted(hit)
