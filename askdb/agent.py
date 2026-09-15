"""可信数据 Agent —— LLM 自主决策循环（v2 设计 §1）。

与固定管道（graph.ask）并存的**另一条链路**，由配置 agent.enabled 门控。区别只在
"编排"：这里 LLM 每轮自己决定"下一步用哪个工具、传什么参数"，而不是走死的
schema_recall→generate→execute。安全性不变 —— 每次 execute_sql 仍是 tools.py 里那个
过 guard/dry_run/只读/脱敏 的安全原子，LLM 决定发哪条 SQL、绝不决定它能不能执行。

一次查询：
  1) grounded 召回      先 search_schema，让后面的判断有 schema 可依
  2) 意图 / 可答性预检   便宜一次结构化调用：可答？越域？单步/多步？（接地，不盲判）
  3) 自主循环          决策(选工具) → 调用 → 观察结果 → 再决策 …… 直到 finish
                       受 R-16 步数上限、R-17 累计 token 上限封顶，超则收敛作答并标注
  4) 收尾             用最后一次成功 execute_sql 的结果 + 归因结论打包成 AskResult

预算、脱敏、租户、可信信号都沿用既有实现：execute_sql 已把行脱敏、按 org 过滤，
循环全程只看得到过闸后的数据（挡设计 §10.1 的多步累积泄露）。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field

from . import grounding, planner, skill, tools
from .audit import PHASE_STARTED
from .config import Config
from .executor import Executor
from .graph import AskResult, _audit_of
from .llm import LlmClient, LlmUsage
from .quota import QuotaExceeded, build_quota
from .trace import Tracer, now_iso, write_audit


# --------------------------------------------------------------------------
# 结构化产出
# --------------------------------------------------------------------------
class IntentCheck(BaseModel):
    """意图 / 可答性预检的结构化产出（接地：已看过召回的 schema）。"""

    answerable: bool = Field(description="用当前可用的表能不能回答这个问题")
    out_of_scope: bool = Field(
        default=False,
        description="问题涉及的业务实体在库里根本不存在（如问『供应商』但无供应商表）。"
                    "严禁攀附同名列硬答 —— 这种情况填 true。")
    clarify: str = Field(default="", description="answerable=false 且非越域时，说明缺什么、要澄清什么")
    multi_step: bool = Field(default=False, description="是否需要多步（先探分布/结果驱动分支）")
    metadata_only: bool = Field(
        default=False,
        description="问的是**库/表/字段本身**（有哪些表、某张表有哪些字段、这个库能查什么），"
                    "而不是表里的数据 → true。"
                    "只要答案需要读任何一行业务数据（计数、求和、分组、列举记录），"
                    "哪怕问法里带着表名，也是 false。")
    reason: str = Field(default="", description="一句话判断依据")


class AgentAction(BaseModel):
    """自主循环每一轮的决策。"""

    thought: str = Field(default="", description="一句话：这一步为什么这么做")
    finish: bool = Field(default=False, description="证据已足以回答 → true；否则 false 并给出下一步工具")
    answer: str = Field(default="", description="finish=true 时的最终结论（含口径与归因）")
    tool: str = Field(default="", description="finish=false 时选的工具名")
    args: dict[str, Any] = Field(default_factory=dict, description="该工具的参数")
    answer_step: int = Field(
        default=0,
        description="finish=true 时，结论依据的是**第几步**的执行结果 —— 填"
                    "【已完成的工具调用与结果】里的那个序号。界面据此渲染结果表。"
                    "0 = 最后一次执行。多步链路里最后一次往往是探查或核对，"
                    "不是回答问题的那一条，所以这一位要填准。")


INTENT_SYSTEM = """你是数据查询的意图预检。已给你「可用的表与业务口径」，据此判断四件事：
1. answerable：用这些表能不能回答用户问题；
2. out_of_scope：问题里的业务实体是否在库中根本不存在——若不存在，**必须**判 true，
   严禁把它攀附到某个名字相近的列上硬answer（例如库里没有「供应商」实体，就不能拿
   model_config.vendor 之类同名列冒充）。
   判据是**有没有承载这个实体的表**，不是"有没有名字像的列"：一个属性列
   （vendor / type / category / source / ref_type）哪怕名字完全对上，也不等于
   那个实体存在。实测反复出现的错法是——先在口径里写明"表中没有独立的 X 实体表"，
   然后照样拿同名列数出一个数交差；**写得出这句话就说明该判 true**。
   同一个问题换个问法（"我们有多少 X"／"X 的数量是多少"／"统计一下 X 总数"）
   判定必须一致，不能因为措辞不同就一会儿拒答一会儿攀附；
3. multi_step：是否需要多步（先看取值分布、或第一步结果决定第二步查哪张表）；
4. metadata_only：问的是**库/表/字段本身**（"有哪些表""某张表有哪些字段""这个库能查什么"），
   还是表里的**数据**。只要答案需要读任何一行业务数据——计数、求和、分组、列举记录——
   就是 false，哪怕问法里写着表名（"documents 表一共有多少行"问的是数据，不是元数据）。
   这一位决定后面的循环要不要把 schema 检索工具摆上桌，判宽了只是多花一轮，
   判窄了会让元数据问题无工具可用，所以**拿不准就填 true**。
可答就 answerable=true；缺查询对象（纯指代、没主语）answerable=false 并在 clarify 写清缺什么。"""

INTENT_USER = """{schema}

【用户问题】
{question}"""

AGENT_SYSTEM = """你是一个可信查数 Agent。你不能直接写库，只能通过工具查数据。

可用工具（只读，可任意多次组合）：
{tools}

规则：
- 列名、类型、枚举取值一律以下方【可用的表】为准 —— 那份是本次召回的**完整原值**，
  **不要猜**，也**不要为了"确认一下"再查一遍它已经写明的东西**。只有下方压根没有
  列出那张表时，才用 get_table_schema。
- 口径要遵循「可用的表与业务口径」里的说明（例如某列的口径注释、枚举取值）。
- execute_sql 只写只读 SQL（SELECT/CTE）；它会自动过护栏、干跑、只读执行、脱敏。
- 一步只做一件事。看到工具结果后再决定下一步。
- 证据已经足以回答用户问题时，finish=true 并在 answer 写出结论——**结论要说明口径**，
  并且只基于工具真正返回的数据，不得编造。
- **finish=true 时必须填 answer_step**：结论依据的是第几步的执行结果（【已完成的
  工具调用与结果】里的序号）。界面按它渲染结果表；不填就默认取最后一次执行，
  而多步链路里最后一次往往是探查或核对，不是回答问题的那一条。
- 若发现问题无法用现有表回答，也 finish=true，在 answer 如实说明无法回答及原因。{answer_style}

结论里的数字，以下五条是硬约束（违反即为错答，且事后可核）：
1. **没跑过就不许写。** 结论里出现的每一个数字，都必须是某次 execute_sql 真正返回过的值，
   或由这些值直接算得。一次 execute_sql 都没成功时，不许写任何数字，也不许说"经查证"
   "已用 DISTINCT 核实""三张表互相印证"这类话——没查就是没查。
2. **结果集行数 ≠ 表的行数。** 一条带 WHERE / LIMIT 的 SQL 返回 N 行，只说明"符合这些条件的
   有 N 行"。要陈述某张表一共多少行，{rule2_tail}
3. **被截断的结果不能用来说总量。** 工具结果标注"已被 R-13 截断"时，那不是全部行；
   此时禁止给出总数、总和、覆盖范围一类的总量性表述，只能描述已看到的部分并说明它被截断了。
4. **列要按它在 SQL 里的真实含义命名。** 你写的是 COUNT(*) 就不能在结论里把它叫成
   "已排除软删除的条数"；写的是 COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) 就要叫
   "已删除数"。要"排除软删除后的条数"，就在 SQL 里真的写 WHERE deleted_at IS NULL。
5. **比率、百分比、差值一律在 SQL 里算成一列，不要在结论里心算。** 需要 a/b 就写
   ROUND(100.0 * a / NULLIF(b, 0), 4) AS xxx_pct，让库算完再读。

6. **看到的只是前几行，不代表整列都长这样。** 工具标注"仅前 N 行"时，禁止据此断言
   "全部都是……""共有 N 家"。要判断整列的分布或总量，{rule6_tail}
7. **合计也别自己加。** 需要总数就在同一条 SQL 里多选一列 COUNT(*) / SUM(...)，
   或单独再查一次。手动把分项加起来实测会加错（把五个正确的分项加成了
   1,008,010，真值 1,027,010），而分项全对会让这个错的合计显得很可信。

8. **一个实体一行，不要把多行拼成一格。** 问"每个 X 的 Y"就 GROUP BY X 正常返回
   N 行，禁止用 string_agg / group_concat / listagg / 手工 CONCAT 把 N 个实体
   压成一行一列的长串（`WECHAT: total=460323 | ALIPAY: total=415330 | …`）。
   三个代价都是实打实的：
   · **行数护栏按行计**。拼成一格后九个渠道算 1 行，R-13 的行数上限形同虚设 ——
     一格里能塞进任意多实体。
   · **脱敏按投影里的列做**。一格里只要混进一个敏感列，整格会被一起打成星号，
     连同其它实体的所有数字一起毁掉（executor._mask 对一个投影取 any）。
   · 结果表退化成 1 行 1 列，没法按列排序、比较、导出，读的人只能盯着一条长串
     自己数。
   要一行一个汇总串的场合（例如"把这些标签列出来"）才用聚合拼接，且只在
   GROUP BY 的组内拼，不要跨整个结果集拼。
{rule9}
另外：表名、列名、枚举取值一律**逐字照抄**工具返回的原值，不得改写成同义词或翻译
（例如返回的是 ANSWER 就不能写成 CHAT）；需要解释时在括号里补中文说明。"""

#: 硬约束第 2、6、9 三条的两套写法 —— 由 agent.sql_consolidation 选。
#:
#: **legacy 那套不是"旧代码"，是回滚位。** 第 2、6 条原话里的"必须单独跑一次
#: COUNT""另跑一条 GROUP BY"把**核对**钉死成了**额外一轮**：生产 trace
#: 4bac5ce7f21b 第 6-9 步两轮自证口径就是这么来的 —— 第 5 步的返回里每家都带着
#: month_cnt=20 与首尾月份，"360 行 = 18 家 × 20 月、无重复无缺失"当时已经成立，
#: 模型仍然照规矩各跑了一轮去"确认"，代价 5,089ms + 10,863 输入 token。
#:
#: 护栏一条都没放松：要陈述总量仍然必须有一条不带过滤的 COUNT 作为依据，
#: 只是不再要求它单独占一轮 —— 写进同一条 SQL 的一列同样成立，而且更省。
#:
#: 这是**改模型行为的概率分布**，不是判定改动，没有影子档可言（同一次调用没法
#: 同时跑两套提示词）。旋钮就是它的标定手段与回滚位：先用 evals/baseline.py
#: 对照组跑一轮 off、一轮 on，比 avg_steps / p95_ms / R-11 拦截率 /
#: ungrounded 命中率，再定版。
_RULES = {
    True: {
        "rule2_tail":
            "必须有一条不带过滤的 COUNT 作为依据 ——\n"
            "   它可以是**同一条 SQL 里的一列**（COUNT(*) OVER ()，"
            "或单独选一列 COUNT(*)），不必单独占一轮。",
        "rule6_tail":
            '就在同一条 SQL 里多选一列把它算出来\n'
            '   （COUNT(DISTINCT x)、COUNT(*) FILTER (...)、MIN/MAX(...)），别拿可见的那几行外推。',
        "rule9": '''
9. **核对写进同一条 SQL，不要为它多跑一轮。** 需要交代总行数、粒度是否唯一、
   覆盖了哪些期次时，把它们作为额外列选进你本来就要跑的那条查询，一次拿全：
   COUNT(*) OVER () AS total_rows、COUNT(*) AS n、MIN(期次列)、MAX(期次列)、
   COUNT(DISTINCT a || '#' || b) —— 而不是先查一遍数据、再单独发一条去"确认"。
   · **上文工具结果里已经出现过的列，直接引用。** 每组已经返回了 n=20，就不必
     再跑一条 COUNT 去证明"每组都是 20"；那条 SQL 换回来的是你已经拿到的东西。
   · 想在结论里多给一列对比（最近一期、近 N 期均值、同比），一样写进同一条 SQL
     的 FILTER 子句，不要为此单独发一条。
   · 这一条**不放松第 1、2、3 条**：没跑过仍然不许写，总量仍然要有不带过滤的
     COUNT 作依据，被截断的结果仍然不能用来说总量。改的只是"怎么拿到依据"，
     不是"要不要依据"。
''',
    },
    False: {
        "rule2_tail": "必须单独跑一次不带过滤的 COUNT。",
        "rule6_tail": "另跑一条 GROUP BY /\n"
                      "   COUNT(*) FILTER，别拿可见的那几行外推。",
        "rule9": "",
    },
}


#: answer 要不要把结果集整表抄一遍 —— 由 agent.answer_no_table_dump 选。
#:
#: 生产 trace 4bac5ce7f21b 的收敛那一次输出 1,415 个 token，主体是一张
#: 18 行 × 6 列的 Markdown 表 —— 而这张表的数据就在 last_exec 里、界面本来就在
#: 渲染它。按实测解码速率（1,415 tok / 18.1s ≈ 78 tok/s）算，光这张表就是十几秒；
#: 按单价算是 ¥0.0038，占整条链路成本的 29.8%。而且 with_structured_output
#: 走的是 function_calling，Markdown 的换行全被转义成 \\n，还额外胀一成。
#:
#: **这条有一个前置依赖，必须同时成立**：界面渲染的得是"回答问题那一条"的结果，
#: 不是"最后执行"那一条。否则答案里不再有表、而界面上的表还是错的 —— 比改之前
#: 更坏。前置由 AgentAction.answer_step + agentgraph._answer_exec 解决，
#: 两者是一个整体，别只关其中一个。
_ANSWER_STYLE = {
    True: '''
- **answer 里不要把结果集整表抄一遍。** 完整结果表由界面直接渲染你指定的那一步的
  返回行，你再抄一遍既不会更准，也会让读的人看到两份。answer 只写三件事：
  ① 口径说明（数据来自哪张表哪一列、怎么算的、覆盖范围）；② 结论；
  ③ 要点 —— 最多列 Top/Bottom 各 3 条，点名时写名称与数值即可。
  结果超过 3 行时尤其如此。**这不是让你少说**：口径与结论要写足，省的只是那张
  界面已经有的表。''',
    False: "",
}


def answer_no_table_dump(cfg: Config) -> bool:
    """收敛轮要不要停止复述结果表。默认开。

    与 sql_consolidation 同理，这是改模型输出长度的概率分布、不是判定改动，
    旋钮既是标定手段也是回滚位。**关掉它之前先想清楚**：answer_step 那套
    结果区取数是独立的正确性修复，不受这个旋钮影响，关掉这里不会把它一起关掉。
    """
    return bool((cfg.raw.get("agent") or {}).get("answer_no_table_dump", True))


def intent_schema_heads(cfg: Config) -> bool:
    """意图预检喂表头层还是全量 schema。**默认 False（全量）—— 见下面第二段。**

    2026-09-15 本机标定（真模型 / 真 schema / 12 条探针 / 3 个源，全量与表头层
    各跑一遍）：判定正确 full 11/12 → heads 12/12，输入 token −51%。唯一一条翻转
    是「统计一下仓库总数」问 ragforge —— **全量判成可答（错），表头层判成越域
    （对）**。方向与 INTENT_SYSTEM 自己的判据一致：它要的是"有没有承载这个实体
    的表"，列名正是它被明确要求忽略的那类证据，摆上去只会喂给它攀附的材料。

    **但默认值是 False，因为 12 条探针选偏了。** 它们全在问"库里有没有这个
    实体"（越域），一条都没测"这些列答不答得了"。2026-09-15 的 23 条对照组
    跑测抓到了漏掉的那一面：b23「被隐藏的评价有多少条？」在 on 档 1/3 出数
    （两次 CLARIFY），off 档 3/3；单独探预检 10 次是 9/10 对 10/10。信号不强，
    但它落在一道**用户可见的拒答门**上，误判的后果是把一个答得了的问题
    回成"问得不够具体"。

    验收标准是那份 120 条生产拒答集回归（要生产登录态，本地跑不了）。
    **在它能跑之前，默认值站在"不改变现有行为"那一侧**：收益这条路是对的，
    只是证据还不够把默认值推过去。跑通那份回归就把配置改 true，
    代码与测试都在，不必重做。
    """
    return bool((cfg.raw.get("agent") or {}).get("intent_schema_heads", False))


def sql_consolidation(cfg: Config) -> bool:
    """核对列要不要并进同一条 SQL。默认开。

    默认开而不是默认关：关着等于把 4bac5ce7f21b 里那两轮自证口径原样留在生产上，
    而这条改动不碰任何判定层，最坏结果是模型少写一列自证、答案照样对。
    真出问题就把它关掉 —— 那是它存在的意义。
    """
    return bool((cfg.raw.get("agent") or {}).get("sql_consolidation", True))


def render_agent_system(cfg: Config, hide: frozenset[str] = frozenset()) -> str:
    """装配 decide 那一次的系统提示。

    **整段是跨请求可缓存的前缀**，所以这里只能填入随部署固定的东西
    （工具规格、旋钮档位），绝不能插入随请求变的值。
    """
    return AGENT_SYSTEM.format(tools=_render_specs(hide),
                               answer_style=_ANSWER_STYLE[answer_no_table_dump(cfg)],
                               **_RULES[sql_consolidation(cfg)])


#: 表头是**全局静态**的，而且排在 {schema} 之前 —— 它同时把跨请求可缓存的前缀
#: 从"系统提示"往后延了一段。改动它要留意这一点：插一个随请求变的值进来，
#: 就把这段前缀作废了。
#:
#: **这里仍然不写"不要再调 search_schema"** —— 那一轮确实是纯重复（线上 trace
#: 3f16cd49baec：3,318ms + 4,022 token + 一次 embedding 计费，换回同一批表；
#: 4bac5ce7f21b 又重演一次），但这份表头对**所有**问题生效，而元数据问题
#: （"这个库里有哪些表"）全靠模型自己调一次 search_schema 才能过 NO_EVIDENCE
#: 那道闸。在这里一刀切地劝它别调，等于把一个潜伏的误杀推成常态。
#:
#: 2026-09-15 起这一轮改由 agentgraph._hidden_tools 按问题类型收：数据问题把
#: search_schema 整个撤出规格表，元数据问题照旧留着。**收暴露面比劝措辞可靠**
#: —— 措辞是概率，规格表里没有这一行才是确定的。
AGENT_USER = """【本次 schema 召回已经完成，下面这份就是召回结果】
每张表的列名、类型、枚举取值都是**完整原值**，不是摘要，可以直接照它写 SQL。
因此：下面已经列出的表不要再调 get_table_schema —— 它返回的与下面逐字相同，
白跑一轮。下面**没有**列出的表才需要查。

{schema}

【用户问题】
{question}

【已完成的工具调用与结果】
{history}

【预算】剩余步数 {steps_left}。请给出下一步（选工具）或 finish=true 给出结论。"""


# --------------------------------------------------------------------------
def _io_json(obj: object) -> str:
    """把工具的参数/返回折成一段可读 JSON，进 span 的输入/输出。

    **取不到就退回 str()，绝不抛**：观测字段把主链路弄崩是最坏的结果。
    截断由 tracer.add 统一做（见 trace.clip_io），这里不重复一套上限。
    """
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(obj)


def _brief(res: tools.ToolResult) -> str:
    """一条工具结果的简短摘要，进 trace。"""
    if not res.ok:
        return f"{res.tool} 未通过：{res.rejected_by or ''} {res.error}".strip()
    d = res.data
    if res.tool == "search_schema":
        # 三条新链路各自留一句。**一张表是怎么进上下文的，排查时走的是三条
        # 完全不同的路**：语义召回进来的看排名、外键补进来的看关联图、靠
        # "这个值就在这张表里"进来的看值检索。只报一个"召回 N 张表"，
        # 线上再想分辨就只能去读提示词全文逐行猜。
        out = f"召回 {len(d.get('tables', []))} 张表"
        if d.get("blind"):
            out += "（盲选）"
        if d.get("fk_added"):
            out += f"；沿外键补入 {'、'.join(d['fk_added'])}"
        if d.get("value_hits"):
            out += f"；取值定位 {'，'.join(d['value_hits'])}"
        if d.get("coverage_gaps"):
            out += f"；未被覆盖的实体「{'、'.join(d['coverage_gaps'])}」"
        return out
    if res.tool == "get_table_schema":
        return f"{d.get('table')}：{len(d.get('columns', []))} 列"
    if res.tool == "execute_sql":
        s = f"返回 {d.get('row_count', 0)} 行"
        if d.get("truncated"):
            # 截断这件事必须在模型看得见的地方说清楚。只在响应里置 truncated=true
            # 而历史里只写"返回 200 行"，模型就会把 200 当成全量——2026-09-11 跑测
            # 里的「全库共 200 条评分记录」正是这么来的。
            s += "（**已被 R-13 截断，这不是全部行**，不得据此陈述总量）"
        if d.get("masked_columns"):
            s += "（有脱敏）"
        return s
    return "ok"


def _render_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "（无）"
    out = []
    for i, h in enumerate(history, 1):
        line = f"第 {i} 步 · {h['tool']}({_fmt_args(h['args'])}) → {h['brief']}"
        if h.get("preview"):
            cols = "、".join(h["preview"]["columns"])
            pv = h["preview"]["rows"]
            rows = "；".join(", ".join(str(v) for v in r) for r in pv)
            total = h["preview"].get("row_count")
            # 必须把"你只看到了几行、一共几行"写死在这里。只写"前几行"太轻，
            # 模型会拿看得见的那几行去断言整列：2026-09-11 复测里它按
            # is_active DESC 排序取回 18 行，看到开头全是 true 就说"18 家全部在用"
            # （实际 13 家）。
            partial = isinstance(total, int) and total > len(pv)
            head = (f"仅前 {len(pv)} 行（本次共返回 {total} 行，**其余未展示，"
                    f"不得据此断言整列的分布**）"
                    if partial else "全部行")
            line += f"\n    列：{cols}\n    {head}：{rows}"
            # 列级统计**只在预览不全时**回灌。
            #
            # 行全给到了的时候，模型看着那几行自己就能数，多贴一份是纯占位；
            # 而预览被裁掉时，它手上确实没有整列的分布 —— 硬约束第 6 条正是为
            # 这种时候写的（"禁止据此断言全部都是…"），可它此前只有"别外推"这句
            # 禁令，没有替代品，于是要么老实不说、要么多跑一轮去查。
            # 现在统计随 execute_sql 一起回来（tools.column_stats，零 IO），
            # 直接摆在它面前。
            if partial and h.get("stats"):
                line += f"\n    整列统计（全部 {total} 行，非仅上面几行）：{h['stats']}"
        elif h.get("columns"):
            line += f"\n    列：{'、'.join(h['columns'])}"
        out.append(line)
    return "\n".join(out)


#: 各参数在历史里保留多长。sql 单列一档：模型要靠回看自己发过的 SQL 才能
#: 说准口径（"这一列到底是 COUNT(*) 还是 COUNT(*) FILTER(...)"），截到 60 字
#: 等于把 SELECT 列表整段切掉——2026-09-11 跑测里把 COUNT(*) 说成"已排除软删除
#: 的条数"，就是看不见自己写了什么。
_ARG_KEEP = {"sql": 800}
_ARG_KEEP_DEFAULT = 60


def _fmt_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in (args or {}).items():
        s = str(v)
        keep = _ARG_KEEP.get(k, _ARG_KEEP_DEFAULT)
        if len(s) > keep:
            s = s[:keep] + " …（已截断）"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


#: 阿拉伯数字。判"这句话有没有在陈述一个量"，全角数字一并算上。
#: 只认数字、不认"五档"这类中文数词：宁可漏判，也不要把"无法回答"这类
#: 纯文字说明误挡掉——漏判的那部分由提示词第 1 条兜。
_NUM_RE = re.compile(r"[0-9\uff10-\uff19]")


def _has_number(text: str) -> bool:
    return bool(_NUM_RE.search(text or ""))


def _render_specs(hide: frozenset[str] = frozenset()) -> str:
    """把只读工具规格渲染成可读列表，注入 AGENT_SYSTEM。

    hide 里的工具**只是不出现在规格表里，并没有从 REGISTRY 摘掉** —— 模型硬要
    调仍然调得到（tools.invoke 走 REGISTRY，与这份规格表无关）。这是有意的：
    暴露面收窄是提示词侧的引导，不是能力阉割，判错了也不会把链路带进死胡同。
    """
    lines = []
    for s in tools.tool_specs():
        if s["name"] in hide:
            continue
        params = "，".join(f"{k}（{v}）" for k, v in s["params"].items())
        lines.append(f"- {s['name']}：{s['summary']}。参数：{params}")
    return "\n".join(lines)


def _sys(base: str, cfg: Config) -> str:
    """系统提示 = 基底 + Skill 方法论口径（可信的来源，见 skill.py）。"""
    block = skill.render(cfg)
    return f"{base}\n\n{block}" if block else base


def _known_constants(cfg: Config) -> list[float]:
    """护栏与预算的配置值。模型引用它们是对的，不该被当成"追溯不到的数"。"""
    out: list[float] = []
    for section in ("guard", "agent", "planner"):
        for v in (cfg.raw.get(section) or {}).values():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def _grounding_mode(cfg: Config) -> str:
    """接地校验的档位：off / shadow / enforce。

    默认 shadow —— 只记不拦。这条判定的误判代价直接落在正确答案上，
    而它的误判率只能在真实流量上量，不能在本机凭样例拍。先影子跑一轮，
    拿到数再决定切不切 enforce。
    """
    mode = str((cfg.raw.get("agent", {}) or {}).get("grounding", "shadow")).lower()
    return mode if mode in ("off", "shadow", "enforce") else "shadow"


def _grounding_no_evidence_mode(cfg: Config) -> str:
    """「没跑成 execute_sql、但某个工具成功过」这一档接地校验的档位：off/shadow/enforce。

    与 grounding（UNGROUNDED 那条）**分开一个旋钮**，因为两者误判的形状不同：
    UNGROUNDED 是"跑成了、答案没接上"，enforce 挡住的是模型把探查结果推开后
    继续编数；而这一档拦的是"没落成 last_exec 却给了数字"，它会连带误杀一类
    **正确的基础统计** —— D-3：150k 行的 COUNT 未在同步窗口内落 last_exec，
    答案里正确的计数被 _meta_evidence 追不到，判成 NO_EVIDENCE。放宽这一档不能
    顺带把 UNGROUNDED 一起放松，所以独立成钮。

    默认 enforce（维持既有行为，不静默放松任何部署）；生产按需切 shadow 观测。
    off 跳过这层校验，shadow 只记不拦、把答案照常递出。
    """
    mode = str((cfg.raw.get("agent", {}) or {}).get(
        "grounding_no_evidence", "enforce")).lower()
    return mode if mode in ("off", "shadow", "enforce") else "enforce"


def _budget(cfg: Config) -> tuple[int, int]:
    a = cfg.raw.get("agent", {}) or {}
    pl = cfg.raw.get("planner", {}) or {}
    max_steps = int(a.get("max_steps", 6))                                  # R-16
    cost_cap = int(a.get("cost_cap_tokens", pl.get("cost_cap_tokens", 12000)))  # R-17
    return max_steps, cost_cap


def _result(cfg: Config, question: str, trace_id: str, thread_id: str, org: int,
            tracer: Tracer, *, ok: bool, reasoning: str = "", last_exec: dict | None = None,
            rejected_by: str | None = None, error: str = "", hint: str = "",
            tables_hit: list[str] | None = None, step_count: int = 1,
            converged: str = "", ungrounded: list[str] | None = None) -> AskResult:
    d = last_exec or {}
    return AskResult(
        ok=ok, question=question, trace_id=trace_id, org_id=org, thread_id=thread_id,
        sql_final=d.get("sql_final", ""), reasoning=reasoning,
        rules_fired=list(d.get("rules_fired", [])), rewrites=list(d.get("rewrites", [])),
        columns=list(d.get("columns", [])), rows=list(d.get("rows", [])),
        row_count=int(d.get("row_count", 0)), truncated=bool(d.get("truncated", False)),
        as_of=d.get("as_of", ""), explain_rows=d.get("explain_rows"),
        masked_columns=list(d.get("masked_columns", [])),
        mask_degraded=bool(d.get("mask_degraded", False)),
        rejected_by=rejected_by, error=error, hint=hint,
        tables_hit=list(tables_hit or []),
        ungrounded_numbers=list(ungrounded or []),
        multi_step=step_count > 1, step_count=step_count, converged_early=converged,
        steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tracer.tok_in, tok_out=tracer.tok_out, cost_cny=tracer.cost_cny,
    )


def run_agent(question: str, cfg: Config, org_id: int | None = None, *,
              executor: Executor | None = None, llm: LlmClient | None = None,
              trace_id: str | None = None, thread_id: str | None = None,
              clarification: str = "", handoff: Any = None,
              on_span: Any = None) -> AskResult:
    """自主决策入口。返回与 graph.ask 同一套 AskResult，并写审计（收尾）。

    clarification 是发起人事后补上的条件（「补充条件」「换个问法」走这里）。
    它进决策历史而**不改写 question** —— question 是这条线程的身份，
    审计标题与审批指纹都读它，就地改掉会让同一条线程变成另一个问题。

    审计是任务中心 / 复核队列 / replay 的共同数据源：它们都从审计记录派生
    （audit.tasks / audit.needs_review），所以 agent 链路一旦如实写审计，这三样
    立刻复用现网机制，无需各造一套。
    """
    result = _drive(question, cfg, org_id, executor=executor, llm=llm,
                    trace_id=trace_id, thread_id=thread_id,
                    clarification=clarification, handoff=handoff, on_span=on_span)
    try:                                  # 审计不该成为查询失败的原因
        write_audit(cfg, _audit_of(result, cfg, "ask"))
    except Exception:
        pass
    return result


def _drive(question: str, cfg: Config, org_id: int | None = None, *,
           executor: Executor | None = None, llm: LlmClient | None = None,
           trace_id: str | None = None, thread_id: str | None = None,
           clarification: str = "", handoff: Any = None,
           on_span: Any = None) -> AskResult:
    """跑一次 agent 图。

    2026-09-12 从 `for step in range(...)` 改成 LangGraph（见 agentgraph）。
    换掉的是控制流，**每一条判定都原样搬了过去**；换来的是检查点覆盖到 agent
    链路，于是「创建任务 / 补充条件 / 换个问法」不必再为了续跑退回老管道 ——
    那条分流的代价是：最该深挖的请求反而被送进只能猜列名的链路。
    """
    from . import agentgraph

    trace_id = trace_id or uuid.uuid4().hex[:12]
    thread_id = thread_id or trace_id
    org = org_id if org_id is not None else int(
        cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
    tracer = Tracer(on_span=on_span)

    # 每日配额快速失败（与管道同一口径）。**进图之前判**，一个 token 都不花。
    dq = build_quota(cfg)
    over, used = dq.exhausted()
    if over:
        tracer.add("quota", tracer.start(), f"当日已用 {used}/{dq.limit}", status="blocked")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="QUOTA", error=f"已达当日模型调用上限（{used}/{dq.limit}）",
                       hint="明日自动恢复；直查 SQL 不受配额限制。")

    # 发起记录先落盘：进程中途被杀时检查点/审计里仍有这条线程，任务中心据此
    # 列得出、凭 thread_id 续得上（与管道 _execute 同一处理）。
    try:
        write_audit(cfg, {
            "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
            "phase": PHASE_STARTED, "thread_id": thread_id, "org_id": org,
            "question": question, "role": cfg.role or "ANONYMOUS",
            "user": cfg.user or "",
            "source": cfg.source_id or "builtin",
            "source_name": cfg.source_name or cfg.path,
        })
    except Exception:
        pass

    own_exec = executor is None
    ex = executor or Executor(cfg)
    client = llm or LlmClient(cfg)
    max_steps, cost_cap = _budget(cfg)
    deps = agentgraph.Deps(
        cfg=cfg, llm=client, executor=ex, tracer=tracer,
        ctx=tools.ToolContext(cfg=cfg, org_id=org, executor=ex),
        # 交接现场随执行走。节点边界据它判"要不要提前交接后台"（见 agentgraph）
        handoff=handoff)
    init = agentgraph.initial_state(question, org, trace_id, thread_id,
                                    max_steps, cost_cap, clarification)
    try:
        final = agentgraph.ensure_graph(cfg).invoke(
            init,
            {"configurable": {"thread_id": thread_id, "deps": deps},
             "recursion_limit": agentgraph.recursion_limit(max_steps)})
    except Exception as e:                        # noqa: BLE001
        # 图本身抛出来（递归上限、检查点库故障）。**不能让它变成裸 500** ——
        # 用户看到的必须是一句能理解的话，且这条线程要如实收尾，否则它会
        # 永远停在「运行中」（那正是 2026-09-11 那 8 条僵尸线程的来路）。
        tracer.add("finalize", tracer.start(), f"执行图异常：{e}", status="failed")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="EXEC", error=f"执行链路异常：{e}",
                       hint="这不是提问本身的问题，稍后重试；持续出现请联系运维。")
    finally:
        if own_exec:
            ex.close()
    return agentgraph.to_result(final, cfg, tracer)
