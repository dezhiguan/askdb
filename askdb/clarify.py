"""问句与推理文案的**确定性**判定。

这个模块只做一件事：把"看起来像但其实答不了"的几种形状，从靠模型自觉
变成靠代码判死。里面每一条都不问模型、不做二次推理，只对字符串和配置
做匹配 —— 因为要治的恰恰是"模型自己说自己没问题"。

2026-09-10 的 1030 次生产跑测给出的三条依据：

* **纯指代追问**：产品设计上没有多轮上下文（页面自己写着"历史查询不会
  自动进入本次上下文"），但 100 条纯指代追问里 63 条照样出了结果。最坏的
  一条是「第二名呢」—— 模型在 reasoning 里写明"无法确定排名维度……仅按
  注册时间倒序取第二条**作为占位**，口径需人工确认"，然后把那一行 users
  当答案返回，可信度 100 分。占位数据长得和答案一模一样。
* **虚构上下文**：27 条 reasoning 里出现「沿用上一轮口径」。那个"上一轮"
  不存在。这句话比错答更坏 —— 它让读的人以为这次查询承接了上文，从而
  跳过核对。
* **租户隔离的假陈述**：66 条 reasoning 说「未写 tenant_id，由系统注入」，
  而护栏改写清单里只有「注入 LIMIT 200」。在一个把"可验证"写在标题上的
  产品里，关于安全机制的错误陈述会让复核的人跳过本该检查的地方。

为什么判定要吃 Config
---------------------
「第二名呢」和「岗位JD库有多少文档」都很短、都没主语动词，区别只在于
后者点了一个**库里真实存在的实体**。实体表来自 cfg.tables / cfg.metrics，
所以判定必须吃配置 —— 拿关键词表硬编会在换一个库之后立刻误伤。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "ANAPHORA_MARKERS", "CONTEXT_CLAIM_PATTERNS", "HEDGE_PATTERNS",
    "ClarifyVerdict", "entity_vocabulary", "find_hedges", "is_anaphoric",
    "scrub_reasoning",
]

#: 指代/省略的标记词。命中其中之一**且**问句里找不到任何库内实体，才判定
#: 为"接着上一句说的"。单独命中不算 —— "这个月的问答量"里的"这"是修饰词，
#: 后面跟着实体，答得了。
ANAPHORA_MARKERS = (
    "那个", "那些", "那条", "那几", "那前", "那后", "那反", "那再", "那就", "那按", "那换",
    "这个再", "这几个再", "它们", "他们", "其中", "上面", "上述", "刚才", "刚刚", "前面那",
    "同上", "一样的", "再来", "再按", "再给", "再看", "再算", "再细", "还是按", "换成",
    "反过来", "倒过来", "接着", "继续", "另外那", "别的呢", "呢",
    "也带上", "也加上", "加上时间", "只看前", "只要前", "去掉", "细分",
)

#: 序数/名次型的省略问句。「第二名呢」「前三名呢」这类，本身不含任何实体。
_ORDINAL = re.compile(r"^第?[一二三四五六七八九十百千0-9]+(名|个|条|位|行|页)?呢?[？?。.！!]*$")

#: 断言"承接了上文"的措辞。**产品上不存在多轮上下文**，所以单步查询里
#: 任何这类句子都是虚构的，必须在输出层抹掉，而不是指望模型不写。
CONTEXT_CLAIM_PATTERNS = (
    re.compile(r"沿用上一轮[^。；;]*[。；;]?"),
    re.compile(r"沿用上一版[^。；;]*[。；;]?"),
    re.compile(r"上一轮口径[^。；;]*[。；;]?"),
    re.compile(r"承接上一轮[^。；;]*[。；;]?"),
    re.compile(r"延续上一轮[^。；;]*[。；;]?"),
    re.compile(r"如上一轮[^。；;]*[。；;]?"),
    re.compile(r"与上一轮(?:保持)?一致[^。；;]*[。；;]?"),
)

#: 声称护栏/系统会做某事的措辞。护栏做了什么由 rules_fired 说了算，
#: 不由模型转述 —— 转述会在护栏没做的时候变成假陈述。
TENANT_CLAIM_PATTERNS = (
    re.compile(r"[，,；;]?\s*(?:未|不)(?:写|加|做)[^。；;]{0,12}(?:租户|tenant)[^。；;]*[。；;]?"),
    re.compile(r"[，,；;]?\s*(?:由)?系统(?:强制)?注入[^。；;]*[。；;]?"),
    re.compile(r"[，,；;]?\s*租户隔离列[^。；;]*[。；;]?"),
    re.compile(r"[，,；;]?\s*(?:无|没有)(?:需要)?(?:涉及)?租户[^。；;]*[。；;]?"),
)

#: 猜测措辞。命中即说明这次结果是**模型自认不确定**下给出的 —— 这件事
#: 必须传导到可信度上。原来它只活在 reasoning 的自由文本里，而右栏照打 100 分。
HEDGE_PATTERNS = (
    "无法确定", "不确定", "按最常见", "默认按", "作为占位", "占位", "口径需人工确认",
    "需人工确认", "假设你指", "假设用户", "猜测", "猜想", "未指明", "未明确",
    "如需其他", "若口径不同", "请补充说明", "仅供参考", "暂按",
)


@dataclass
class ClarifyVerdict:
    """判定结果。`ask` 是要显示给用户的那句话 —— 判定为真时必须能直接用。"""

    anaphoric: bool = False
    reason: str = ""
    ask: str = ""
    hits: list[str] = field(default_factory=list)


def entity_vocabulary(cfg) -> set[str]:
    """这个库里"能被点名的东西"。

    表名、表别名、列名、列的中文描述里的词、口径名与口径别名。判定一句问话
    有没有主体，靠的就是它 —— 换一个库自动跟着变，不用维护关键词表。
    """
    vocab: set[str] = set()
    for t in getattr(cfg, "tables", {}).values():
        vocab.add(t.name.lower())
        for a in (t.aliases or []):
            if a:
                vocab.add(str(a).lower())
        for c in t.columns.values():
            vocab.add(c.name.lower())
            # 列描述取前一段中文，"文档数缓存计数器。⚠️…" → "文档数缓存计数器"
            head = re.split(r"[。．.，,（(]", str(c.desc or ""))[0].strip()
            if 2 <= len(head) <= 12:
                vocab.add(head.lower())
    for m in getattr(cfg, "metrics", []) or []:
        vocab.add(m.name.lower())
        for a in (m.aliases or []):
            if a:
                vocab.add(str(a).lower())
    return {v for v in vocab if len(v) >= 2}


#: 即便问句里没有库内实体，这些词也说明它在问一个**具体的东西**，
#: 不是在接着上一句说。放宽一点，避免把正常的冷启动问题判成追问。
_SELF_CONTAINED = (
    "多少", "几个", "总数", "数量", "哪些", "哪个", "列出", "统计", "占比", "比例",
    "平均", "最大", "最小", "趋势", "分布", "排名", "top", "汇总", "合计",
)


def is_anaphoric(question: str, cfg) -> ClarifyVerdict:
    """这句问话是不是"接着上一句说的"。

    判真的条件刻意收得很紧，三条同时成立才算：

    1. 短（去掉标点后不超过 16 个字）—— 长句几乎总会带上主体；
    2. 命中指代标记，或整句就是一个序数（「第二名呢」）；
    3. 问句里**找不到任何库内实体**，也没有自足的疑问词。

    收紧的理由是误伤的代价不对称：漏判一条追问，用户得到一个可疑答案；
    误判一条正常问题，用户得到一次莫名其妙的拒答。前者能靠可信度扣分兜底，
    后者直接是功能缺失。
    """
    q = (question or "").strip()
    flat = re.sub(r"[\s，,。.、；;！!？?：:~～·]+", "", q)
    if not flat:
        return ClarifyVerdict()
    if len(flat) > 16:
        return ClarifyVerdict()

    low = flat.lower()
    vocab = entity_vocabulary(cfg)
    if any(v in low for v in vocab):
        return ClarifyVerdict()

    ordinal = bool(_ORDINAL.match(q.strip()))
    marker = next((m for m in ANAPHORA_MARKERS if m in flat), "")
    if not ordinal and not marker:
        return ClarifyVerdict()
    # 序数句（「第二名呢」）本身就没有主体，自足疑问词救不了它。
    if not ordinal and any(w in low for w in _SELF_CONTAINED):
        return ClarifyVerdict()

    why = "这是一个序数追问，没有说明排的是什么" if ordinal else \
          f"问句里的「{marker}」指向上一次查询"
    return ClarifyVerdict(
        anaphoric=True,
        reason=why,
        ask=("每次查询都是独立的，历史查询不会进入本次上下文，"
             "所以这里没有「上一次」可以承接。"
             "请把要查的对象和口径写进问题里，"
             "例如把「第二名呢」写成「按文档数排名第二的知识库是哪个」。"),
        hits=[m for m in ANAPHORA_MARKERS if m in flat] or (["<序数>"] if ordinal else []),
    )


def find_hedges(reasoning: str) -> list[str]:
    """推理里出现的猜测措辞。空列表 = 模型没有自认不确定。"""
    text = reasoning or ""
    return [p for p in HEDGE_PATTERNS if p in text]


def scrub_reasoning(reasoning: str, *, multi_step: bool = False,
                    tenant_injected: bool = False) -> tuple[str, list[str]]:
    """抹掉推理里**与事实不符**的两类句子，返回 (清理后的文本, 抹掉了什么)。

    - 承接上文的断言：单步查询里不存在"上一轮"，一律抹掉。多步链路里确有
      前一步，这时保留（改写成"上一步"由提示词负责，这里不动）。
    - 护栏行为的转述：只有护栏**真的注入了**租户谓词时才允许留下这类句子。
      判据是调用方给的 tenant_injected，不是模型的说法。

    抹掉而不是拒答：这些句子挂在一条本身可能完全正确的 SQL 上，为一句多余的
    话把结果丢掉不划算。但它们必须消失 —— 留着就是在替护栏背书。
    """
    text = reasoning or ""
    removed: list[str] = []
    pats: list[re.Pattern[str]] = []
    if not multi_step:
        pats.extend(CONTEXT_CLAIM_PATTERNS)
    if not tenant_injected:
        pats.extend(TENANT_CLAIM_PATTERNS)
    for p in pats:
        for m in p.finditer(text):
            frag = m.group(0).strip()
            if frag:
                removed.append(frag)
        text = p.sub("", text)
    # 抹完可能留下悬空的标点或空句
    text = re.sub(r"[，,；;]\s*(?=[，,；;。.])", "", text).strip()
    text = re.sub(r"^[，,；;。.]+", "", text).strip()
    text = re.sub(r"[，,；;]+$", "。", text).strip()
    return text, removed
