"""高成本查询审批（P07 / 设计文档 Q-08）。

**为什么是"挂起"而不是"拒绝"**

R-11 此前对超阈值查询直接打回，附一句"缩小时间范围"。对一次性的年度对账
或全量导数来说，那是一句无解的话 —— 需求本身就是要扫那么多行。
于是人要么放弃，要么绕开 askdb 直接连库，而后者恰恰是这套系统要消灭的行为。
挂起给出第三条路：留痕、有人放行、照常在护栏里跑。

**为什么审批人是系统管理员**

它的 Policy 是空表集，永远不可能是查询的发起人，因此**自批在结构上不可能
发生** —— 不靠流程约定，靠的是 identity.DEFAULT_POLICIES 里那一行。
数据负责人反而不能批：数据源变更由它提出，兼任放行方会让"提出与放行分属
两人"失效。

**存储为什么是 JSONL**

与审计同一套办法：一条记录一次 write、O_APPEND 原子追加、状态靠回放推导。
审批记录是出事后的凭据，和审计是同一类东西，用同一种存法才不会出现
"审计还在、审批没了"。引一个数据库进来则要多一套迁移、多一处可用性依赖，
而这套系统最不该因为审批库连不上就没法查数。

**批准之后是谁在跑**

发起人自己重发一次，服务端只是放行 R-11 这一道 —— 不是服务端替他执行。
这样"以发起人的角色恢复执行"是自然成立的，不需要在服务端存任何人的凭据，
也不存在审批人不小心用自己的身份跑出别人查不到的数据这种可能。
放行是**一次性**的：消费即作废，否则一次审批等于永久豁免。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import Config
from .trace import now_iso

#: 一条审批的三种终局。REQUESTED 是初始态。
REQUESTED, APPROVED, REJECTED, CONSUMED = "REQUESTED", "APPROVED", "REJECTED", "CONSUMED"


def store(cfg: Config) -> Path:
    """审批流水的位置。跟着审计日志走 —— 同一个实例的两份凭据不该分家。"""
    return cfg.audit_log.with_name(cfg.audit_log.stem + "-approvals.jsonl")


def fingerprint(text: str) -> str:
    """请求内容的指纹。

    放行必须绑定到**具体那一条**请求上。不绑的话，拿一条小查询骗到批准、
    再用同一个 id 去跑一条全表扫描，审批就成了摆设。
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _append(path: Path, rec: dict[str, Any]) -> None:
    # 与 trace.write_audit 同一套写法：绕开缓冲层，整行一次交给内核。
    # 多副本共享同一个文件时，这是"撕不撕行"的关键 —— 审批记录不能有半行。
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 坏行跳过，不能让一行坏数据顶掉整个队列
    return out


def state(cfg: Config) -> dict[str, dict[str, Any]]:
    """回放出每条审批的当前状态。后写的覆盖先写的。

    与 audit.tasks() 按 thread_id 聚合是同一个套路：**不存可变状态，
    只存事件**。这样任何一次写入失败都只是少一个事件，不会留下一条
    半更新的记录 —— 而半更新的审批记录会让人不知道到底批没批。
    """
    cur: dict[str, dict[str, Any]] = {}
    for rec in _read(store(cfg)):
        aid = str(rec.get("id") or "")
        if not aid:
            continue
        cur[aid] = {**cur.get(aid, {}), **rec}
    return cur


def request(cfg: Config, *, trace_id: str, user: str, roles: list[str],
            kind: str, question: str, sql: str, match_text: str,
            est_rows: int | None, threshold: int, source: str = "") -> dict[str, Any]:
    """登记一条待审批。id 用 trace_id —— 审批与审计天然对得上。

    match_text 与 sql 是**两回事，别合并**：
      · sql 给审批人看，是护栏改写后真正要执行的那条（带租户谓词、LIMIT）
      · match_text 是发起人当初提交的原文，重跑时按它比对指纹

    早期版本拿改写后的 SQL 算指纹，于是批准之后原样重发一次必然对不上 ——
    因为用户手里那条永远是改写前的。这个 bug 的表现是"批了也跑不了"，
    而排查方向会被引向审批状态机，实际错在指纹取材。
    """
    rec = {
        "id": trace_id, "status": REQUESTED, "ts": now_iso(),
        "user": user, "roles": list(roles), "kind": kind,
        "question": question, "sql": sql,
        "fingerprint": fingerprint(match_text),
        "est_rows": est_rows, "threshold": threshold, "source": source,
    }
    _append(store(cfg), rec)
    return rec


def decide(cfg: Config, aid: str, *, approver: str, approved: bool,
           note: str = "") -> dict[str, Any] | None:
    """放行或驳回。只有系统管理员走得到这里（准入在 server 层判）。

    审批动作独立留痕，并记下**审批人看过这条查询的原文** —— 那是放开
    「系统管理员不看查询内容」的代价，代价要能被事后核对。
    """
    cur = state(cfg).get(aid)
    if cur is None or cur.get("status") != REQUESTED:
        return None                      # 不存在，或已经有过结论：不可重复决策
    rec = {
        "id": aid, "status": APPROVED if approved else REJECTED,
        "decided_ts": now_iso(), "approver": approver, "note": note,
        # 如实记下审批人为了判断而看到了什么
        "approver_saw_content": True,
    }
    _append(store(cfg), rec)
    return {**cur, **rec}


def waiver(cfg: Config, aid: str, *, user: str, kind: str, text: str) -> str:
    """能不能凭这条审批放行一次。返回空串表示可以，否则返回拒绝原因。

    四条都要成立，缺一条就不是"这个人这次这条请求"：
      · 存在且已批准
      · 未被用过（一次性）
      · 是本人的（别人批下来的额度不能借用）
      · 内容指纹一致（不能拿小查询换来的批准去跑大查询）
    """
    cur = state(cfg).get(aid)
    if cur is None:
        return "审批单不存在"
    if cur.get("status") == CONSUMED:
        return "该审批已使用过。审批是一次性的，请重新申请。"
    if cur.get("status") == REJECTED:
        return f"该申请已被驳回：{cur.get('note') or '未说明原因'}"
    if cur.get("status") != APPROVED:
        return "该申请尚未审批"
    if (cur.get("user") or "") != user:
        return "审批单不属于当前账号"
    if cur.get("fingerprint") != fingerprint(text):
        return "查询内容与申请时不一致，需要重新申请"
    if (cur.get("kind") or "") != kind:
        return "查询方式与申请时不一致，需要重新申请"
    return ""


def consume(cfg: Config, aid: str) -> None:
    """把放行标记为已用。**执行之后才调** —— 执行失败不该白烧一次审批。"""
    _append(store(cfg), {"id": aid, "status": CONSUMED, "consumed_ts": now_iso()})


def listing(cfg: Config, *, only_user: str | None = None) -> list[dict[str, Any]]:
    """队列。only_user 不为 None 时只给这个人自己的申请。

    与审计的可见范围同一条口径：没有 APPROVE 的人只看得到自己提的。
    """
    rows = list(state(cfg).values())
    if only_user is not None:
        rows = [r for r in rows if (r.get("user") or "") == only_user]
    # 待审批的排在最前 —— 这一页存在的意义就是"还有什么等着我处理"
    order = {REQUESTED: 0, APPROVED: 1, REJECTED: 2, CONSUMED: 3}
    return sorted(rows, key=lambda r: (order.get(r.get("status", ""), 9),
                                       str(r.get("ts", ""))), reverse=False)
