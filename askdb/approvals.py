"""高成本查询审批（P07 / 设计文档 Q-08）。

**为什么是"挂起"而不是"拒绝"**

R-11 此前对超阈值查询直接打回，附一句"缩小时间范围"。对一次性的年度对账
或全量导数来说，那是一句无解的话 —— 需求本身就是要扫那么多行。
于是人要么放弃，要么绕开 askdb 直接连库，而后者恰恰是这套系统要消灭的行为。
挂起给出第三条路：留痕、有人放行、照常在护栏里跑。

**为什么审批人是系统管理员**

数据负责人不能批：数据源变更由它提出，兼任放行方会让"提出与放行分属两人"
失效。所以放行方收敛到一个不提需求的角色上。

自批过去是**结构上不可能**的：系统管理员的 Policy 是空表集，一张表都查不到，
因此不可能是任何一条审批单的发起人。2026-09-06 产品决定所有角色可见面一致、
系统管理员也能查数，那个前提没有了 —— 自批改由 decide() 里的显式判定挡住。
一条隐式保证换成了一条显式判定，后者会被绕过、前者不会，所以改审批链路前
先读那一处。

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
from datetime import datetime, timedelta
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


def _records(cfg: Config) -> list[dict[str, Any]]:
    """审批流水的全部事件，按写入顺序。库或文件由部署决定。"""
    from . import auditstore

    if auditstore.enabled(cfg):
        return auditstore.read_approvals()
    return _read(store(cfg))


def _write(cfg: Config, rec: dict[str, Any]) -> None:
    """落一条审批事件。

    **写失败要抛**（与审计相反）：审计是旁路，丢一条是损失；而这里的写入
    就是"批准/驳回"这个操作本身 —— 静默失败会让人以为批过了，而流水里没有。
    """
    from . import auditstore

    if auditstore.enabled(cfg):
        auditstore.append_approval(rec)
        return
    _append(store(cfg), rec)


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
    for rec in _records(cfg):
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
    _write(cfg, rec)
    return rec


class SelfApproval(RuntimeError):
    """发起人试图审批自己的查询。见 decide() 里那段注释。"""


def decide(cfg: Config, aid: str, *, approver: str, approved: bool,
           note: str = "") -> dict[str, Any] | None:
    """放行或驳回。只有系统管理员走得到这里（准入在 server 层判）。

    审批动作独立留痕，并记下**审批人看过这条查询的原文** —— 那是放开
    「系统管理员不看查询内容」的代价，代价要能被事后核对。
    """
    cur = state(cfg).get(aid)
    if cur is None or cur.get("status") != REQUESTED:
        return None                      # 不存在，或已经有过结论：不可重复决策
    # 发起人不得自批。
    #
    # 这条判定 2026-09-06 才补上，补的是一个刚刚消失的结构性保证：此前
    # SYS_ADMIN 的策略是空表集（一张表都查不到），因此它**不可能**是任何
    # 一条审批单的发起人，自批在结构上不成立，不需要判。产品决定系统管理员
    # 也能查数之后，那个前提没有了 —— 同一个人现在既能触发 R-11 开出审批单，
    # 又是唯一有 APPROVE 的角色。
    #
    # 返回 None 与"审批单不存在"同一条出路：不向调用方区分这两种情形。
    if (cur.get("user") or "") and (cur.get("user") or "") == approver:
        raise SelfApproval("不能审批自己发起的查询。请由另一位系统管理员处理。")
    rec = {
        "id": aid, "status": APPROVED if approved else REJECTED,
        "decided_ts": now_iso(), "approver": approver, "note": note,
        # 如实记下审批人为了判断而看到了什么
        "approver_saw_content": True,
    }
    _write(cfg, rec)
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
    _write(cfg, {"id": aid, "status": CONSUMED, "consumed_ts": now_iso()})


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


def _ts(value: Any) -> datetime | None:
    """审批流水里的时间戳。写入方永远是 now_iso()，所以正常情况一定解析得出；
    解析不出就当没有 —— 宁可少算一条，不要拿一个假时间去算耗时。
    """
    try:
        return datetime.fromisoformat(str(value or ""))
    except (ValueError, TypeError):
        return None


def summary(cfg: Config, *, days: int, only_user: str | None = None) -> dict[str, Any]:
    """审批的窗口汇总。给审计中心那张卡用，与 listing() 分工：
    那个给审批队列页，要的是每一条；这里只要三个数。

    **两个数的时间口径不同，是有意的：**

      · ``pending`` 是**当前**未决数，不受窗口约束，与任务中心「等待审批」
        同源（都是 REQUESTED 的净状态）。挂了三个月没人批的单子正是这张卡
        该喊出来的东西，按窗口滤掉等于越久越看不见。
      · ``decided`` / ``avg_decide_ms`` 按**决策时刻**切窗口 —— 问的是
        "最近这些天批下来的平均花了多久"。按申请时刻切会把窗口外提的、
        窗口内批的那些漏掉，而它们恰恰是耗时最长的样本。

    only_user 必须与 /api/audit/stats 的 only_user 一起收敛：同一页上三张卡
    只给本人、审批那张给全量，就是一次可见范围泄露（谁在申请跑大查询、
    被驳回几次，一眼可见）。

    没有已决样本时 avg_decide_ms 为 None —— 不是 0。0 会被读成"秒批"。
    """
    rows = list(state(cfg).values())
    if only_user is not None:
        rows = [r for r in rows if (r.get("user") or "") == only_user]

    cut = datetime.now().astimezone() - timedelta(days=days)
    pending = sum(1 for r in rows if r.get("status") == REQUESTED)
    decided = 0
    waits: list[int] = []
    for r in rows:
        if r.get("status") == REQUESTED:
            continue
        end = _ts(r.get("decided_ts"))
        if end is None or end < cut:
            continue
        decided += 1
        # 申请时刻取 REQUESTED 那条留下的 ts。decide() 写的记录里没有 ts 键，
        # 靠 state() 的字典合并保住它 —— 别在 decide() 里补写 ts，那会把
        # 申请时刻静默改成决策时刻，平均耗时直接变 0 且不会报错。
        start = _ts(r.get("ts"))
        if start is not None and end >= start:
            waits.append(int((end - start).total_seconds() * 1000))

    return {
        "pending": pending,
        "decided": decided,
        "avg_decide_ms": round(sum(waits) / len(waits)) if waits else None,
    }
