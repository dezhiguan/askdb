"""执行期故障处置（NEEDS_OPERATOR）。

**这是三条处置链路里的第三条，与另外两条的分工**

  · 审批（approvals）—— 事前：这条查询该不该去跑。判据是扫描量，决策人是
    系统管理员，产出一张一次性的放行票。
  · 复核（reviews）—— 事后：跑出来的数字算不算数。判据是审计痕迹，决策人是
    系统管理员，产出一个采信/打回的结论。
  · 处置（本模块）—— 旁路：**这次失败与提问本身无关**。判据是 rejected_by
    == "EXEC"（库连不上、执行期出错），决策人是运维，产出的是一句
    "库恢复了，可以重试" 或 "这条恢复不了"。

为什么必须是第三条而不是并进复核：前两条判断的对象都是**这次提问**（问得该
不该跑、答得对不对），而这一条判断的对象是**系统**。让审批人去回答"库好了
没有"，等于要求一个管人的角色去读连接池日志；反过来让运维去采信一个业务数字，
更不成立。两件事的判据、决策人、证据来源没有一处重合。

**为什么此前这一档只进不出**

audit.stage() 会把 EXEC 折算成 needs_operator，界面上写着「等运维」，而系统里
既没有一个叫运维的角色，也没有任何写端点能把它标成已处理。于是它是一个纯粹
的显示标签：任务进得去、出不来。2026-09-11 补上 SRE 角色、OPS_RESOLVE 能力位
与本模块，这一档才第一次有了出口。

**两种结论，都是终局**

  · RESOLVED —— 故障已排除。发起人可以原样重试，**系统不替他重试**：
    理由与审批放行同源，重试是一次真实的模型消费与库访问，得由要这个数字的
    人自己发起，而不是由运维在不知情的情况下替他花掉一次配额。
  · WONTFIX —— 恢复不了（源已下线、表已删除）。留这一档是因为"没人处理"和
    "处理过了，结论是没救"在界面上必须分得开，否则运维每天都要重新看一遍
    同一批注定处理不了的任务。

**存储**

与 approvals / reviews 完全同构：JSONL 或库表由部署决定，一条记录一次 write，
状态靠回放推导。**不复用那两套存储**：三者都以 trace_id 为主键，挤在一张表上
会互相覆盖 —— 同一条 trace 完全可能先被审批放行、执行时又撞上库故障。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Config
from .trace import now_iso

#: 一条处置的状态。没有 REQUESTED —— 待处置是从审计痕迹（rejected_by=="EXEC"）
#: 确定性推导出来的，不需要谁去登记。理由与 reviews 完全相同：预先登记会引入
#: "痕迹在、登记没写成"这个第三态，而那个状态没有任何人能发现。
RESOLVED, WONTFIX = "RESOLVED", "WONTFIX"

#: 合法结论。接口层照着它校验，别在别处再写一份。
STATUSES = (RESOLVED, WONTFIX)


def store(cfg: Config) -> Path:
    """处置流水的位置。跟着审计日志走，与审批、复核同一条口径。"""
    return cfg.audit_log.with_name(cfg.audit_log.stem + "-ops.jsonl")


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
    # O_APPEND 单次 write：多进程并发下不会写出交错的半行
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _records(cfg: Config) -> list[dict[str, Any]]:
    """处置流水的全部事件，按写入顺序。"""
    from . import auditstore

    if auditstore.enabled(cfg):
        return auditstore.read_ops()
    return _read(store(cfg))


def _write(cfg: Config, rec: dict[str, Any]) -> None:
    """落一条处置事件。写失败要抛 —— 理由同 approvals._write：
    这次写入就是"标为已处理"这个动作本身，静默失败会让运维以为处理完了，
    而任务还挂在队列里。"""
    from . import auditstore

    if auditstore.enabled(cfg):
        auditstore.append_ops(rec)
        return
    _append(store(cfg), rec)


def state(cfg: Config) -> dict[str, dict[str, Any]]:
    """回放出每条处置的当前状态。后写的覆盖先写的。"""
    cur: dict[str, dict[str, Any]] = {}
    for rec in _records(cfg):
        rid = str(rec.get("id") or "")
        if not rid:
            continue
        cur[rid] = {**cur.get(rid, {}), **rec}
    return cur


def decided(cfg: Config) -> dict[str, str]:
    """已经有过结论的那些：trace_id → RESOLVED / WONTFIX。

    任务态那边只需要这一份映射，与 reviews.decided 同一个形状 ——
    两处的调用点紧挨着，形状不一致就会有人传错。
    """
    return {rid: str(rec.get("status") or "")
            for rid, rec in state(cfg).items()
            if rec.get("status") in STATUSES}


def resolve(cfg: Config, trace_id: str, *, operator: str, status: str,
            note: str = "", owner: str = "") -> dict[str, Any]:
    """把一条执行期故障标为已处理。只有持 OPS_RESOLVE 的人走得到这里。

    **这里没有"不得自处置"那道门**，与审批、复核有意不同：那两条判的是
    "该不该放行我自己的请求"，自己给自己盖章是实打实的利益冲突；而这一条
    判的是"库通了没有"，是一句可被任何人复验的系统事实。运维自己发起的查询
    撞上库故障、又由他自己修好并标记 —— 那恰恰是正常工作流程，挡掉它只会
    逼人换个账号点一下。

    留痕仍然完整：处置人、结论、备注、时间都进流水，事后核对得到。
    """
    if status not in STATUSES:
        raise ValueError(f"处置结论只能是 {' / '.join(STATUSES)}")
    rec = {
        "id": trace_id, "status": status, "ts": now_iso(),
        "decided_ts": now_iso(), "operator": operator, "note": note,
        "owner": owner,
    }
    _write(cfg, rec)
    return rec


def listing(cfg: Config, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """处置队列：待处置的在前，已决的在后。

    ``pending`` 由调用方从任务列表里筛出来（那边已经算过一次判定，
    这里不重算 —— 两处各算一遍就会漂）。形状与 reviews.listing 对齐。
    """
    done = state(cfg)
    rows = [{**item, "ops_status": ""} for item in pending]
    for rid, rec in done.items():
        rows.append({"trace_id": rid, "thread_id": rid,
                     "ops_status": rec.get("status"),
                     "operator": rec.get("operator"), "note": rec.get("note"),
                     "decided_ts": rec.get("decided_ts"), "owner": rec.get("owner")})
    order = {"": 0, WONTFIX: 1, RESOLVED: 2}
    return sorted(rows, key=lambda r: (order.get(str(r.get("ops_status")), 9),
                                       str(r.get("decided_ts") or r.get("ts") or "")))
