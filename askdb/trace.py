"""步骤级追踪、成本归因与审计落盘。

成本归因到**步骤**而非整次调用 —— 这是判断"钱花在哪一步"的前提，
也是消融实验中成本对比的数据来源（技术设计说明书 §7）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class StepTrace:
    step: str
    ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
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
        tok_in: int = 0, tok_out: int = 0,
    ) -> StepTrace:
        st = StepTrace(
            step=step,
            ms=int((time.perf_counter() - since) * 1000),
            tok_in=tok_in, tok_out=tok_out, note=note, status=status,
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

    def as_list(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.steps]


def cost_cny(tok_in: int, tok_out: int, llm_cfg: dict[str, Any]) -> float:
    p_in = float(llm_cfg.get("price_input_per_1k", 0.0))
    p_out = float(llm_cfg.get("price_output_per_1k", 0.0))
    return round(tok_in / 1000 * p_in + tok_out / 1000 * p_out, 6)


def now_iso() -> str:
    """带时区的本地时间戳。审计记录没有时间等于没有审计。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_audit(path: Path, record: dict[str, Any]) -> None:
    """审计记录追加落盘。写失败不能影响主链路 —— 查询已经完成了。

    多副本共享同一个审计文件时，"一条记录一次 write" 是撕不撕行的关键：
    带缓冲的写可能把一条记录拆成多次系统调用，两个进程的片段交错落盘，
    整行 JSON 就废了 —— 而审计恰恰是出事后唯一的凭据，不能有半行。
    O_APPEND 下单次 write 的定位与写入是原子的，所以这里绕开 Python 的
    缓冲层，把整行一次性交给内核。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass
