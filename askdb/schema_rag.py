"""Schema 与业务口径召回。

设计要点（技术设计说明书 §3.2.3）：
  * **禁止全库注入。** 无关表既浪费 token，又会干扰模型选表。
  * token 预算超出时按相关度截断，并**记录告警** ——
    静默截断会造成不可解释的准确率下降。
  * P0 采用关键词/别名匹配；P1 换向量检索。两种模式共用同一份文档构造逻辑，
    换实现时提示词内容不变，消融实验才有可比性。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config, Metric, Table
from .trace import embed_cost_cny

log = logging.getLogger("askdb.schema_rag")


# --------------------------------------------------------------------------
# 降级留痕
# --------------------------------------------------------------------------
#
# 2026-09-09 加。配置写着 mode: vector、运行时却每一次都回落 keyword，
# 这件事在线上持续了两天没被发现 —— 唯一的线索是单次结果里的 recall_note，
# 而那行字只有点开某一条结果才看得见。**声明的模式与实际跑的模式不一致，
# 属于部署级故障**，得像故障一样报出来：进程日志里 WARNING 一次，
# /api/health 常驻一格。
#
# 只记"最后一次"而不是全量计数明细：这里要回答的问题只有一个 ——
# "现在这台实例的向量召回到底在不在跑"，多余的维度只会让人多读几眼。

_deg_lock = threading.Lock()
_degraded: dict[str, Any] = {"declared": "", "effective": "", "reason": "",
                             "since": 0.0, "count": 0}


def note_degraded(declared: str, effective: str, reason: str) -> None:
    """记一次"没按声明的模式跑"。**同一个原因只在日志里喊一次** ——
    每次问答都 WARNING 一行会把日志淹掉，而它要传达的信息是状态不是事件。"""
    with _deg_lock:
        first = _degraded["reason"] != reason or _degraded["effective"] != effective
        if first:
            _degraded.update(declared=declared, effective=effective,
                             reason=reason, since=time.time(), count=0)
        _degraded["count"] += 1
    if first:
        log.warning("schema 召回降级：配置声明 %s，实际在跑 %s —— %s",
                    declared, effective, reason)


def note_healthy(mode: str) -> None:
    """这一次是按声明跑的。恢复了就把降级状态清掉 —— 留着旧告警会让人
    去查一个已经不存在的问题。"""
    with _deg_lock:
        if _degraded["reason"]:
            log.info("schema 召回已恢复：%s", mode)
        _degraded.update(declared=mode, effective=mode, reason="",
                         since=0.0, count=0)


def degradation() -> dict[str, Any]:
    """当前降级状态。给 /api/health 用；没降级时 degraded 为 False。"""
    with _deg_lock:
        d = dict(_degraded)
    return {
        "declared": d["declared"],
        "effective": d["effective"],
        "degraded": bool(d["reason"]),
        "reason": d["reason"],
        "since": (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(d["since"]))
                  if d["since"] else ""),
        "count": d["count"],
    }


def reset_degradation() -> None:
    """测试用。"""
    with _deg_lock:
        _degraded.update(declared="", effective="", reason="", since=0.0, count=0)


@dataclass
class Recall:
    tables: list[Table] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    prompt: str = ""
    est_tokens: int = 0
    truncated: list[str] = field(default_factory=list)   # 因预算被裁掉的表名
    mode: str = ""
    note: str = ""                                      # 降级等需要告知的情况
    #: 这次召回没有任何表命中关键词，挑出来的表是兜底而非相关度排序的结果。
    #: 必须一路传到界面：盲选下的答案看起来与正常答案毫无区别。
    blind: bool = False
    #: 声明的模式跑不起来、回落到了别的模式。note 里已经有一句人话，但那是
    #: 拼进"命中 N 张表"后面的一段自由文本 —— 要在 Span 表里把失败的那次
    #: 尝试单独落成一条，得有结构化的来源、错误与耗时。
    degraded_from: str = ""     # 声明的模式（回落时才有值）
    degrade_error: str = ""     # 回落原因的原始消息
    degrade_code: str = ""      # 异常类名，当错误码用
    degrade_ms: int = 0         # 失败那次尝试自己烧掉的时间
    #: 这次召回真正烧掉的 embedding 输入 token 与金额（vector 模式才有）。
    #: 全是厂商回传的实测值，取不到就是 0 —— 不估。
    embed_tokens: int = 0
    embed_cost: float = 0.0
    embed_model: str = ""

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]


def _est_tokens(text: str) -> int:
    """粗略估算：中文按字符计，英文按 4 字符 1 token。够用于预算控制。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def table_doc(t: Table) -> str:
    """把一张表渲染成提示词片段。召回与注入共用，保证两者一致。"""
    lines = [f"表 {t.name} —— {t.desc}"]
    if t.aliases:
        lines.append(f"  别名：{'、'.join(t.aliases)}")
    for c in t.columns.values():
        bits = [f"  - {c.name} ({c.type})"]
        if c.desc:
            bits.append(c.desc)
        if c.enum:
            bits.append(f"取值：{'/'.join(c.enum)}")
        if c.tenant:
            bits.append("【租户隔离列，系统会强制注入，不要自己写】")
        lines.append("  ".join(bits))
    return "\n".join(lines)


def metric_doc(m: Metric) -> str:
    head = f"口径「{m.name}」"
    if m.aliases:
        head += f"（同义：{'、'.join(m.aliases)}）"
    body = m.expr or m.predicate or ""
    out = f"{head}\n  必须使用此定义，不得自行构造：{body}"
    # 粒度是硬约束，语气要比"说明"更重 —— 它管的不是表达式对不对，
    # 而是这个表达式能不能被放进别的聚合语境
    if m.grain:
        out += f"\n  聚合粒度（同样不得违反）：{m.grain}"
    if m.note:
        out += f"\n  说明：{m.note}"
    return out


#: 库里是英文，人问的是中文 —— 这本词典就是那道缝。
#:
#: 2026-09-06 实测：careermate 源 33 张表，5 条中文提问的召回结果**完全相同**
#: （agent_messages / interview_questions / agent_tool_calls，正好是白名单前三
#: 张），因为 _score 只做 `t.name.lower() in question` 这种字面包含，中文问题
#: 对英文标识符恒为 0 分，全表并列第一，排序退化成白名单顺序。后果不是拒答，
#: 是**看起来成功的错答**：问"一共有多少个用户"拿 agent_messages 的
#: COUNT(DISTINCT user_id) 答了 6512，真值 users 表 10084。
#:
#: 词典是兜底不是终点：数据源白名单里的 desc / aliases 一旦填上（结构扫描现在
#: 会把库里的表注释、列注释抓进来），它们的权重更高，词典就只在没有注释的库上
#: 起作用。想要真正的语义召回，把 schema_rag.mode 换成 vector。
#:
#: 只收**双向都成立**的对应：把"记录"映射到 log 这种一对多的联想留给向量召回，
#: 塞进词典只会让本来准的查询变歪。
CN_HINTS: dict[str, tuple[str, ...]] = {
    "用户": ("user", "users", "account", "member"),
    "账号": ("user", "users", "account"),
    "会员": ("member", "user"),
    "组织": ("org", "organization", "tenant"),
    "租户": ("tenant", "org", "organization"),
    "公司": ("company", "corp", "employer"),
    "企业": ("company", "corp", "enterprise"),
    "岗位": ("job", "position", "post"),
    "职位": ("job", "position"),
    "工作": ("job", "work"),
    "招聘": ("job", "recruit", "hire"),
    "投递": ("application", "apply", "delivery"),
    "申请": ("application", "apply"),
    "简历": ("resume", "cv"),
    "面试": ("interview",),
    "题目": ("question", "quiz"),
    "问题": ("question",),
    "答案": ("answer",),
    "会话": ("session", "conversation", "chat"),
    "对话": ("conversation", "chat", "session", "message"),
    "消息": ("message", "msg"),
    "任务": ("task", "job", "run"),
    "计划": ("plan",),
    "工具": ("tool",),
    "调用": ("call", "invoke", "invocation"),
    "执行": ("run", "exec", "execution"),
    "知识库": ("knowledge", "kb", "knowledgebase"),
    "文档": ("document", "doc", "file"),
    "文件": ("file", "document"),
    "分块": ("chunk", "segment"),
    "检索": ("retrieval", "search", "query"),
    "召回": ("retrieval", "recall"),
    "模型": ("model", "llm"),
    "费用": ("cost", "fee", "expense", "usage"),
    "成本": ("cost", "usage"),
    "用量": ("usage", "quota"),
    "配额": ("quota", "limit"),
    "评测": ("eval", "evaluation", "benchmark"),
    "审计": ("audit",),
    "日志": ("log", "logs", "record"),
    "记录": ("log", "record", "history"),
    "历史": ("history", "log"),
    "权限": ("permission", "auth", "role", "acl"),
    "角色": ("role",),
    "登录": ("login", "signin", "session"),
    "通知": ("notification", "notify", "message"),
    "收藏": ("saved", "favorite", "collect", "star"),
    "标签": ("tag", "label"),
    "分类": ("category", "type", "class"),
    "城市": ("city", "location", "region"),
    "地区": ("region", "area", "location"),
    "地址": ("address", "location"),
    "薪资": ("salary", "pay", "compensation", "wage"),
    "工资": ("salary", "pay", "wage"),
    "学习": ("study", "learn"),
    "笔记": ("note", "notes"),
    "画像": ("profile", "portrait"),
    "档案": ("profile", "archive"),
    "偏好": ("preference", "prefs", "setting"),
    "设置": ("setting", "config", "preference"),
    "版本": ("version", "revision"),
    "快照": ("snapshot",),
    "状态": ("state", "status"),
    "反思": ("reflection", "reflect"),
    "记忆": ("memory",),
    "匹配": ("match", "matching"),
    "机会": ("opportunity", "job", "match"),
    "产物": ("artifact", "output"),
    "结果": ("result", "output"),
    "断点": ("checkpoint",),
    "协作": ("collab", "collaboration"),
    "安全": ("security", "audit"),
    "时间": ("time", "date", "at"),
    "数量": ("count", "num", "total"),
    "金额": ("amount", "money", "cost"),
}


def _tokens(name: str) -> set[str]:
    """标识符切词：`agent_tool_calls` → {agent, tool, calls, call}。

    末尾的复数 s 一并收进去，因为提问里的中文对应词是单数（"调用"→call），
    而表名习惯用复数（calls）—— 差这一个字母就前功尽弃。
    """
    out: set[str] = set()
    for w in re.split(r"[^0-9A-Za-z]+|(?<=[a-z0-9])(?=[A-Z])", str(name)):
        if not w:
            continue
        w = w.lower()
        out.add(w)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])
    return out


#: **泛词**：命中它们几乎说明不了什么。
#:
#: "记录"映射到 log / record / history，而任何库里都有一堆 *_log、*_history —— 实测
#: 问"有多少条聊天记录"，靠"记录"二字召回了 security_audit_logs、
#: user_password_history、tool_execution_log，真正该用的 agent_messages 一张没进。
#: 更糟的是当时 blind=False：有表得了分，盲选判定就以为召回成功，一句警告都不给。
#:
#: 所以泛词只算**弱信号**：加很小的分，且不足以让一次召回摆脱"盲选"这个判定。
#: 一个问题若只靠泛词得分，它与一张表都没命中在**可信度上是一回事**。
WEAK_HINTS: frozenset[str] = frozenset({
    "记录", "日志", "历史", "数据", "信息", "内容", "结果", "状态", "时间", "数量", "金额",
})


def _wanted(question: str) -> tuple[set[str], set[str]]:
    """从提问里解出"想找什么"，分强弱两档。

    英文词原样收（人直接写了 users 就是强信号），中文词经词典换成英文词；
    泛词映射出来的那些落到弱档。
    """
    q = question.lower()
    strong = {w for w in re.split(r"[^0-9A-Za-z]+", q) if len(w) > 1}
    weak: set[str] = set()
    for cn, ens in CN_HINTS.items():
        if cn not in question:
            continue
        (weak if cn in WEAK_HINTS else strong).update(ens)
    return strong, weak - strong


def _score(t: Table, question: str) -> tuple[int, bool]:
    """关键词相关度，外加**这次得分是不是靠得住**。

    命中判定按**词**，不按子串：中文提问对英文标识符做子串匹配恒为 0 分，
    而 0 分并列会让排序退化成白名单顺序 —— 那正是这个函数出过的事故。

    第二个返回值是"有没有强信号"：泛词（见 WEAK_HINTS）加的那点分不算数。
    全场没有一个强信号，就等同于一张表都没命中，上层照盲选处理。
    """
    s = 0
    strong_hit = False
    q = question.lower()
    strong, weak = _wanted(question)
    name_tokens = _tokens(t.name)

    if t.name.lower() in q:                       # 直接写了表名，最强信号
        s += 10
        strong_hit = True
    elif name_tokens & strong:
        # 表名的词被问到（含中文经词典转换）。按命中词数给分，
        # `interview_questions` 对"面试题目"命中两个词，理应压过只命中一个的表。
        s += 6 * len(name_tokens & strong)
        strong_hit = True
    elif name_tokens & weak:
        s += 1                                    # 泛词：给个排序上的微弱偏好，仅此而已

    for a in alias_hints(t):
        if a and a in question:
            s += 8
            strong_hit = True
    if t.desc:
        # 表注释是**库里真有的元数据**，权重排在别名之后、字段之前。
        # 中文注释与中文提问同语种，命中它比任何词典都准。
        hits = sum(1 for w in _desc_words(t.desc) if w in question)
        if hits:
            s += 4 * hits
            strong_hit = True
        # 整词对不上时退到 2-gram 重合：注释写「异常订单标记」、问题问
        # 「异常订单」，整词匹配是 0 分，2-gram 重合 3 个。封顶是必须的 ——
        # 注释越长重合越多，不封顶就变成"注释长的表恒赢"。
        overlap = len(_bigrams(t.desc) & _bigrams(question))
        if overlap >= 2:
            s += min(overlap, _BIGRAM_CAP)
            strong_hit = True

    for c in t.columns.values():
        ctoks = _tokens(c.name)
        if c.name.lower() in q:
            s += 3
            strong_hit = True
        elif ctoks & strong:
            s += 2
            strong_hit = True
        elif ctoks & weak:
            s += 1
        if c.desc:
            hits = sum(1 for w in _desc_words(c.desc) if w in question)
            if hits:
                s += 2 * hits
                strong_hit = True
        for e in c.enum:
            if e.lower() in q:
                s += 2
                strong_hit = True
    return s, strong_hit


#: 表注释里 "别名：甲、乙、丙" 这一段。运行时数据源的别名写在库注释里，
#: 扫描时并不会落进 Table.aliases —— 于是「别名」这份最准的语义在召回时
#: 完全没被用上（实测 order_exceptions 注释写着「别名：异常单、问题订单」，
#: 而问「未解决的异常订单」时它一分都拿不到）。
_ALIAS_RE = re.compile(r"别名[:：]\s*([^。；;\n]+)")

#: 2-gram 重合的封顶加分。压在表名整词命中（+6）之下 —— 注释只是佐证，
#: 不该盖过"表名就叫这个"这种最强信号；调高会让有注释的表压掉没注释的表。
_BIGRAM_CAP = 3


def alias_hints(t: Table) -> list[str]:
    """这张表的全部别名：显式声明的，加上注释里 "别名：…" 写的。

    纯函数、不改存储 —— 已经注册好的数据源不必重新扫描就能享受到。
    """
    out = list(t.aliases or [])
    m = _ALIAS_RE.search(t.desc or "")
    if m:
        out += [w.strip() for w in re.split(r"[、,，/|]", m.group(1)) if w.strip()]
    return out


#: 预聚合汇总表的判据。命名与注释各认一半 —— 这批库两种写法都有。
_SUMMARY_NAME = re.compile(r"(_stats_daily|_daily_stats|_stats|_summary|_agg)$")
# 只认"按 X 汇总"这类明确说法。光一个"汇总"太松：order_items 的注释里写着
# "汇总到 orders"，它是明细表，误判进来就等于把最该避开的大表当成了汇总表。
_SUMMARY_DESC = ("按日统计", "按日汇总", "按小时汇总", "按月汇总",
                 "优先查这张表", "日报", "按天/按月统计优先")


def summary_tables(cfg) -> list[Table]:
    """这个库里的预聚合汇总表。

    存在的理由很具体：明细表动辄百万行，`COUNT(*)` 会被 R-11 的扫描阈值拦下，
    而链路回灌给模型的提示是"缩小时间范围或加筛选条件"—— 模型照做，于是把
    一个窄窗口的数当成全量答案返回（实测「一共有多少笔订单」答 5.4 万，真值
    120 万）。库里其实备好了 order_daily_stats 这类汇总表，一次求和就是真值；
    问题只在于重试那一轮模型不一定看得见它。把它们显式挑出来喂回去。
    """
    out = []
    for t in cfg.tables.values():
        if _SUMMARY_NAME.search(t.name) or any(k in (t.desc or "") for k in _SUMMARY_DESC):
            out.append(t)
    return out


def summary_hint(cfg) -> str:
    """汇总表清单，渲染成可直接拼进提示词的一段。没有汇总表时返回空串。"""
    tabs = summary_tables(cfg)
    if not tabs:
        return ""
    return ("\n\n【本库的预聚合汇总表 —— 总量/按期统计类问题优先用这些，"
            "它们已经按天（或按维度）算好，不必扫明细表】\n"
            + "\n\n".join(table_doc(t) for t in tabs))


def _bigrams(text: str) -> set[str]:
    """中文按 2-gram 切。

    中文提问与中文注释之间靠"整词包含"匹配太脆：注释写「异常订单标记」，
    问题问「异常订单」，一个字之差就是 0 分 —— 这正是漏召回的直接原因。
    2-gram 重合不需要分词器，也不引依赖，在表注释这种短文本上足够稳。
    """
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    out: set[str] = set()
    for run in cjk:
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _desc_words(desc: str) -> list[str]:
    """注释里够长、值得当关键词的片段。

    只切标点：中文分词要么引依赖要么做不准，而"命中一个 2 字以上的连续片段"
    在表注释这种短文本上已经够用。
    """
    return [w for w in re.split(r"[\s,，、。;；:：（）()\[\]/]+", str(desc))
            if len(w) >= 2]


def _keyword_pick(question: str, cfg: Config, top_k: int, max_k: int,
                  ) -> tuple[list[Table], bool]:
    """按关键词挑表。第二个返回值 = **这次挑选是不是瞎猜**。

    瞎猜（全表 0 分）与"挑出了 3 张相关的表"在返回值上原来长得一模一样，
    于是链路把一次盲选当成一次正常召回接着往下跑，最后给出一个语气笃定的
    错答案。这个布尔量存在的唯一目的，就是让上层能区分这两件事。
    """
    all_tables = list(cfg.tables.values())
    graded = [(_score(t, question), t) for t in all_tables]
    scored = sorted(((sc, t) for (sc, _), t in graded), key=lambda x: -x[0])
    # 盲选的判据是**有没有强信号**，不是"有没有表得分"。只靠泛词得的那 1 分
    # 不足以说明召回对了 —— 让它算数，等于把一次偏掉的召回伪装成成功的召回。
    blind = not any(strong for (_, strong), _ in graded)
    picked = [t for s, t in scored if s > 0][:max_k]
    if len(picked) < top_k:
        # 召回不足时补齐，宁可多给一张表，也不要让模型无表可用
        for _, t in scored:
            if t not in picked:
                picked.append(t)
            if len(picked) >= top_k:
                break
    return picked, blind


def recall(question: str, cfg: Config, index: Any = None) -> Recall:
    mode = cfg.raw["schema_rag"].get("mode", "keyword")
    budget = int(cfg.raw["schema_rag"].get("token_budget", 4000))
    top_k = int(cfg.raw["schema_rag"].get("top_k", 3))
    max_k = int(cfg.raw["schema_rag"].get("max_k", 8))
    # 盲选兜底的专用预算。常规 budget（默认 1500）限制的是**正常召回**时别注入
    # 太多表挤占上下文；但盲选意味着关键词全落空，此时"让模型看得见全部表名"的
    # 价值远大于省那几千 token —— 全量注入实测仅 ~3000+ token（成本可忽略），却能
    # 把"问 A 答 B / 假称没有某表"从根上挡掉。用常规 budget 判全量回退，等于全量
    # 永远塞不进、回退形同虚设，正是这次要修的：把判据放宽到这条专用预算。
    blind_budget = max(budget, int(cfg.raw["schema_rag"].get("blind_budget", 8000)))
    eff_budget = budget

    all_tables = list(cfg.tables.values())
    metrics = [m for m in cfg.metrics if m.matches(question)]
    note = ""
    blind = False
    degraded_from = degrade_error = degrade_code = ""
    degrade_ms = 0
    embed_tokens, embed_cost, embed_model = 0, 0.0, ""

    if mode == "all":
        note_healthy("all")
        picked = all_tables
    elif mode == "vector":
        from .vectors import EmbeddingUnavailable, get_index

        idx = index if index is not None else get_index(cfg)
        # 表和口径在同一个索引里，若只取 max_k 条，两者会互相挤占名额 ——
        # 于是多取一些，再各自按配额与阈值筛。
        want = max_k + len(cfg.metrics) + 2
        min_score = float(cfg.raw["schema_rag"].get("min_score", 0.35))
        max_metrics = int(cfg.raw["schema_rag"].get("max_metrics", 2))
        _t_vec = time.perf_counter()
        try:
            # 带用量的那个入口优先 —— 没有它就拿不到"这次召回花了多少钱"。
            # 取不到时退回 search()：测试替身与旧实现只有这一个方法。
            with_usage = getattr(idx, "search_with_usage", None)
            if callable(with_usage):
                hits, embed_tokens = with_usage(question, want)
            else:
                hits, embed_tokens = idx.search(question, want), 0
        except EmbeddingUnavailable as e:
            # 召回退化只是准确率下降，不该让整条链路不可用。
            # **但它必须留痕**：配置声明 vector 而实际在跑 keyword，这件事
            # 只写在单次结果的 note 里就等于没人知道（2026-09-07 切过来之后
            # 线上两天都在回落，没有任何一处报出来）。
            note_degraded("vector", "keyword", str(e))
            degraded_from, degrade_error = "vector", str(e)
            degrade_code = type(e).__name__
            degrade_ms = int((time.perf_counter() - _t_vec) * 1000)
            picked, blind = _keyword_pick(question, cfg, top_k, max_k)
            mode, note = "keyword", f"向量召回不可用，已回落关键词：{e}"
        else:
            note_healthy("vector")
            embed_model = str(cfg.raw["schema_rag"].get("embedding_model", ""))
            embed_cost = embed_cost_cny(embed_tokens, cfg.raw["schema_rag"])
            ranked = [(h.score, cfg.tables[h.key.split(":", 1)[1]])
                      for h in hits
                      if h.key.startswith("table:") and h.key.split(":", 1)[1] in cfg.tables]
            # 过线的才要，但至少保底 top_k 张。
            # 保底不是妥协，是设计 §3.2.3 的明文要求：「召回 Top-K（默认 3，
            # 上限 5）表」。因此 min_score **不是硬阈值** —— 过线表不足 top_k
            # 时，低于阈值的表会被补齐进来。配置注释已同步说明这一点；
            # 若要让它成为硬阈值，须先改设计文档里的 Top-K 约定。
            over = [t for s, t in ranked if s >= min_score]
            picked = over[:max_k]
            # **一条都没过线 = 这次也是盲选。**
            #
            # 与 keyword 那边的"只有泛词命中"是同一件事的两种说法：下面补齐的
            # top_k 张是保底，不是相关度筛出来的。不在这里判，换到 vector 模式
            # 就等于把盲选示警整个关掉 —— 而盲选下的错答与正常答案在页面上
            # 长得一模一样，那层保护正是为它加的。
            blind = not over
            if len(picked) < top_k:
                picked = [t for _, t in ranked[:top_k]]
            # 向量也能召回口径 —— 别名没写全时靠语义补上。
            # 但口径是强约束（"必须用此定义"），塞多了反而会误导模型，
            # 所以只收相似度过线的前若干条。
            for h in hits:
                if not h.key.startswith("metric:") or h.score < min_score:
                    continue
                if sum(1 for _ in metrics) >= max_metrics + len(
                        [m for m in cfg.metrics if m.matches(question)]):
                    break
                name = h.key.split(":", 1)[1]
                m = next((x for x in cfg.metrics if x.name == name), None)
                if m and m not in metrics:
                    metrics.append(m)
            if len(picked) < top_k:
                for t in _keyword_pick(question, cfg, top_k, max_k)[0]:
                    if t not in picked:
                        picked.append(t)
                    if len(picked) >= top_k:
                        break
    else:
        note_healthy("keyword")
        picked, blind = _keyword_pick(question, cfg, top_k, max_k)

    # 一张表都没命中 = 这次召回是**盲选**，挑出来的只是白名单前几张。
    #
    # 原来这里什么都不做，链路照常往下跑，于是"问 A 答 B"—— 实测问"一共有多少
    # 个用户"，盲选给出 agent_messages，模型老老实实按给的表算了个
    # COUNT(DISTINCT user_id)，返回 6512，而真值是 users 表的 10084。
    # 没有任何一层报错，因为每一层都做对了自己那件事。
    #
    # 两条出路，先选便宜的那条：全部表塞得进预算就全给，让模型自己挑 ——
    # 模型看得见 33 张表的表名时不会挑错，看不见时只能在给它的 3 张里硬凑。
    # 塞不进就如实说"这次是盲选"，让上层把不确定性透出去，而不是伪装成一次
    # 正常召回。§3.2.3「禁止全库注入」针对的是**常态**，不是这种召回失败的兜底。
    if blind:
        # 措辞跟着模式走：两种模式失败的**方式**不同，排查的下一步也不同。
        # keyword 是词对不上（该加别名/注释），vector 是语义都不够近（该看
        # 阈值或问法）。给一句放之四海的"召回失败"，等于让人自己去猜。
        why = ("没有一张表的语义相似度达到阈值" if mode == "vector"
               else "关键词召回一张表都没命中")
        whole = _render(all_tables, metrics)
        if all_tables and _est_tokens(whole) <= blind_budget:
            picked = all_tables
            # 全库都给了，模型手上不再有"看不见的表"，这就不算盲选了 ——
            # 只有"给了 3 张、真正该用的那张不在里面"才需要向用户示警。
            blind = False
            # 下面的裁剪循环若仍用常规 budget，会把刚给的全量又按尾部裁回去，
            # 白忙一场。采纳全量兜底时，裁剪也跟着放宽到 blind_budget。
            eff_budget = blind_budget
            said = (f"{why}，已改为把全部 {len(all_tables)} 张表"
                    f"交给模型自行判断（盲选兜底预算内）")
        else:
            said = (f"{why}，下列 {len(picked)} 张表是按"
                    + ("相似度顺序" if mode == "vector" else "白名单顺序")
                    + "取的，**不是**过线选出来的；结果可能答非所问，请核对 SQL")
        # 已经有话要说（向量不可用回落到关键词）就接上，别覆盖 ——
        # "为什么回落"与"回落之后也没召到"是两条独立的信息，
        # 少了前一条，排查会从"关键词为什么不准"开始，方向就错了。
        note = f"{note}；{said}" if note else said

    # 召回正常但白名单整个塞得进预算 —— 把剩下的表按相关度顺序补在后面。
    #
    # §3.2.3「禁止全库注入」防的是几十上百张表挤占上下文；当整份白名单只有
    # 8 张、渲染出来还不到预算的一半时，"只挑 5 张"省不下什么，却实打实地
    # 制造了一种失败：该用的那张恰好没进候选，模型于是断言"库里没这类数据"
    # （实测 order_exceptions、carriers、warehouses 都是这么丢的）。
    #
    # **补在后面、不打乱前面的顺序**：排序是相关度信号，模型按顺序读，
    # 下面的预算裁剪也从尾部裁 —— 用白名单顺序覆盖掉排序，等于把召回的
    # 结论扔了，最相关的那张反而可能被裁掉。
    # **只在 keyword 模式做**：vector 模式的 min_score 是一条有意的阈值，
    # 低于它的表按设计就是干扰项（tests/test_schema_rag.py 里明写着），
    # 在那儿补全等于把阈值取消掉。关键词打分粗糙得多，漏召回的风险也高得多，
    # 这条兜底正是给它准备的。
    if mode == "keyword" and not blind and len(picked) < len(all_tables):
        rest = [t for t in all_tables if t not in picked]
        if _est_tokens(_render(picked + rest, metrics)) <= eff_budget:
            picked = picked + rest

    # 命中口径涉及的表必须一并注入，否则口径表达式引用的列不可见
    by_name = {t.name: t for t in picked}
    for m in metrics:
        for s in m.scope:
            if s not in by_name and s in cfg.tables:
                picked.append(cfg.tables[s])
                by_name[s] = cfg.tables[s]

    # token 预算：超出则按相关度从尾部裁剪，并记录被裁掉的表
    truncated: list[str] = []
    while picked:
        text = _render(picked, metrics)
        if _est_tokens(text) <= eff_budget or len(picked) == 1:
            break
        truncated.append(picked[-1].name)
        picked = picked[:-1]

    prompt = _render(picked, metrics)
    return Recall(
        tables=picked,
        metrics=metrics,
        prompt=prompt,
        est_tokens=_est_tokens(prompt),
        truncated=truncated,
        mode=mode,
        note=note,
        blind=blind,
        degraded_from=degraded_from,
        degrade_error=degrade_error,
        degrade_code=degrade_code,
        degrade_ms=degrade_ms,
        embed_tokens=embed_tokens,
        embed_cost=embed_cost,
        embed_model=embed_model,
    )


def _render(tables: list[Table], metrics: list[Metric]) -> str:
    parts = ["【可用的表】"]
    parts += [table_doc(t) for t in tables]
    if metrics:
        parts.append("\n【业务口径 —— 涉及以下概念时必须使用给定定义】")
        parts += [metric_doc(m) for m in metrics]
    return "\n\n".join(parts)
