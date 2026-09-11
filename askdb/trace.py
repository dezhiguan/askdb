"""步骤级追踪、成本归因与审计落盘。

成本归因到**步骤**而非整次调用 —— 这是判断"钱花在哪一步"的前提，
也是消融实验中成本对比的数据来源（技术设计说明书 §7）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


#: Span 状态口径 —— 三档，一处定义。
#:
#: 原来只有 ok/blocked/failed/skipped 四个值在用，而页面上实际只区分
#: 「等于 ok」与「不等于 ok」两态。于是两类信息一起丢了：主模型失败后被
#: 重试救回来的那条链路，和一次就成的干净链路，长得一模一样；向量召回
#: 回落关键词这种「跑成了但能力降级了」的情况，只能算成成功。
#:
#: OK 档   = 按主路径完成（hit 是命中应答缓存，那是一次正常收尾）
#: SOFT 档 = 有产出，但不是主路径的产出，或产出本身需要存疑
#: 其余    = 硬失败（failed / blocked / skipped）
OK_STATUSES = frozenset({"ok", "hit"})
SOFT_STATUSES = frozenset({
    "fallback",     # 由备选模型 / 重试救回来的产出
    "degraded",     # 产出了，但能力低于主路径（如向量召回回落关键词）
    "empty",        # 执行成功但零行
})


def step_failed(status: str) -> bool:
    """这一步是不是**硬失败**。

    SOFT 档不算失败 —— 把 fallback 算成失败，「模型调用成功率」会在切备选
    成功时反而下跌；把 empty 算成失败，一次如实返回零行的查询会变成故障。
    但它们也不是 ok，那正是这三个值存在的理由。
    """
    return status not in OK_STATUSES and status not in SOFT_STATUSES


@dataclass
class StepTrace:
    step: str
    ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cached_in: int = 0          # tok_in 中命中前缀缓存的部分（含在 tok_in 里，不另计）
    cost_cny: float = 0.0       # 该步的金额，按**当次实际应答的模型**的单价算
    note: str = ""
    status: str = "ok"          # 取值见本模块开头的三档口径
    #: 这一步的第几次尝试 / 共几次。**失败的尝试各占一条 StepTrace**，
    #: 不被成功的那次覆盖 —— 覆盖掉的话，"重试救回来了"这件事在库里
    #: 就不存在，前端再怎么改也渲染不出来。0 表示这一步只跑了一次。
    attempt: int = 0
    attempts_total: int = 0
    #: **实际应答的模型**，按次记。整条链路一个 model 字段是不够的：
    #: 主模型超时切备选时，那个字段记的是配置里的主模型，与真正出活的
    #: 那个不是同一个，账也跟着记错。
    model: str = ""
    #: 失败时的厂商错误码 / 异常类名。空字符串表示这一步没失败。
    #: **不含任何内容**，因此可以随 /api/trace 出接口。
    error_code: str = ""
    #: 厂商回的原始错误消息，原样保留不截断。
    #: **有意与 note 分开**：厂商的 4xx 消息可能回显请求片段（提示词里带着
    #: 表结构与用户的问题），而 /api/trace 是免登录可读的、刻意不放 SQL 与
    #: 问题原文。所以它不进 STEP_FIELDS —— 只留在审计记录与 /api/replay
    #: （要登录、要开关）那条路上，与 sql_raw/question 同一道边界。
    error_message: str = ""
    #: 失败后做了什么 —— 切备选、就地重试、回落备用路径、放行。
    #: 没有它，一条 failed 的 span 只说明"这里断过"，说不清链路怎么活下来的。
    disposition: str = ""
    #: agentic 链路里这一步调用的**具体工具名**（search_schema / get_table_schema /
    #: execute_sql）。此前工具名只藏在 note 前缀里、靠 "·" 分割不稳，追踪页因此
    #: 只能显示"工具调用"。结构化单列出来，前端流程条与 Span 列可直接显示是哪个工具。
    #: 不含内容，可随 /api/trace 出接口。
    tool: str = ""
    #: 该步涉及的表名。目前只有 schema_recall 填：note 里的"命中 N 张表"是个
    #: 数字，而看的人真正要判断的是**哪 N 张** —— 召回偏了与召回对了，在那个
    #: 数字上完全一样。放结构化字段而不是拼进 note，是因为界面要能逐张列出，
    #: 也因为 note 会被 token 预算之外的其他信息挤长。
    tables: list[str] = field(default_factory=list)


@dataclass
class Tracer:
    steps: list[StepTrace] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def start(self) -> float:
        return time.perf_counter()

    def add(
        self, step: str, since: float, note: str = "", status: str = "ok",
        tok_in: int = 0, tok_out: int = 0, cached_in: int = 0, cost_cny: float = 0.0,
        tables: list[str] | None = None, ms: int | None = None,
        attempt: int = 0, attempts_total: int = 0, model: str = "",
        error_code: str = "", error_message: str = "", disposition: str = "",
        tool: str = "",
    ) -> StepTrace:
        """ms 显式传入时不按 since 算 —— 一个节点落多条 span（每次尝试一条）
        时，since 是**整个节点**的起点，拿它算每一条就等于给每次尝试都记上
        全节点的耗时，几条加起来远超总耗时。只有单条 span 的节点才用 since。
        """
        st = StepTrace(
            step=step,
            ms=int((time.perf_counter() - since) * 1000) if ms is None else int(ms),
            tok_in=tok_in, tok_out=tok_out, cached_in=cached_in,
            cost_cny=cost_cny, note=note, status=status,
            tables=list(tables or []),
            attempt=attempt, attempts_total=attempts_total, model=model,
            error_code=error_code, error_message=error_message,
            disposition=disposition, tool=tool,
        )
        self.steps.append(st)
        return st

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    @property
    def tok_in(self) -> int:
        return sum(s.tok_in for s in self.steps)

    @property
    def tok_out(self) -> int:
        return sum(s.tok_out for s in self.steps)

    @property
    def cached_in(self) -> int:
        return sum(s.cached_in for s in self.steps)

    @property
    def cost_cny(self) -> float:
        """一次问答的总金额 = 各步金额之和。

        必须逐步累加而不是拿总 token 乘单价：一次问答里的多次调用可能由**不同
        模型**应答（兜底切备选），也可能落在不同计费时段（跨过高峰边界），
        单价并非全程一致。
        """
        return round(sum(s.cost_cny for s in self.steps), 6)

    def as_list(self) -> list[dict[str, Any]]:
        # tables 只有 schema_recall 填，其余步骤是空列表。审计是**逐条追加**的
        # 存储，每条多带五六个 "tables": [] 会一路乘进文件与库里，故落盘前去掉。
        out = []
        for s in self.steps:
            d = asdict(s)
            # 同理，新增的五个字段绝大多数步骤都用不上（只跑一次、没失败），
            # 空值一律不落盘，免得每条审计凭空胖五个键。
            for k in ("tables", "attempt", "attempts_total", "model",
                      "error_code", "error_message", "disposition", "tool"):
                if not d.get(k):
                    d.pop(k, None)
            out.append(d)
        return out


def peak_multiplier(llm_cfg: dict[str, Any], at: datetime | None = None) -> float:
    """当前时刻的价格倍率：高峰 1.0，空闲 offpeak_multiplier。

    DeepSeek 直连按峰谷两档计费，空闲价是高峰价的一半，高峰是北京时间周一至
    周五 9:00-12:00 与 14:00-18:00。不配 peak_windows 的厂商（百炼就是）恒为
    1.0 —— 没有峰谷这回事，不要给它凭空造一个折扣出来。

    单价按**高峰**填在配置里，这里只做向下打折。反过来（按空闲填、高峰加价）
    会让漏配 peak_windows 的后果变成系统性低估，那是更坏的失败方向。
    """
    windows = llm_cfg.get("peak_windows") or []
    if not windows:
        return 1.0
    off = float(llm_cfg.get("offpeak_multiplier", 1.0))
    tz = timezone(timedelta(hours=float(llm_cfg.get("peak_utc_offset_hours", 8))))
    now = (at or datetime.now(timezone.utc)).astimezone(tz)
    if llm_cfg.get("peak_weekdays_only", True) and now.weekday() >= 5:
        return off
    minutes = now.hour * 60 + now.minute
    for w in windows:
        try:
            start, end = (part.strip() for part in str(w).split("-"))
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except (ValueError, TypeError):
            continue          # 配错的时段当作不存在，宁可按高峰算（偏贵不偏便宜）
        if sh * 60 + sm <= minutes < eh * 60 + em:
            return 1.0
    return off


def call_cost_cny(
    tok_in: int, tok_out: int, cached_in: int, llm_cfg: dict[str, Any],
    at: datetime | None = None,
) -> float:
    """**一次模型调用**的金额。传进来的 llm_cfg 必须是实际应答那个模型的配置。

    三件按真实账单口径做的事：
    1. 命中前缀缓存的输入按缓存价计。cached_in 是厂商回传的实测值
       （usage_metadata.input_token_details.cache_read），不是估的 ——
       实测 DeepSeek 直连命中率可达 867 中 768，而命中价是未命中的 1/30，
       不区分的话输入成本会高估几倍。
    2. 按调用当刻的计费时段打折（见 peak_multiplier）。
    3. 由调用方传入应答模型自己的单价，兜底切备选时不再按主模型价记账。

    tok_in 含 cached_in（两家的 prompt_tokens 都是命中+未命中的总数，
    实测 deepseek 回传 hit 768 + miss 99 = in 867），所以未命中量要相减得出，
    不能把 cached_in 再加一遍。
    """
    p_in = float(llm_cfg.get("price_input_per_1k", 0.0))
    p_out = float(llm_cfg.get("price_output_per_1k", 0.0))
    # 不配缓存价就退回按未命中价计 —— 宁可高估，不要凭空给个便宜价
    p_cached = float(llm_cfg.get("price_cached_input_per_1k", p_in))
    cached = max(0, min(int(cached_in or 0), int(tok_in or 0)))
    miss = int(tok_in or 0) - cached
    amount = (miss / 1000 * p_in + cached / 1000 * p_cached + int(tok_out or 0) / 1000 * p_out)
    return round(amount * peak_multiplier(llm_cfg, at), 6)


def embed_cost_cny(tokens: int, schema_rag_cfg: dict[str, Any]) -> float:
    """一次 embedding 调用的金额。**只按输入计**——嵌入没有输出 token。

    单价必须由配置给出，漏配就是 0 元。这里**不设默认价**：默认一个
    看起来很像的数，账面就会一直是对不上的，而没人会去核对一个
    "看起来合理"的数字。0 元在成本页上是显眼的，会被人问起来。
    """
    price = float(schema_rag_cfg.get("embedding_price_per_1k", 0.0) or 0.0)
    return round(max(0, int(tokens or 0)) / 1000 * price, 6)


def cost_cny(tok_in: int, tok_out: int, llm_cfg: dict[str, Any]) -> float:
    """全部未命中缓存、按当前计费时段的粗算 —— 只用于事前估算与展示。

    真实记账走 call_cost_cny（按次、按实际应答模型、按实测缓存命中量），
    审计里的 cost_cny 是各步之和，不是这个函数算出来的。
    """
    return call_cost_cny(tok_in, tok_out, 0, llm_cfg)


def now_iso() -> str:
    """带时区的本地时间戳。审计记录没有时间等于没有审计。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_audit(target: Any, record: dict[str, Any]) -> None:
    """落一条审计。写失败不能影响主链路 —— 查询已经完成了。

    **target 是 Config 就写库，是 Path 就写文件。** 2026-09-09 起生产走
    PostgreSQL（见 auditstore 模块开头那段：共享 hostPath 的前提没有任何
    调度约束在守，且文件方案只增不减、每次请求全量读）。文件这条路留着，
    本机开发与样例配置仍然用它 —— 那时想要的就是一个能 grep、能删的文件。

    文件写法保持原样：多副本共享同一个文件时，"一条记录一次 write" 是撕不撕
    行的关键 —— 带缓冲的写可能把一条记录拆成多次系统调用，两个进程的片段
    交错落盘，整行 JSON 就废了。O_APPEND 下单次 write 的定位与写入是原子的，
    所以这里绕开 Python 的缓冲层，把整行一次性交给内核。
    """
    if not isinstance(target, Path):
        from . import auditstore

        if auditstore.enabled(target):
            auditstore.append_audit(record)
            return
        target = target.audit_log

    path = target
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # 循环写到写完：os.write 允许短写（信号打断、磁盘写满），
            # 不管返回值就会留下半行，而读侧对半行只能跳过 —— 表现为
            # "审计悄悄少了一条"，比写失败更难发现
            while line:
                line = line[os.write(fd, line):]
        finally:
            os.close(fd)
    except OSError:
        pass


def langsmith_status() -> dict[str, Any]:
    """LangSmith 观测是否启用 —— 只读环境变量，不发探测请求。

    只如实报告 enabled / project 两件事。不报"上报成功率"：上报是
    langchain 内部的异步旁路，进程里量不出真实成功率，编一个 100%
    出来就是在审计页上撒谎。
    """
    flag = os.environ.get(
        "LANGSMITH_TRACING", os.environ.get("LANGCHAIN_TRACING_V2", "")
    ).strip().lower()
    enabled = flag in ("1", "true", "yes")
    project = (os.environ.get("LANGSMITH_PROJECT")
               or os.environ.get("LANGCHAIN_PROJECT") or "default")
    return {"enabled": enabled, "project": project if enabled else None}


def observability_status() -> dict[str, Any]:
    """观测后端状态：Langfuse（自托管）优先，其次 LangSmith（云）。

    只认环境变量、不发探测请求。国内机房到 LangSmith 云出网未必通，
    自托管 Langfuse 是默认推荐；两者都配了按 Langfuse 算。
    """
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        host = os.environ.get("LANGFUSE_HOST", "")
        return {
            "backend": "langfuse", "enabled": True,
            "project": os.environ.get("LANGFUSE_PROJECT", "askdb-prod"),
            "host": host,
            # 页面跳转地址与上报地址分离：自托管实例常常只在内网可达，
            # 浏览器侧经隧道访问（如 localhost:3000）。没配就退回上报地址。
            "url": os.environ.get("LANGFUSE_PUBLIC_URL", host),
        }
    ls = langsmith_status()
    if ls["enabled"]:
        return {"backend": "langsmith", "enabled": True, "project": ls["project"],
                "host": "https://smith.langchain.com",
                "url": "https://smith.langchain.com"}
    return {"backend": None, "enabled": False, "project": None, "host": "", "url": ""}
