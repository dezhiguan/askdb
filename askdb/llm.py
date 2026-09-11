"""模型调用 —— 经 OpenAI 兼容端点，结构化输出。

刻意保持薄：本项目的价值在护栏与链路，不在提示词技巧。
唯一的硬要求是**结构化输出**，避免模型把解释文字混进 SQL。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .config import Config
from .quota import QuotaExceeded, build_quota
from .trace import call_cost_cny

__all__ = ["LlmAttempt", "LlmClient", "LlmNotConfigured", "LlmUsage", "SqlDraft",
           "QuotaExceeded"]


class LlmNotConfigured(RuntimeError):
    """未配置密钥 —— 消息里直接给出可执行的修复步骤。"""


class SqlDraft(BaseModel):
    """模型的结构化产出。字段说明会进入 function schema，模型看得到。"""

    sql: str = Field(description="一条 SELECT 语句。不要写 markdown 代码块，不要加解释文字。")
    reasoning: str = Field(
        default="",
        description=("一句话说明这条 SQL 做了什么：用了哪些表、按什么条件过滤。"
                     "只描述这条 SQL 本身，不要描述系统、护栏或平台会做什么，"
                     "也不要提到任何'上一轮/上一次'——每次查询都是独立的。"),
    )
    caliber: str = Field(
        default="",
        description=("本次统计的口径，一句话。写明数的是哪张表的什么、"
                     "用了哪个业务口径定义、数值是实时统计还是取自缓存计数列。"
                     "例如：'文档数 = documents 表中 parse_status=COMPLETED 的行数（实时统计）'。"),
    )


SYSTEM = """你是一个只读数据查询助手，把用户的问题翻译成一条 SQL。

硬性要求：
1. 只能生成一条 SELECT 语句。禁止 INSERT/UPDATE/DELETE/DDL，禁止多条语句。
2. 只能使用下面列出的表和字段。**不存在的字段一律不要编**，宁可少查一列。
3. 禁止 SELECT *，显式列出需要的列。
4. 标注为租户隔离列的字段（如 org_id），**不要自己写进 WHERE**。
   同时**不要在 reasoning 里提这件事**（"未写租户过滤，由系统注入""租户隔离列
   由系统注入"之类）。护栏实际做了哪些改写会单独展示给用户，由你转述一遍
   只会产生假陈述：单租户库上根本没有注入这一步，而那句话照样写着。
   reasoning 只描述**这条 SQL 自己做了什么**。
5. 涉及【业务口径】里的概念时，必须使用给定的定义表达式，不得自行构造。
6. 结果列请使用中文别名，便于阅读。
7. 问"每个 X 有多少 Y"这类问题时，用 X LEFT JOIN Y —— 一个 Y 都没有的 X
   也是答案的一部分，内连接会把它们整行丢掉，读的人看不出少了谁。
   要的是"只统计有 Y 的 X"时才用内连接。
8. SQL 方言是 {dialect}。只能使用该方言真实存在的函数：
   - postgres 无 STRFTIME/DATE_FORMAT；按月份/日期格式化用 to_char(列, 'YYYY-MM')，
     按月分桶用 date_trunc('month', 列)。
   - duckdb 才用 strftime。
   - mysql 无 to_char/strftime/date_trunc：按月份格式化用 DATE_FORMAT(列, '%Y-%m')，
     按月分桶用 DATE_FORMAT(列, '%Y-%m-01')；字符串拼接用 CONCAT(…)，`||` 在
     MySQL 默认是逻辑或、不是拼接；间隔写 INTERVAL 1 DAY（数字与单位都不加
     引号），当前日期用 CURDATE()。
   不要跨方言套用另一种数据库的函数名。
9. 按名称定位某一个具体实体时，优先用主键 id 精确匹配；只有在问题给的是
   名称、拿不到 id 时才按名称匹配。按名称匹配一律用规范化的等值/包含，且
   **把匹配到的名称一并选进结果列**，让读的人能看出到底匹配上了哪一个。
   模糊匹配（ILIKE '%词%'）可能同时命中多个同类实体，若问的是"某一个"，
   这会把多个实体的量悄悄合计成一个数——除非问题本身就要"所有含该词的"，
   否则不要用模糊匹配做单实体统计。
10. **不要用近似的表硬凑答案。** 如果问题要查的**核心对象/主体**在给定表里没有
   直接对应（只能靠某个 type/category/ref_type 字段拼一个近似含义、或跨到本库
   根本不存在的概念），就返回空 SQL 并在 reasoning 里说明缺哪张表，而不是挑一张
   最像的表编一个过滤条件。返回 0 行的编造比如实说"答不了"更难被发现。
   但要分清"核心对象缺失"和"修饰词无对应列"：问题里的**平台名/产品名/租户名**
   （如"careermate 有多少用户""本平台的文档数"）通常只是**限定语境的修饰词**，
   当整库就属于这一个平台、没有可据以过滤的列时，**直接忽略这个修饰词、照常统计
   主体**（数 users、数 documents），不要因为找不到"平台"对应列就拒答。只有当
   要数/要查的那个主体本身没有表时才返回空。

11. **枚举取值必须逐字照抄** schema 里 `取值：` 后面列出的字符串，包括大小写。
   写错大小写的比较（`status = 'failed'` 而库里是 `'FAILED'`）语法完全正确、
   结果恒为空，是最难被发现的一类错。schema 没给取值时，不要凭猜测拼字符串
   去比较，改用非空判断或按该列分组把取值列出来。
12. **按维度分区的统计表要先聚合再取。** 注释写着"一天一行"的表直接取最新一行
   即可；写着按仓/按渠道/按类目分的，最新一天有多行，必须 SUM/AVG 汇总，
   `ORDER BY 日期 DESC LIMIT 1` 只会取到其中一个分区（实测把 26 个仓的缺货数
   答成了 1 个仓的）。
13. **两跳问题只返回最终那个值。** 问"X 最多的那个 A，他的 B 是多少"，要的是 B，
   用子查询定位 A、外层只选 B（可再带一列 A 的名称便于核对），不要把 A 的
   整行档案返回 —— 那没有回答问题。
14. **相对时间一律用当前时间函数表达。** 用户消息里会给出【当前日期】。问到
   今天/昨天/本周/本月/最近 N 天时，写 {interval_example}
   这样的表达式，不要凭印象写死一个日期字面量，更**不要拿 `MAX(时间列)`
   当"今天"** —— 库里最新有数据的那一天不是今天，这么写会把前天的数
   贴上"昨日"的标签返回。用户问的那一天若确实没有数据，就让查询自然返回
   空结果，并**不要**用 COALESCE(…, 0) 把"没有数据"抹成"数值是 0"：
   前者是"查不到"，后者是"确实一单没有"，两句话的业务含义完全不同。

15. **缺的是"维度"时，和缺表一样要如实说。** 问"哪个活动的 ROI 最高"，而统计表
   只有日粒度、没有活动外键 —— 这时返回"某一天 ROI 最高的那一行"不是答案，
   是把一个别的问题的答案递过去。判断的落点很简单：**结果里有没有那个被问到的
   实体**。没有就返回空 SQL 并说明缺哪个维度。
16. **问实体就按实体聚合。** 一张表若是"一个实体每期一行"（月度评分、日快照），
   问"最差的 5 家供应商"要的是**供应商**，必须 GROUP BY 实体再排序；直接
   `ORDER BY 分数 LIMIT 5` 取到的是"最差的 5 条月度记录"，实测 5 条分散在
   三个不同年月、还混进一条两年前的。只取某一期时，要把期次一并选进结果列。

17. **没有"上一轮"。** 每次查询都是完全独立的，你看不到任何历史问答，
   系统也不会把上次的结果带进来。因此**禁止**写"沿用上一轮口径""承接上一轮"
   "与上次保持一致"这类话 —— 它们描述的事情没有发生过，而读的人会因为这句话
   跳过核对。问题若省略了主体（"第二名呢""那反过来排呢""把创建时间也带上"），
   你**没有**足够信息回答：返回空 SQL，在 reasoning 里说明需要用户补充什么。
   **不要**挑一个"最常见的"主体来猜，更不要把猜出来的结果标成"占位"后照样返回 ——
   页面上占位数据和答案长得一模一样。

18. **不确定就不要给结果。** 如果你在 reasoning 里要写"无法确定""按最常见的"
   "假设用户指的是""口径需人工确认"，那就说明这题的口径没定下来 —— 正确动作是
   返回空 SQL 并把要澄清的点列出来，而不是带着这句话给一个数。一个标着
   "仅供参考"的数字，在页面上和一个确定的答案没有区别。

19. **按名称匹配必须自己做规范化。** 用户输入的名称常常少一个空格、大小写不同
   （库里是"岗位 JD 库"，用户打的是"岗位JD库"）。要么写成去空格 + 转小写之后的
   等值比较（如 `REPLACE(LOWER(k.name), ' ', '') = '岗位jd库'`），要么用包含匹配，
   **不要**直接 `k.name = '用户原样输入'` —— 那会返回零行，而零行在页面上会被
   读成"这个库是空的"，比报错更难被发现。reasoning 里说"用了规范化匹配"而 SQL
   里其实是精确等值，属于假陈述，同样禁止。

如果问题无法用给定的表回答，就在 reasoning 里说明缺什么，sql 字段返回空字符串。"""

#: 「昨天」在各方言里的正确写法。**必须跟着方言变**：MySQL 不认
#: `INTERVAL '1 day'` 那种带引号的写法（会当成字符串，语法直接报错），
#: 而这一条提示词的作用恰恰是让模型别写死日期 —— 给一个在当前库上跑不通的
#: 示例，等于把它推回去写日期字面量，那正是这条规则要防的事。
INTERVAL_EXAMPLE = {
    "mysql": "`CURDATE() - INTERVAL 1 DAY`",
    "postgres": "`CURRENT_DATE - INTERVAL '1 day'`",
    "duckdb": "`CURRENT_DATE - INTERVAL '1 day'`",
}
DEFAULT_INTERVAL_EXAMPLE = "`CURRENT_DATE - INTERVAL '1 day'`"

USER = """{schema}
{now}
【用户问题】
{question}{step}"""

RETRY = """{schema}
{now}
【用户问题】
{question}

【上一次生成的 SQL】
{last_sql}

【失败原因】
{error}

请修正后重新生成。不要重复同样的错误。

约束（不得违反）：
1. 只修正 SQL 本身的写法。**不得改用其他表或字段，去回答一个关于不可用
   对象的问题** —— 用户问 A，就不能改成答 B。
2. **不得擅自缩小用户问的范围。** 失败原因是"扫描行数超过阈值"时，正确做法
   依次是：① 改用【预聚合汇总表】把结果一次算出来；② 该库没有汇总表时，
   如实返回空 SQL 并在 reasoning 里说明"数据量超出单次查询上限"。
   **绝不要**给用户没有要求的查询加时间窗（"近 30 天""2024 年 1 月"）或其他
   过滤条件来把扫描量压下去 —— 那会把一个窄范围的数字当成全量答案返回，
   用户看不出任何异常。确有必要收窄时，必须在 reasoning 首句写明
   "已收窄范围：<具体条件>"。
3. 禁止使用 TABLESAMPLE 等抽样语法。抽样得到的是估计值不是真值，
   用它去满足扫描阈值等于返回一个错数。
4. 若失败原因是"表不在白名单""不允许的语句类型""跨 schema 引用""危险函数"，
   说明该对象**本就不开放**，换个写法救不回来。此时应如实说明无法作答，
   而不是找一个能通过校验的替代品。返回一个看起来合理但答非所问的数字，
   比返回失败危险得多。"""


@dataclass
class LlmUsage:
    """一次或多次模型调用的用量与**已结算金额**。

    金额随用量一起累加，而不是最后拿总 token 乘一个单价：一次问答里的多次调用
    可能由不同模型应答（兜底），也可能跨过计费时段边界，全程单价并不一致。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0    # 含在 input_tokens 内，不另计
    cost_cny: float = 0.0

    def add(self, other: "LlmUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.cost_cny = round(self.cost_cny + other.cost_cny, 6)


def _err_code(exc: BaseException) -> str:
    """把异常折成一个能放进表格一列的短码。

    优先用厂商自己给的状态码 —— 「HTTP 429」和「HTTP 504」是两回事，前者
    该退避重试、后者该换模型，折成同一个异常类名就分不出来了。取不到再退回
    从消息里抠三位数字（openai 客户端把它写成 "Error code: 429 - {...}"），
    都没有才用类名。**不编码**：宁可给个类名，也不猜一个看起来很像的数字。
    """
    for attr in ("status_code", "http_status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return f"HTTP {v}"
        if isinstance(v, str) and v.strip():
            return v.strip()[:32]
    m = re.search(r"[Ee]rror code:\s*(\d{3})", str(exc))
    if m:
        return f"HTTP {m.group(1)}"
    return type(exc).__name__


def _prompt_text(system: str, human: str) -> str:
    """把一次调用真正发出去的两段消息拼成可读的一份。

    分节标注而不是直接首尾相接：system 与 human 在排查时是两件事 ——
    「护栏说明写漏了」和「表结构没喂进去」看起来都是一大段文本，
    不标节就得靠眼睛找分界。
    """
    return f"【system】\n{system}\n\n【human】\n{human}"


def _raw_text(raw: object) -> str:
    """把厂商原始响应折成一段文本。

    include_raw=True 时 langchain 回的是 {"raw": AIMessage, "parsed": ..., "parsing_error": ...}。
    三样都要：parsed 是结构化结果，raw.content 是模型原话（结构化失灵时它常常
    不是空的，而那正是唯一能看出模型想说什么的东西），parsing_error 说明为什么没解析出来。
    **取不到就返回空串，绝不抛** —— 观测字段不能反过来把主链路弄崩。
    """
    if raw is None:
        return ""
    try:
        if isinstance(raw, dict):
            parts: list[str] = []
            msg = raw.get("raw")
            content = getattr(msg, "content", None)
            if content:
                parts.append(f"【content】\n{content}")
            calls = getattr(msg, "tool_calls", None)
            if calls:
                parts.append("【tool_calls】\n" + json.dumps(calls, ensure_ascii=False,
                                                            default=str, indent=2))
            parsed = raw.get("parsed")
            if parsed is not None:
                dump = getattr(parsed, "model_dump", None)
                obj = dump() if callable(dump) else parsed
                parts.append("【parsed】\n" + json.dumps(obj, ensure_ascii=False,
                                                        default=str, indent=2))
            err = raw.get("parsing_error")
            if err:
                parts.append(f"【parsing_error】\n{err}")
            return "\n\n".join(parts)
        dump = getattr(raw, "model_dump", None)
        if callable(dump):
            return json.dumps(dump(), ensure_ascii=False, default=str, indent=2)
        return str(raw)
    except Exception:
        return ""


@dataclass
class LlmAttempt:
    """**一次真实的厂商调用**。

    这是本次改造的地基：回退与重试原本整个发生在 LlmClient 内部，
    Tracer 在外面只看得到最后返回的那个结果 —— 主模型超时、切备选、
    切成功，三件事在审计里一件都不剩。把每次尝试都记下来，
    上层才有东西可落 span。
    """

    model: str
    status: str = "ok"              # ok | failed
    ms: int = 0
    error_code: str = ""
    error_message: str = ""
    disposition: str = ""           # 失败后做了什么
    is_fallback: bool = False       # 这次尝试是不是备选模型出的
    usage: LlmUsage = field(default_factory=LlmUsage)
    #: 这次调用**实际发出去的提示词全文**（system + human 拼接）。
    #: 与 error_message 不同，它不是出了事才有——每次调用都记，因为
    #: 「模型看到了什么」正是判断一条链路对不对的第一手材料。
    #: 内容边界见 audit.STEP_FIELDS 上那段说明：2026-09-12 起随 /api/trace 出接口。
    prompt: str = ""
    #: 厂商回的**原始响应**，结构化解析之前的那一份。解析后的对象只是它的
    #: 一个投影，模型说了什么、为什么这么说，只有这里留得住。
    raw: str = ""


class LlmClient:
    """主模型 + 可选备选模型。

    备选只在主模型**调用失败**时兜底（限流、超时、厂商故障），
    不在"生成的 SQL 不对"时切换 —— 那是反思重试该干的事，两者不要混。
    """

    def __init__(self, cfg: Config, llm_cfg: dict | None = None, is_fallback: bool = False,
                 journal: list[LlmAttempt] | None = None):
        self.cfg = cfg
        self.llm_cfg = llm_cfg if llm_cfg is not None else cfg.llm
        self.is_fallback = is_fallback
        self._model = None
        self._fallback: LlmClient | None = None
        # 主客户端与备选客户端**共用同一本流水**：备选是主模型失败后的下一次
        # 尝试，两者属于同一步，分开记就拼不回"先谁后谁"。
        self._journal: list[LlmAttempt] = [] if journal is None else journal
        # 配额扣在这里，而不是请求入口：一次提问会触发多次模型调用
        # （多步规划每步生成 + 每步评估 + 反思重试），按请求计数会大幅低估花费。
        self.quota = build_quota(cfg)

    def _reserve(self) -> None:
        """发起调用前先占一个名额。超限直接抛，一个 token 都不花。

        备选模型也照扣 —— 主模型失败后切备选是**又一次**真实的厂商调用，
        不扣就等于失败重试不要钱。
        """
        self.quota.reserve()

    # ---- 备选模型 ----

    def _fallback_client(self) -> "LlmClient | None":
        if self.is_fallback:
            return None
        spec = self.llm_cfg.get("fallback")
        if not spec:
            return None
        if isinstance(spec, str):          # 仅换模型名，其余沿用主配置
            spec = {"model": spec}
        merged = {**{k: v for k, v in self.llm_cfg.items() if k != "fallback"}, **spec}
        if self._fallback is None:
            self._fallback = LlmClient(self.cfg, llm_cfg=merged, is_fallback=True,
                                       journal=self._journal)
        return self._fallback

    # ---- 调用流水 ----

    def take_attempts(self) -> list[LlmAttempt]:
        """取走并清空本步的尝试流水。

        **每个调用点都必须取一次**，包括抛异常的分支：不取的话，这一步失败的
        尝试会顺延到下一个节点被取走，落成挂在别人名下的 span。
        """
        out = list(self._journal)
        self._journal.clear()
        return out

    def _note_ok(self, t0: float, usage: LlmUsage,
                 prompt: str = "", raw: object = None) -> None:
        self._journal.append(LlmAttempt(
            model=self.model_name, status="ok", is_fallback=self.is_fallback,
            ms=int((time.perf_counter() - t0) * 1000), usage=usage,
            prompt=prompt, raw=_raw_text(raw),
        ))

    def _note_fail(self, t0: float, exc: BaseException, disposition: str,
                   usage: LlmUsage | None = None, prompt: str = "",
                   raw: object = None) -> None:
        self._journal.append(LlmAttempt(
            model=self.model_name, status="failed", is_fallback=self.is_fallback,
            ms=int((time.perf_counter() - t0) * 1000),
            error_code=_err_code(exc),
            # 原始消息原样留着，截断交给前端 —— 在这里截就再也拿不回来了
            error_message=str(exc),
            disposition=disposition,
            usage=usage or LlmUsage(),
            # 失败那次的提示词**尤其**要留：它正是"为什么会失败"的现场。
            prompt=prompt, raw=_raw_text(raw),
        ))

    @property
    def model_name(self) -> str:
        return str(self.llm_cfg.get("model", "?"))

    def _api_key(self) -> str | None:
        import os

        return os.environ.get(self.llm_cfg["api_key_env"]) or None

    def _build(self):
        if self._model is not None:
            return self._model
        key = self._api_key()
        if not key:
            env = self.llm_cfg["api_key_env"]
            raise LlmNotConfigured(
                f"未配置模型密钥（环境变量 {env}）。\n"
                f"  1. cp .env.example .env\n"
                f"  2. 在 .env 中填入 {env}=你的密钥\n"
                f"  3. 或直接 export {env}=...\n"
                f"提示：无需密钥也能验证护栏与执行链路，用 `askdb sql \"SELECT ...\"`。"
            )
        from langchain_openai import ChatOpenAI  # 延迟导入，未配密钥时不必加载

        kwargs: dict = {}
        # 思考模式一律默认关掉：它不支持强制 tool_choice，结构化输出会直接 400；
        # SQL 生成也用不上长链思考 —— 输出是最贵的一档，白烧 reasoning token
        # 没有意义。可在配置里 thinking: true 显式打开。
        #
        # 关的参数名各家不同，且都是厂商私有参数，发给别家可能被拒，因此按
        # provider 分发，不做统一封装 —— 少一层抽象，多一份"发错家"的可见性。
        thinking = bool(self.llm_cfg.get("thinking", False))
        provider = str(self.llm_cfg.get("provider", "")).lower()
        if "deepseek" in provider:
            state = "enabled" if thinking else "disabled"
            kwargs["extra_body"] = {"thinking": {"type": state}}
        elif "dashscope" in provider:
            # 百炼的混合思考模型（qwen3.8-flash / qwen3.8-max / qwen3.8-27b）
            # 官方文档明写"默认开启思考模式"，不显式关就会当场踩上面那个 400。
            # 参数名是 enable_thinking，非 OpenAI 标准参数，须走 extra_body。
            kwargs["extra_body"] = {"enable_thinking": thinking}

        self._model = ChatOpenAI(
            model=self.llm_cfg["model"],
            base_url=self.llm_cfg["base_url"],
            api_key=key,
            temperature=float(self.llm_cfg.get("temperature", 0)),
            timeout=90,
            max_retries=1,
            **kwargs,
        )
        return self._model

    def structured(self, schema, system: str, human: str) -> tuple[Any, LlmUsage]:
        """通用的结构化调用 —— 规划与评估节点共用，不重复一套调用逻辑。"""
        self._reserve()
        model = self._build().with_structured_output(
            schema, method="function_calling", include_raw=True
        )
        prompt = _prompt_text(system, human)
        t0 = time.perf_counter()
        try:
            out = model.invoke([("system", system), ("human", human)])
        except Exception as primary_err:
            fb = self._fallback_client()
            self._note_fail(t0, primary_err,
                            f"切备选模型 {fb.model_name} 重试" if fb else "无备选模型，链路终止",
                            prompt=prompt)
            if fb is None:
                raise
            try:
                return fb.structured(schema, system, human)
            except Exception as fb_err:
                raise RuntimeError(
                    f"主模型 {self.model_name} 调用失败：{primary_err}；"
                    f"备选 {fb.model_name} 也失败：{fb_err}"
                ) from primary_err
        usage = _usage_of(out, self.llm_cfg)
        parsed = out["parsed"] if isinstance(out, dict) else out
        if parsed is None:
            err = RuntimeError("模型未按结构化格式返回")
            # 没产出也照记 token：这次调用真的花了钱，抛异常不是不计费的理由。
            #
            # 原来这里"无重试，链路终止"。2026-09-11 跑测里它把两条最普通的
            # GROUP BY 问句直接判死（rejected_by=LLM），而这只是一次偶发的
            # 格式抖动 —— 与调用抛异常在性质上没有区别，而那条路径是切备选
            # 模型重试的。两条路走同一套处置，不再厚此薄彼。
            fb = self._fallback_client()
            self._note_fail(t0, err,
                            f"格式失败，切备选模型 {fb.model_name} 重试" if fb
                            else "格式失败，无备选模型，链路终止", usage=usage,
                            prompt=prompt, raw=out)
            if fb is not None:
                try:
                    return fb.structured(schema, system, human)
                except Exception as fb_err:
                    raise RuntimeError(
                        f"主模型 {self.model_name} 未按结构化格式返回；"
                        f"备选 {fb.model_name} 也失败：{fb_err}") from err
            raise RuntimeError("模型未按结构化格式返回，请重试或更换模型。")
        self._note_ok(t0, usage, prompt=prompt, raw=out)
        return parsed, usage

    def generate_sql(
        self,
        question: str,
        schema_prompt: str,
        dialect: str = "duckdb",
        last_sql: str = "",
        error: str = "",
        step: str = "",
        today: str = "",
    ) -> tuple[SqlDraft, LlmUsage]:
        # 必须显式指定 function_calling：
        #   默认可能落到 JSON mode（response_format=json_object），而百炼要求
        #   该模式下消息里必须出现 "json" 字样，否则直接 400。
        #   Tool Calls 在 DashScope 与 DeepSeek 两边都支持，且比 JSON mode 更稳。
        self._reserve()
        model = self._build().with_structured_output(
            SqlDraft, method="function_calling", include_raw=True
        )
        system = SYSTEM.format(
            dialect=dialect,
            interval_example=INTERVAL_EXAMPLE.get(dialect, DEFAULT_INTERVAL_EXAMPLE))
        # 模型不知道今天是几号 —— 不给它，"昨天""最近一个月"就只能靠猜。
        # 实测猜出来的是 `MAX(stat_date)`（返回前天的数）和 `'2025-07-01'`
        # （真实数据到 2026-09）。给了之后 R-24 才有一条**正确的出路**可指。
        now = f"\n【当前日期】{today}（相对时间以此为准）\n" if today else ""
        if error:
            human = RETRY.format(schema=schema_prompt, question=question,
                                 last_sql=last_sql, error=error, now=now)
        else:
            human = USER.format(schema=schema_prompt, question=question,
                                step=step, now=now)

        prompt = _prompt_text(system, human)
        t0 = time.perf_counter()
        try:
            out = model.invoke([("system", system), ("human", human)])
        except Exception as primary_err:
            fb = self._fallback_client()
            self._note_fail(t0, primary_err,
                            f"切备选模型 {fb.model_name} 重试" if fb else "无备选模型，链路终止",
                            prompt=prompt)
            if fb is None:
                raise
            # 主模型不可用时兜底一次。失败原因串在一起抛出，便于定位到底是谁挂了。
            try:
                draft, usage = fb.generate_sql(question, schema_prompt, dialect,
                                               last_sql, error, step, today)
            except Exception as fb_err:
                raise RuntimeError(
                    f"主模型 {self.model_name} 调用失败：{primary_err}；"
                    f"备选 {fb.model_name} 也失败：{fb_err}"
                ) from primary_err
            return draft, usage

        draft = out["parsed"] if isinstance(out, dict) else out
        usage = _usage_of(out, self.llm_cfg)
        if draft is None:
            # 结构化输出偶发失灵是**瞬时抖动**，不是这道题答不了：实测
            # "一共有多少个仓库"（数 26 行）也翻过车，隔一会儿重问即成功。
            # 原来这里直接抛，用户拿到的是一句"请重试或更换模型"——把一次
            # 本可自愈的抖动变成了一次失败。就地重试一次，仍失败才抛。
            # 重试是**又一次真实的厂商调用**，用量照记、配额照扣 ——
            # 与切备选模型同理，不扣就等于失败重试不要钱。
            self._note_fail(t0, RuntimeError("模型未按结构化格式返回"),
                            "同模型就地重试一次", usage=usage, prompt=prompt, raw=out)
            self._reserve()
            t1 = time.perf_counter()
            try:
                retry_out = model.invoke([("system", system), ("human", human)])
            except Exception as retry_err:
                self._note_fail(t1, retry_err, "就地重试也失败，链路终止", prompt=prompt)
                raise
            draft = retry_out["parsed"] if isinstance(retry_out, dict) else retry_out
            retry_usage = _usage_of(retry_out, self.llm_cfg)
            usage.add(retry_usage)
            if draft is None:
                self._note_fail(t1, RuntimeError("模型连续两次未按结构化格式返回"),
                                "已重试一次仍失败，链路终止", usage=retry_usage,
                                prompt=prompt, raw=retry_out)
                raise RuntimeError("模型连续两次未按结构化格式返回，请稍后重试或更换模型。")
            # 救回来了。**这一条只记重试那次的用量** —— 被废弃的那次已经由
            # 上面那条 failed 各自记着，成功这条再记一遍就是把返工的账算两遍。
            self._note_ok(t1, retry_usage, prompt=prompt, raw=retry_out)
            return draft, usage
        self._note_ok(t0, usage, prompt=prompt, raw=out)
        return draft, usage


def _usage_of(out: object, llm_cfg: dict | None = None) -> LlmUsage:
    """把厂商回传的用量折成 LlmUsage，并当场按 llm_cfg 的单价结算金额。

    缓存命中量取 langchain 归一化后的 input_token_details.cache_read ——
    实测百炼与 DeepSeek 两边都填这个字段（DeepSeek 另给 prompt_cache_hit_tokens，
    值一致），所以不必按厂商分支去读私有字段。
    """
    raw = out.get("raw") if isinstance(out, dict) else None
    meta = getattr(raw, "usage_metadata", None) or {}
    tok_in = int(meta.get("input_tokens", 0) or 0)
    tok_out = int(meta.get("output_tokens", 0) or 0)
    cached = int((meta.get("input_token_details") or {}).get("cache_read", 0) or 0)
    cost = call_cost_cny(tok_in, tok_out, cached, llm_cfg) if llm_cfg else 0.0
    return LlmUsage(
        input_tokens=tok_in,
        output_tokens=tok_out,
        cached_input_tokens=cached,
        cost_cny=cost,
    )
