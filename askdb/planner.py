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
    """R-15：中间结果规模上限。

    下传的值会作为字面量拼进下一条 SQL。列表过长会让提示词膨胀、
    SQL 长度失控，而且往往说明第一步的筛选本身就有问题。
    """
    cap = int(cfg.raw.get("planner", {}).get("max_carry_rows", 50))
    for k, v in carry.items():
        if len(v) > cap:
            return False, f"中间结果 {k} 有 {len(v)} 项，超过上限 {cap}"
    return True, ""


def preview_rows(rows: list[list[Any]], n: int = 5) -> list[list[Any]]:
    """结果摘要 —— 只回灌前几行，不把整个结果集塞进提示词。"""
    return [list(r) for r in rows[:n]]
