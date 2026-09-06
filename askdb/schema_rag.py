"""Schema 与业务口径召回。

设计要点（技术设计说明书 §3.2.3）：
  * **禁止全库注入。** 无关表既浪费 token，又会干扰模型选表。
  * token 预算超出时按相关度截断，并**记录告警** ——
    静默截断会造成不可解释的准确率下降。
  * P0 采用关键词/别名匹配；P1 换向量检索。两种模式共用同一份文档构造逻辑，
    换实现时提示词内容不变，消融实验才有可比性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import Config, Metric, Table


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

    for a in t.aliases:
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
    budget = int(cfg.raw["schema_rag"].get("token_budget", 1500))
    top_k = int(cfg.raw["schema_rag"].get("top_k", 3))
    max_k = int(cfg.raw["schema_rag"].get("max_k", 5))

    all_tables = list(cfg.tables.values())
    metrics = [m for m in cfg.metrics if m.matches(question)]
    note = ""
    blind = False

    if mode == "all":
        picked = all_tables
    elif mode == "vector":
        from .vectors import EmbeddingUnavailable, VectorIndex

        idx = index or VectorIndex(cfg)
        # 表和口径在同一个索引里，若只取 max_k 条，两者会互相挤占名额 ——
        # 于是多取一些，再各自按配额与阈值筛。
        want = max_k + len(cfg.metrics) + 2
        min_score = float(cfg.raw["schema_rag"].get("min_score", 0.35))
        max_metrics = int(cfg.raw["schema_rag"].get("max_metrics", 2))
        try:
            hits = idx.search(question, want)
        except EmbeddingUnavailable as e:
            # 召回退化只是准确率下降，不该让整条链路不可用
            picked, blind = _keyword_pick(question, cfg, top_k, max_k)
            mode, note = "keyword", f"向量召回不可用，已回落关键词：{e}"
        else:
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
        if all_tables and _est_tokens(whole) <= budget:
            picked = all_tables
            # 全库都给了，模型手上不再有"看不见的表"，这就不算盲选了 ——
            # 只有"给了 3 张、真正该用的那张不在里面"才需要向用户示警。
            blind = False
            said = (f"{why}，已改为把全部 {len(all_tables)} 张表"
                    f"交给模型自行判断（仍在 token 预算内）")
        else:
            said = (f"{why}，下列 {len(picked)} 张表是按"
                    + ("相似度顺序" if mode == "vector" else "白名单顺序")
                    + "取的，**不是**过线选出来的；结果可能答非所问，请核对 SQL")
        # 已经有话要说（向量不可用回落到关键词）就接上，别覆盖 ——
        # "为什么回落"与"回落之后也没召到"是两条独立的信息，
        # 少了前一条，排查会从"关键词为什么不准"开始，方向就错了。
        note = f"{note}；{said}" if note else said

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
        if _est_tokens(text) <= budget or len(picked) == 1:
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
    )


def _render(tables: list[Table], metrics: list[Metric]) -> str:
    parts = ["【可用的表】"]
    parts += [table_doc(t) for t in tables]
    if metrics:
        parts.append("\n【业务口径 —— 涉及以下概念时必须使用给定定义】")
        parts += [metric_doc(m) for m in metrics]
    return "\n\n".join(parts)
