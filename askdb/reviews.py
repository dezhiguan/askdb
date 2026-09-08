"""结果复核（WAITING_REVIEW）。

**与审批是两件事，别合并。**

  · 审批（approvals）是**事前**的：这条查询该不该去跑。判据是护栏 R-11
    的扫描量，决策发生在执行之前，放行是一张一次性的票，用过即作废。
  · 复核（本模块）是**事后**的：这条已经跑完了、也返回了数字，但**这个数字
    可信吗**。决策发生在执行之后，不放行任何东西，只决定这次结果采不采信。

两者的决策人恰好都是系统管理员、动作恰好都是"放行 / 打回"，但触发时机与
判定对象完全不同。若哪天连触发条件也合并了，那就该删掉其中一套 —— 两套
做同一件事的机制，最后的结局是两边都没人维护。

**为什么需要它**

这套系统最危险的失败不是被拦下，是**看起来成功的错答**：链路每一层都做对了
自己那件事，最后给出一个语气笃定的错数字。2026-09-07 实测过一次：问"一共有
多少个用户"，召回给的是 agent_messages，模型老老实实按给的表算了个
COUNT(DISTINCT user_id)，返回 6512，而真值是 users 表的 10084 —— 没有任何
一层报错。

这类结果在系统里留下的**是有痕迹的**（盲选召回、脱敏退化、反复重试后才收敛、
token 触顶收敛），痕迹已经在审计记录里，此前只是没人接。复核就是把这些痕迹
接成一个待办：由系统管理员看一眼，采信或打回。

**判据是确定性的，不是模型打分**

与 audit._risk 同一套做法：规则写在代码里、理由随值一起返回，页面可解释。
不引入"可信度分数"这种东西 —— 一个 0~100 的分数说不清它凭什么，
而复核这件事的全部意义就是**说得清**。

**存储**

与 approvals 同一套：JSONL、一条记录一次 write、状态靠回放推导。理由见
approvals 模块开头那段 —— 复核结论和审计是同一类凭据，用同一种存法才不会
出现"审计还在、复核没了"。**不复用 approvals 的存储**：那边以 trace_id 为
主键，同一条 trace 完全可能既有过审批又需要复核，挤在一个键上就会互相覆盖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Config
from .trace import now_iso

#: 一条复核的三种状态。REQUESTED 是初始态（由痕迹自动推导，不需要谁去登记）。
REQUESTED, ACCEPTED, RETURNED = "REQUESTED", "ACCEPTED", "RETURNED"


def store(cfg: Config) -> Path:
    """复核流水的位置。跟着审计日志走，与 approvals 同一条口径。"""
    return cfg.audit_log.with_name(cfg.audit_log.stem + "-reviews.jsonl")


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                      # 撕裂行：跳过，不中断
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
    return out


def _append(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    # O_APPEND 单次 write：与审计同一条口径，多进程并发下不会写出交错的半行
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _records(cfg: Config) -> list[dict[str, Any]]:
    """复核流水的全部事件，按写入顺序。库或文件由部署决定（与审批同一开关）。"""
    from . import auditstore

    if auditstore.enabled(cfg):
        return auditstore.read_reviews()
    return _read(store(cfg))


def _write(cfg: Config, rec: dict[str, Any]) -> None:
    """落一条复核事件。写失败要抛 —— 理由同 approvals._write。"""
    from . import auditstore

    if auditstore.enabled(cfg):
        auditstore.append_review(rec)
        return
    _append(store(cfg), rec)


def state(cfg: Config) -> dict[str, dict[str, Any]]:
    """回放出每条复核的当前状态。后写的覆盖先写的。"""
    cur: dict[str, dict[str, Any]] = {}
    for rec in _records(cfg):
        rid = str(rec.get("id") or "")
        if not rid:
            continue
        cur[rid] = {**cur.get(rid, {}), **rec}
    return cur


def decided(cfg: Config) -> dict[str, str]:
    """已经有过结论的那些：trace_id → ACCEPTED / RETURNED。

    任务态那边只需要这一份映射：**没有结论的就还在等复核**，
    不需要预先为每条存疑结果登记一条 REQUESTED —— 待复核是从审计痕迹
    推导出来的，登记反而会引入"痕迹在、登记没写成"的第三种状态。
    """
    return {rid: str(rec.get("status") or "")
            for rid, rec in state(cfg).items()
            if rec.get("status") in (ACCEPTED, RETURNED)}


class SelfReview(RuntimeError):
    """发起人试图复核自己的结果。理由同 approvals.SelfApproval。"""


def decide(cfg: Config, trace_id: str, *, reviewer: str, accepted: bool,
           note: str = "", owner: str = "") -> dict[str, Any]:
    """采信或打回。只有系统管理员走得到这里（准入在 server 层判）。

    与审批同一条自查规则：**发起人不得复核自己的结果**。理由完全相同 ——
    系统管理员现在也能查数，于是同一个人既可能是结果的发起人、又是唯一
    有 APPROVE 的角色；不显式挡一次，"复核"就成了自己给自己盖章。

    打回**不撤销已经发生的事**：数字已经返回给发起人了，复核改变的是
    这条记录此后的可信标记与任务态。想真正阻止结果外流，那是事前审批的
    职责，不是这里 —— 两件事别混。
    """
    if owner and owner == reviewer:
        raise SelfReview("不能复核自己发起的查询结果。请由另一位系统管理员处理。")
    rec = {
        "id": trace_id,
        "status": ACCEPTED if accepted else RETURNED,
        "decided_ts": now_iso(), "reviewer": reviewer, "note": note,
        "owner": owner,
    }
    _write(cfg, rec)
    return rec


def listing(cfg: Config, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """复核队列：待复核的在前，已决的在后。

    ``pending`` 由调用方从任务列表里筛出来（那边已经算过一次待复核判定，
    这里不重算 —— 两处各算一遍就会漂）。
    """
    done = state(cfg)
    rows = [{**item, "review_status": REQUESTED} for item in pending]
    for rid, rec in done.items():
        rows.append({"trace_id": rid, "thread_id": rid,
                     "review_status": rec.get("status"),
                     "reviewer": rec.get("reviewer"), "note": rec.get("note"),
                     "decided_ts": rec.get("decided_ts"), "owner": rec.get("owner")})
    order = {REQUESTED: 0, RETURNED: 1, ACCEPTED: 2}
    return sorted(rows, key=lambda r: (order.get(str(r.get("review_status")), 9),
                                       str(r.get("decided_ts") or r.get("ts") or "")))
