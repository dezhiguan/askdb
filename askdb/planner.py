"""多步查询规划（技术设计说明书 §5.3）。

**首要原则：能用一条 SQL 表达的依赖，一律交给 SQL，不走多步。**
大量看似"多跳"的问题用 CTE 或窗口函数单条即可完成，且更快、更便宜、更准。
多步是兜底路径，不是默认路径 —— 滥用会同时抬高成本、延迟与错误率。

只有三类情形才判定需要多步：
  探索型         —— 要先看数据的实际取值分布，才能确定筛选条件
  结果驱动分支   —— 第一步的结果决定第二步查哪张表或用哪套口径
  超出 SQL 表达  —— 中间需要 LLM 做语义判断，SQL 无法表达
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .config import Config


class Plan(BaseModel):
    """规划节点的结构化产出。"""

    multi_step: bool = Field(
        description="是否需要多步。能用一条 SQL（含 CTE、窗口函数）表达的一律填 false。"
    )
    reason: str = Field(default="", description="一句话说明判断依据。")
    goal: str = Field(
        default="", description="本步要查什么。多步时只描述当前这一步，不要描述全部。"
    )


PLAN_SYSTEM = """你是一个数据查询的规划者。判断用户的问题需要几步 SQL 才能回答。

**默认答案是单步。** 只有下面三种情形才需要多步：
1. 探索型：必须先看到数据的实际取值分布，才能确定筛选条件。
   例："看看处理状态都有哪些取值，再统计非正常状态的分布"
2. 结果驱动分支：第一步的结果决定第二步查哪张表或用哪套口径。
   例："最近异常的是哪个模块，把它对应的明细拉出来"
3. 超出 SQL 表达能力：中间需要语义判断，SQL 写不出来。
   例："找出命名不规范的知识库，统计它们的文档量"

**反例（这些都是单步）：**
- "哪个知识库失败率最高，它的失败文档是什么类型" —— 用 CTE 一条搞定
- "各城市订单量和客单价前 5" —— 单条聚合
- "对比本月和上月的成本" —— 单条聚合加条件分支
表面有多个子问题不代表需要多步；只要依赖关系能用 SQL 表达，就是单步。

多步的代价是成本翻倍、延迟上升、出错概率增加。拿不准时选单步。"""

PLAN_USER = """{schema}

【用户问题】
{question}"""

REPLAN_SYSTEM = """你在推进一个多步数据查询。根据已完成步骤的结果，决定下一步查什么。

只描述**下一步**要查的内容，不要重复已经查到的东西。
如果已有结果已经足以回答用户的问题，把 multi_step 设为 false 并把 goal 留空。"""

REPLAN_USER = """{schema}

【用户问题】
{question}

【已完成的步骤】
{history}

【可直接引用的中间结果】
{carry}"""


class Assessment(BaseModel):
    """结果评估节点的结构化产出。"""

    enough: bool = Field(description="已有结果是否足以回答用户的问题。")
    reason: str = Field(default="", description="一句话说明。")
    next_goal: str = Field(
        default="",
        description=(
            "enough=false 时必填：下一步要查什么，一句话。"
            "判定「还不够」的人最清楚缺的是什么，这个目标由你给出，"
            "重规划节点据此展开为具体查询。enough=true 时留空。"
        ),
    )
    carry: dict[str, list] = Field(
        default_factory=dict,
        description=(
            "需要下传给下一步的标识列取值，形如 {\"kb_ids\": [12, 7]}。"
            "只放标识列，不要下传整行；不需要时留空。"
        ),
    )


ASSESS_SYSTEM = """你在评估一次数据查询的中间结果。

判断两件事：
1. 已有结果是否足以回答用户的原始问题；
2. 若不够，下一步需要引用哪些标识值（如 id 列表）。

**只下传标识列**（id / 编码），不要下传整行数据 —— 下传的值会作为字面量
拼进下一条 SQL，行数过多会让 SQL 长度失控。"""

ASSESS_USER = """【用户问题】
{question}

【本步目标】
{goal}

【本步执行的 SQL】
{sql}

【本步结果】共 {n} 行，前若干行：
{rows}"""


@dataclass
class SubStep:
    """一个已完成步骤的摘要。进状态、进检查点，因此必须可序列化。"""

    index: int
    goal: str
    sql: str
    row_count: int
    preview: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"第 {self.index} 步：{self.goal}\n  SQL：{' '.join(self.sql.split())}"
        head += f"\n  结果：{self.row_count} 行"
        if self.preview:
            cols = "、".join(self.columns)
            rows = "；".join(", ".join(str(v) for v in r) for r in self.preview)
            head += f"（{cols}）{rows}"
        return head


def render_history(steps: list[dict[str, Any]]) -> str:
    if not steps:
        return "（无）"
    return "\n".join(SubStep(**s).render() for s in steps)


def render_carry(carry: dict[str, list]) -> str:
    if not carry:
        return "（无）"
    return "\n".join(f"{k} = {v}" for k, v in carry.items())


def carry_within_limit(carry: dict[str, list], cfg: Config) -> tuple[bool, str]:
    """R-15：中间结果规模上限，外加 carry_columns_only 的形态校验。

    下传的值会作为字面量拼进下一条 SQL。列表过长会让提示词膨胀、
    SQL 长度失控，而且往往说明第一步的筛选本身就有问题。
    """
    pl = cfg.raw.get("planner", {})
    cap = int(pl.get("max_carry_rows", 50))
    cols_only = bool(pl.get("carry_columns_only", True))

    for k, v in carry.items():
        if len(v) > cap:
            return False, f"中间结果 {k} 有 {len(v)} 项，超过上限 {cap}"
        if cols_only:
            # §5.3.2「仅下传标识列，不下传整行」。模型很容易把整行塞进 carry，
            # 那既会泄露非必要字段（§10.1 多步累积泄露），也会撑爆下一步的 SQL。
            bad = next((x for x in v if isinstance(x, (list, dict, tuple))), None)
            if bad is not None:
                return False, f"中间结果 {k} 下传了整行而非标识列（carry_columns_only）"
    return True, ""


#: 回灌进提示词的结果预览上限，按**渲染后的字符数**算，不是行数。
#:
#: 原来是写死的"前 5 行"。拿行数当预算单位两头不讨好：5 行窄表（一个渠道名 +
#: 一个整数）根本省不下什么，5 行宽表照样能撑爆预算 —— 真正该封顶的量没被度量。
#: 更要命的是它对小结果过度节流：trace 26096703989b 里一个 9 行 2 列的结果，
#: 全给进提示词也就 ~150 字符，为省这点，模型看不到后 4 行，先把总数编成
#: 1,027,010（真值 1,122,911）被接地校验拦下，又空转一轮反思重跑了一条逐字节
#: 相同的 SQL —— 白烧 9,096 token / 7.9 秒，占那次查询总量的 43% 与 35%。
#:
#: 放宽是安全的：结果集本身已由 R-13（guard.max_rows，生产配的是 200）封顶，
#: 这一层是在一个已经有界的东西上再砍一刀，这里还有字符预算兜底。
PREVIEW_CHARS = 1600


def preview_rows(rows: list[list[Any]],
                 budget: int = PREVIEW_CHARS) -> list[list[Any]]:
    """结果摘要 —— 在字符预算内尽量多给行，**给得下就全给**。

    "能不能全给"本身是一个会传到模型那里的信号：agent._render_history 在
    预览行数等于 row_count 时，会把提示词里那句"仅前 N 行（其余未展示，不得
    据此断言整列的分布）"换成"全部行"。所以只要这里把行给全，模型就没有理由
    再写"结果被截断，其余渠道不在此列出"—— trace 26096703989b 的自相矛盾正是
    这么来的（结果表 9 行齐全，紧挨着的答案却说列不出来）。

    **至少给一行**：单行就超预算时宁可超。给出一个零行的预览，模型会读成
    "这次查询没有返回数据"，那是比超预算坏得多的错。
    """
    out: list[list[Any]] = []
    used = 0
    for r in rows:
        row = list(r)
        # 与 _render_history 的渲染方式对齐（", " 连列、"；" 连行）。两边各按
        # 一套算，这个预算就是虚的。
        cost = len(", ".join(str(v) for v in row)) + 1
        if out and used + cost > budget:
            break
        out.append(row)
        used += cost
    return out
