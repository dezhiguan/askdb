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


@dataclass
class StepTrace:
    step: str
    ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cached_in: int = 0          # tok_in 中命中前缀缓存的部分（含在 tok_in 里，不另计）
    cost_cny: float = 0.0       # 该步的金额，按**当次实际应答的模型**的单价算
    note: str = ""
    status: str = "ok"          # ok | blocked | failed | skipped


@dataclass
class Tracer:
    steps: list[StepTrace] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def start(self) -> float:
        return time.perf_counter()

    def add(
        self, step: str, since: float, note: str = "", status: str = "ok",
        tok_in: int = 0, tok_out: int = 0, cached_in: int = 0, cost_cny: float = 0.0,
    ) -> StepTrace:
        st = StepTrace(
            step=step,
            ms=int((time.perf_counter() - since) * 1000),
            tok_in=tok_in, tok_out=tok_out, cached_in=cached_in,
            cost_cny=cost_cny, note=note, status=status,
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
        return [asdict(s) for s in self.steps]


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
