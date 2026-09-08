"""审计流水的读取与聚合 —— 只读，不产生任何写入。

审计文件是多副本经 O_APPEND 共享追加的 JSONL（见 trace.write_audit），
这里是它唯一的消费入口：流水分页、关键词检索、时间窗统计。

两条纪律：
- **列表接口的摘要有意不含 SQL 文本与结果行。** 流水页是常开页面，
  SQL 细节只允许经 /api/replay 的字段白名单 + 配置开关出去
  （判定链路回放接口设计说明 §4.2 / §5.2）。
- 个别坏行（进程被杀时的半行）跳过而不是报错 —— 审计恰恰是出事后
  要看的页面，不能因为一次事故写坏一行就整页打不开。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# 出现在流水列表里的字段。白名单式：新加字段须显式列入，
# 避免未来往审计记录里塞了敏感字段后被列表接口顺手带出去。
SUMMARY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "role", "user", "question", "rejected_by",
    "attempts", "rows_returned", "elapsed_ms", "cost_cny",
    "step_count", "multi_step", "source", "source_name",
)

# /api/trace 的字段白名单：执行追踪页要的是**节点链与计量**。
# 与 REPLAY_FIELDS 的分界是刻意的 —— 这里不给 sql_raw / sql_final / question /
# tables_hit，SQL 文本、问题原文与命中表仍然只经 /api/replay 出去（要登录、
# 要开关、还要按调用者当下的可见表收窄）。步骤 note 里会出现表名，所以
# /api/trace 同样做那道可见表收窄，只是不返回 tables_hit 本身。
TRACE_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "role", "model",
    "tok_in", "tok_out", "step_count", "multi_step", "attempts",
    "elapsed_ms", "cost_cny", "rejected_by", "source", "source_name",
)

# 步骤对象自身也走白名单 —— 记录里的 steps 由各节点自由追加，
# 哪天有人往里塞了 sql 或行样本，这里不会顺手带出去。
STEP_FIELDS = ("step", "status", "ms", "tok_in", "tok_out", "note")

# 真正过模型的图节点。与前端 traceSteps.ts 的 STEP_TYPE == 'MODEL' 是同一份口径，
# 两边都写一次是因为一个算数、一个只做展示；漂了会让「模型调用成功率」这格
# 与页面上标 MODEL 的那些 span 对不上 —— tests 里钉住了两边一致。
MODEL_STEPS = frozenset({"plan", "generate_sql", "assess", "reflect"})


# /api/replay 的字段白名单（判定链路回放接口设计说明 §4.2）。
# rows / schema_prompt 两个字段在设计上**绝不出接口** —— 用白名单而不是
# 黑名单：漏给一个无害字段是体验问题，漏挡一个敏感字段是事故。
REPLAY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "role", "user", "question",
    "tables_hit", "metrics_hit", "sql_raw", "sql_final",
    "rules_fired", "rejected_by", "attempts", "explain_rows",
    "step_count", "multi_step", "converged_early", "rows_returned",
    "elapsed_ms", "tok_in", "tok_out", "cost_cny", "steps",
    "source", "source_name",
)


#: 发起时先落的那条记录的标记。**它不是一次调用的结果，只是一个占位**：
#: 说明"这条线程存在、归谁、打哪个库"，收尾时另有一条带结果的记录。
#:
#: 为什么要有它：2026-09-07 实测 kill -9 打在查询中途 —— 检查点写了
#: （现场在），审计一条没写。任务中心完全由审计构建，于是这条任务从系统里
#: 彻底消失；即便手里有 thread_id 去续跑也走不通，因为**数据源只记在审计里**，
#: 读不到就退回内置源。而"进程挂了"恰恰是断点续跑唯一的真实用例。
#: 补上这条记录之后，同一个线程立刻变回 interrupted / resumable 并真的续上了
#: （节点从 generate_sql 起步，schema_recall 没重跑）—— 机制本来就是好的，
#: 缺的只是它。
PHASE_STARTED = "started"


def read_records(path: Path, *, include_started: bool = False) -> list[dict[str, Any]]:
    """读出全部审计记录，保持文件（时间）顺序。

    **默认滤掉发起记录**（phase=started）：它没有结果、没有成本、没有收尾码，
    进了统计就是把每次调用数成两次、把成功率稀释一半。只有任务中心需要它
    （那一页要回答"有没有一条线程正在跑/跑一半没了"），显式传参取。
    """
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
            if not (isinstance(rec, dict) and rec.get("trace_id")):
                continue
            if not include_started and rec.get("phase") == PHASE_STARTED:
                continue
            out.append(rec)
    return out


def _summary(rec: dict[str, Any]) -> dict[str, Any]:
    s = {k: rec.get(k) for k in SUMMARY_FIELDS}
    # 老记录没有 kind 字段：它们全部产生自 /api/ask 链路
    s["kind"] = rec.get("kind", "ask")
    # 角色是后加的字段，老记录没有 —— 如实标"未记录"，别默认成 ANONYMOUS
    s["role"] = rec.get("role") or "（未记录）"
    s["user"] = rec.get("user") or ""
    s["ok"] = not rec.get("rejected_by")
    return s


def list_audits(
    path: Path, page: int = 1, page_size: int = 10,
    q: str = "", kind: str = "", with_text: bool = True,
    only_user: str | None = None, status: str = "", source: str | None = None,
) -> dict[str, Any]:
    """流水分页，新记录在前。q 同时匹配 trace_id 与问题文本。

    with_text=False 时**问题原文不出接口**，且 q 只匹配 trace_id。

    两件事必须一起做：只把 question 抹掉、仍允许按文本搜，等于留了一个预言机
    —— 搜"广州"能搜出 12 条，就已经把内容说出来了。遮蔽和检索面是同一道边界，
    分开做等于没做。

    only_user 不为 None 时只返回该用户发起的记录（产品与测试角色就是这样看
    审计的：只看自己的）。**这一层过滤排在 q 与分页之前**，理由和上面那条
    完全一样 —— 先搜后滤会让 total 泄露别人有多少条命中，那也是一个预言机。
    空串是合法取值：它表示"只看没有发起人的记录"，不是"不过滤"。

    status / source 是执行追踪页的两个下拉：
      · status ∈ {ok, rejected, interrupted} —— 按记录自身的收尾判定，
        与 _thread_status 同一口径（线程看最后一条，这里看这一条）
      · source —— 记录里的数据源 id。None 才是"不筛"，空串是合法取值
        （"未记录数据源"那一档）—— 用空串当哨兵的话，老记录那一档永远选不中
    返回里额外给一个 sources：**当前可见记录里真出现过的**数据源，
    在其余筛选之前算 —— 否则选中某个源之后，下拉里就只剩这一个选项，
    人就退不回去了。
    """
    recs = read_records(path)
    recs.reverse()
    if only_user is not None:
        recs = [r for r in recs if (r.get("user") or "") == only_user]

    seen: dict[str, str] = {}
    for r in recs:
        sid = str(r.get("source") or "")
        if sid not in seen:
            seen[sid] = str(r.get("source_name") or r.get("source") or "（未记录数据源）")
    sources = [{"id": sid, "name": name} for sid, name in seen.items()]

    if kind:
        recs = [r for r in recs if r.get("kind", "ask") == kind]
    if status:
        recs = [r for r in recs if _record_status(r) == status]
    # None = 不筛；空串是**合法取值**，表示"未记录数据源"那一档
    if source is not None:
        recs = [r for r in recs if str(r.get("source") or "") == source]
    if q:
        ql = q.strip().lower()
        recs = [
            r for r in recs
            if ql in str(r.get("trace_id", "")).lower()
            # 发起人与问题原文同属"内容"，一起受 with_text 管：只抹显示、
            # 仍允许按它搜，等于留了一个预言机（见上面那段）
            or (with_text and ql in str(r.get("question", "")).lower())
            or (with_text and ql in str(r.get("user", "")).lower())
        ]
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    start = (page - 1) * page_size
    return {
        "total": len(recs), "page": page, "page_size": page_size,
        "items": [_redact(_summary(r), with_text) for r in recs[start:start + page_size]],
        # 页面据此显示遮蔽提示，而不是让人以为这些记录本来就没有问题文本
        "text_visible": with_text,
        "sources": sources,
    }


def _redact(item: dict[str, Any], with_text: bool) -> dict[str, Any]:
    """未登录时抹掉能指认到人的那两个字段。

    留下的是时间、角色、护栏结果、耗时与成本 —— 那些是**聚合与结构**，
    也正是这一页要展示的东西（护栏在拦什么、拦了多少、贵不贵）。
    抹掉的是问题原文与发起人：它们是别人问过的内容，不是这一页的展示目标。

    抹成 None 而不是删键：前端按字段渲染，少一个键会变成 undefined 到处冒，
    而 None 是一个明确的"这里有东西但你看不到"。
    """
    if with_text:
        return item
    return {**item, "question": None, "user": ""}


#: 这两个收尾码都表示「现场还在检查点里」：INTERRUPTED 是执行中断，
#: RESUME_BLOCKED 是续跑前置校验没过（权限收窄 / 库连不上 / 表结构变了）。
#: 后者**不是终态** —— 条件恢复后这条线程照样能续，所以判定上与中断同档。
#: 归成 rejected 的话，任务中心会把它当"已收尾"，续跑入口跟着消失。
_OPEN_CODES = frozenset({"INTERRUPTED", "RESUME_BLOCKED"})


def _record_status(rec: dict[str, Any]) -> str:
    """单条记录怎么收尾的 —— ok / rejected / interrupted。

    **审计中心的三档口径，有意保持粗粒度**：那一页问的是"这次调用成没成"，
    不是"接下来该谁动手"。细分档由 stage() 给，任务中心用它。
    两处都从 rejected_by 读，别在别处再写第三份。
    """
    if rec.get("rejected_by") in _OPEN_CODES:
        return "interrupted"
    return "rejected" if rec.get("rejected_by") else "ok"


#: 收尾码 → 任务态。**确定性折算，不是模型打分**，与 _risk 同一套做法。
#:
#: 2026-09-07 之前这里只有 done / rejected / interrupted 三档，于是六种
#: 语义完全不同的结局被压进同一个"已拦截"：碰了安全红线（不可放行）、
#: 模型答不上来（换个问法就行）、库连不上（找运维，而且天然可重试）、
#: 等人放行（有人点一下就能继续）—— 四种里有三种其实还有下一步，
#: 而页面一律显示"已拦截"，读起来全是终结态。
#:
#: 判据全部来自记录里**已有**的字段，不新增任何一维：
#:   · INTERRUPTED / RESUME_BLOCKED —— 现场在检查点里，可续跑
#:   · EXEC                         —— 执行期故障（连不上、超时），该找运维
#:   · NO_SQL                       —— 模型没产出 SQL，下一步在**用户**手上
#:   · R-xx                         —— 护栏拦下，除非拿到审批否则改写法也没用
#: R-11 另有一档：它是唯一"拿到审批就能继续"的拒绝，有未决审批单时归
#: waiting_approval，由调用方把审批单 id 传进来（审计不认识 approvals 存储，
#: 也不该认识 —— 那是两套存储，耦合进来这里就没法单测了）。
RUNNING = "running"                       # 只落了发起记录，还没收尾
DONE = "done"
WAITING_REVIEW = "waiting_review"         # 跑完了，但结果可信度存疑，等系统管理员采信/打回
REVIEW_RETURNED = "review_returned"       # 复核打回：这个数字不采信
REJECTED = "rejected"                     # 安全红线：护栏拦下
WAITING_INPUT = "waiting_input"           # 等用户补充/换个问法
WAITING_APPROVAL = "waiting_approval"     # 等负责人放行
NEEDS_OPERATOR = "needs_operator"         # 等运维：库连不上、执行期故障
INTERRUPTED = "interrupted"               # 断点在，可续跑


#: 结果可信度存疑的痕迹 → 一句人话。**判据全在审计里已有的字段上**，
#: 不新增维度、不引入"可信度分数"（一个 0~100 的数说不清它凭什么，
#: 而复核这件事的全部意义就是说得清）。
#:
#: 这四条对应的都是**看起来成功的错答**那一类失败：链路每层都做对了自己
#: 那件事，最后给出一个语气笃定的错数字。2026-09-07 实测过一次：问"一共有
#: 多少个用户"，召回给的是 agent_messages，模型按给的表算出 6512，
#: 真值 10084，没有任何一层报错。
def review_reasons(rec: dict[str, Any]) -> list[str]:
    """这条结果为什么值得复核。空列表 = 不需要复核。"""
    why: list[str] = []
    if rec.get("recall_blind"):
        why.append("召回是盲选：给模型的表不是按相关度选出来的，可能答非所问")
    if rec.get("mask_degraded"):
        why.append("脱敏判定退化：SQL 解析不出投影来源，整行按敏感返回")
    if int(rec.get("attempts") or 1) >= 3:
        why.append(f"反复重试 {rec.get('attempts')} 次后才收敛，前几轮都被打回")
    if rec.get("converged_early"):
        why.append(f"token 触顶提前收敛：{rec.get('converged_early')}")
    return why


def needs_review(rec: dict[str, Any]) -> bool:
    """跑成了，但结果带着存疑痕迹。**只对成功的结果问这个问题** ——
    被拦下的那些本来就没给出数字，没有"采不采信"可言。"""
    return not rec.get("rejected_by") and bool(review_reasons(rec))


def stage(rec: dict[str, Any], *, has_open_approval: bool = False,
          review_status: str = "") -> str:
    """一条记录**当前处在哪一档**，以及言下之意是"下一步该谁动手"。

    review_status 由调用方从复核存储取（审计不认识那套存储，理由同审批）：
    空串 = 还没结论。
    """
    if rec.get("phase") == PHASE_STARTED:
        return RUNNING
    code = rec.get("rejected_by")
    if code in _OPEN_CODES:
        return INTERRUPTED
    if not code:
        # 复核只在**成功的结果**上发生：先看有没有结论，没有再看要不要复核。
        if review_status == "RETURNED":
            return REVIEW_RETURNED
        if review_status == "ACCEPTED":
            return DONE          # 已采信，回到普通的"已完成"
        return WAITING_REVIEW if needs_review(rec) else DONE
    if code == "EXEC":
        return NEEDS_OPERATOR
    if code == "NO_SQL":
        return WAITING_INPUT
    if has_open_approval:
        # 目前只有 R-11 会开审批单；判据用"有没有未决审批"而不是硬编码规则号，
        # 将来哪条规则接上审批，这里不用改。
        return WAITING_APPROVAL
    return REJECTED


def _thread_status(last: dict[str, Any], *, has_open_approval: bool = False,
                   review_status: str = "") -> str:
    """一条线程现在处于什么状态 —— 看它**最后一条**记录。

    续跑写新 trace 但 thread 不变，所以线程的当前状态永远由最后一条决定；
    归属才看第一条（见 tasks 的说明）。
    """
    return stage(last, has_open_approval=has_open_approval,
                 review_status=review_status)


# ---- 风险分档 ----------------------------------------------------------------
# 审计里**没有**"风险等级"这个字段，也不该有一个模型给出的主观打分。
# 这里的分档是对已记录事实的**确定性折算**，规则写在代码里、理由随值一起返回
# （risk_why），页面上鼠标停上去就能看到凭什么是这一档 —— 一个说不出理由的
# 风险标签，比不标更糟。
#
# 分档看"这次查询碰到了哪条边界"，不看它失败没失败：
#   HIGH   越权 / 触碰不该碰的数据：写操作、未开放表、跨库、危险函数、
#          租户不明、超出可见期限
#   MEDIUM 成本与扫描：扫描超阈值、笛卡尔积、成本上限、配额耗尽；
#          以及虽未被拦下、但确实是大扫描 / 结果被上限截断 / 走了多步链路的查询
#   LOW    其余：写法问题（多语句、字段名错、SELECT *）、环境故障，
#          以及正常的小查询 —— 它们没有碰到任何数据边界
_RISK_HIGH = {"R-02", "R-03", "R-06", "R-07", "R-10", "R-19"}
_RISK_MEDIUM = {"R-08", "R-11", "R-17", "R-20", "QUOTA"}

_RISK_WHY = {
    "R-02": "含写入意图，被只读护栏拦下",
    "R-03": "用到未开放的表",
    "R-06": "跨 schema / 跨库引用",
    "R-07": "用到被禁用的函数",
    "R-10": "无法确定所属租户",
    "R-19": "超出该角色可见的数据期限",
    "R-08": "出现笛卡尔积",
    "R-11": "预估扫描量超阈值",
    "R-20": "SQL 文本过长或嵌套过深，解析成本超预算",
    "R-17": "累计成本达上限",
    "QUOTA": "调用配额已用完",
}


def _risk(rec: dict[str, Any], max_rows: int, max_scan_rows: int) -> tuple[str, str]:
    """一条线程最后一次执行的风险档 + 理由。

    阈值取**当前配置**：历史记录不带当时的阈值，拿今天的口径折算是明摆着的
    近似 —— 但比不给强，也比凭空编一个等级诚实。
    """
    rejected = str(rec.get("rejected_by") or "")
    if rejected in _RISK_HIGH:
        return "HIGH", _RISK_WHY[rejected]
    if rejected in _RISK_MEDIUM:
        return "MEDIUM", _RISK_WHY[rejected]
    if rejected:
        return "LOW", f"被 {rejected} 拦下，未触及数据边界"

    scan = rec.get("explain_rows")
    if isinstance(scan, int) and max_scan_rows > 0 and scan >= max_scan_rows // 2:
        return "MEDIUM", f"预估扫描 {scan:,} 行，已达阈值的一半以上"
    rows = rec.get("rows_returned")
    if isinstance(rows, int) and max_rows > 0 and rows >= max_rows:
        return "MEDIUM", f"结果达行数上限 {max_rows}，可能已被截断"
    if rec.get("multi_step"):
        return "MEDIUM", "走了多步执行链路"
    return "LOW", "只读单步查询，未触及任何边界"


#: 每一档**下一步该谁动手**。任务态的全部意义就在这一列 ——
#: 四种"已拦截"里有三种还有下一步，说不清楚就等于没分档。
_NEXT_ACTOR = {
    RUNNING: "系统正在执行",
    DONE: "",
    WAITING_REVIEW: "等系统管理员复核：结果带存疑痕迹，采信或打回",
    REVIEW_RETURNED: "复核未通过：这个数字不采信，换个问法重新发起",
    REJECTED: "不可放行：这条触碰的是安全边界，改写法也过不去",
    WAITING_INPUT: "等你补充：把问题说具体些，或直接写出表名",
    WAITING_APPROVAL: "等负责人放行：审批通过后凭票重跑",
    NEEDS_OPERATOR: "等运维：数据源连不上或执行期故障，恢复后可重试",
    INTERRUPTED: "可续跑：现场还在检查点里",
}


def tasks(path: Path, only_user: str | None = None, *,
          max_rows: int = 0, max_scan_rows: int = 0,
          open_approval_ids: Any = None,
          review_status: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """执行线程，新的在前。``only_user=None`` 给全部，字符串只给这个人发起的。

    askdb 没有任务表，任务这个概念完全落在审计流水与检查点上：
    一次提问开一条线程（thread_id），续跑写新 trace 但线程不变。
    所以"我有哪些任务" = 按 thread_id 聚合我发起过的审计记录。

    这里列全部而不是只列中断的：中断只在异常逃出执行图时才发生
    （进程故障、递归超限、检查点库异常），是故障态不是常规流程。
    只列中断等于这一页正常情况下永远是空的 —— 实际就是这么空了。
    可续跑的那些由 ``resumable`` 字段标出来，续跑入口只对它们开放。

    **可见范围与归属是两件事，2026-09-06 起在这里分开。**

    原来这两件事是同一件：只列出 user 名下的线程，理由是"列得出来就该续得了"。
    可见面统一之后那条捆绑站不住 —— 它的实际效果是审计中心列着所有人的记录
    （连未登录访客都看得到全部原文），任务中心却因为按发起人过滤而空着一页，
    登录用户看到的比匿名还少。这正是这次要消灭的形状。

    现在：**可见范围**由调用方给的 only_user 决定（server 按 TASKS_ALL 能力位
    算，人人都有 → None → 全部可见）；**归属**由每条记录上的 ``owner`` 字段
    如实标出，续跑仍然只有主人能做（/api/resume 的校验一行没改）。
    页面据 owner 把别人的线程标出来并置灰续跑入口 —— 与"未登录可读不可写"
    是同一条轴：看得见不等于动得了。
    """
    threads: dict[str, list[dict[str, Any]]] = {}
    # 这一页**要**发起记录：一条线程只落了发起、没落收尾，说明它要么正在跑、
    # 要么跑一半进程没了 —— 两种都得看得见，而这正是原来整片丢失的那一档。
    for rec in read_records(path, include_started=True):
        tid = rec.get("thread_id") or rec.get("trace_id")
        if tid:
            threads.setdefault(str(tid), []).append(rec)

    open_approvals = {str(a) for a in (open_approval_ids or ())}
    out: list[dict[str, Any]] = []
    for tid, recs in threads.items():
        # 同一次调用的发起记录与收尾记录共用 trace_id：收尾一到，发起就该退场，
        # 否则"最后一条"可能是那条占位，线程会永远显示成运行中。
        done_traces = {r.get("trace_id") for r in recs
                       if r.get("phase") != PHASE_STARTED}
        recs = [r for r in recs
                if r.get("phase") != PHASE_STARTED
                or r.get("trace_id") not in done_traces]
        if not recs:
            continue
        # 归属看这条线程的**第一条**记录：续跑会写新 trace，但发起人不变。
        # 按最后一条判会让"谁续跑谁就成了主人"。
        owner = recs[0].get("user") or ""
        if only_user is not None and owner != only_user:
            continue
        last = recs[-1]
        item = _summary(last)
        item["thread_id"] = tid
        item["attempts_on_thread"] = len(recs)
        item["first_ts"] = recs[0].get("ts", "")
        item["question"] = recs[0].get("question") or last.get("question") or ""
        trace = str(last.get("trace_id") or tid)
        item["status"] = _thread_status(
            last, has_open_approval=trace in open_approvals,
            review_status=(review_status or {}).get(trace, ""))
        # 为什么值得复核，逐条给出去 —— 复核人要判断的正是这几句，
        # 让他自己去猜"这条为什么进了队列"，这个队列就没人会用。
        item["review_why"] = review_reasons(last) if not last.get("rejected_by") else []
        # RUNNING 也报可续：进程被杀留下的线程与"正在跑"在审计上分不开，
        # 而前者的现场就在检查点里。这里只给一个**候选**，服务端随后按检查点
        # 逐条核实（graph.is_resumable），核不过就落回 False —— 不核实就会
        # 出现"这里说能续、点下去 404"，那正是这个字段要避免的分叉。
        item["resumable"] = item["status"] in (INTERRUPTED, RUNNING)
        # 「下一步该谁动手」直接给出去，页面不用再照着状态码写一遍 if/else ——
        # 写两遍就会漂，而这句话是这一页存在的理由。
        item["next_actor"] = _NEXT_ACTOR.get(item["status"], "")
        # 归属如实给出去。空串 = 匿名发起，不是"丢了" —— 页面要能说清这一点。
        item["owner"] = owner
        # 风险档是折算出来的，不是记录里的字段 —— 理由一并给出，页面可解释
        item["risk"], item["risk_why"] = _risk(last, max_rows, max_scan_rows)
        out.append(item)

    out.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return out


#: 任务列表的筛选取值里，``all`` 是"不筛"，空串是**一个合法的档**
#: （未记录数据源 / 匿名发起）。用空串当"不筛"的哨兵，这两档就永远选不中 ——
#: /api/audit 的 source 参数踩过同一个坑，那里用 None 区分，这里用 all，
#: 因为界面上的下拉本来就是 all 打头，一路传到底不必再翻译一次。
FILTER_ANY = "all"

#: 发起时间档，与界面上那四项一一对应。写在这里而不是在 server 上，
#: 是为了让"合法取值"只有一份定义 —— 两份就会漂。
SINCE_CHOICES = ("all", "today", "7d", "30d")

TASK_STATUSES = (
    RUNNING, DONE, WAITING_REVIEW, REVIEW_RETURNED, REJECTED,
    WAITING_INPUT, WAITING_APPROVAL, NEEDS_OPERATOR, INTERRUPTED,
)

RISK_LEVELS = ("HIGH", "MEDIUM", "LOW")


def _day_of(ts: str) -> date | None:
    t = _parse_ts(ts)
    return t.date() if t is not None else None


def _within_since(ts: str, since: str, now: datetime) -> bool:
    """发起时间档。**解析不出来的时间不放行** —— 选了"今天"却混进一条
    时间不明的记录，比少一条更糟：它会被当成今天发生的。
    """
    if since in ("", FILTER_ANY):
        return True
    t = _parse_ts(ts)
    if t is None:
        return False
    if since == "today":
        return t.date() == now.date()
    days = {"7d": 7, "30d": 30}.get(since, 0)
    if not days:
        return True
    # 记录带时区、now 也带（_now 用 astimezone），减法才成立
    return (now - t) <= timedelta(days=days)


def paginate_tasks(
    items: list[dict[str, Any]], *, page: int = 1, page_size: int = 10,
    status: str = FILTER_ANY, source: str = FILTER_ANY,
    risk: str = FILTER_ANY, user: str = FILTER_ANY, since: str = FILTER_ANY,
) -> dict[str, Any]:
    """把 tasks() 的全量线程筛好、统计好、切好页 —— 一次返回给页面。

    **统计与下拉选项算在筛选之前，分页算在筛选之后。** 这三段顺序是这个
    接口的全部要害：

      · 四张统计卡讲的是"这套系统当下的处境"（多少在跑、多少等人动手），
        它不该随手上的筛选变 —— 跟着筛选走的话，筛完"已完成"再看
        「待处理」永远是 0，那个数字就没有意义了。
      · 筛选下拉的可选值同理：只列**当前列表里真出现过的**数据源与发起人，
        但如果它跟着筛选收窄，选中一个源之后下拉里就只剩这一个，人就退不
        回去了（/api/audit 的 sources 是同一条口径，见 list_audits）。
      · total 则必须是**筛完之后**的条数，否则页码算出来是错的。

    这三个数原来都在浏览器里算，代价是每次打开都要把全部线程发过去
    （实测一次一千四百多条）。搬到这里之后出网的只有当前这一页，
    而页面上那几个数字一个不少。
    """
    now = datetime.now().astimezone()

    counts = Counter(str(it.get("status") or "") for it in items)
    done = [it for it in items if it.get("status") == DONE]
    # 时间解析不出来的不计入今天 —— 与 _within_since 同一条口径，
    # 宁可少算一条，也不要把一条时间不明的记录报成"今日完成"
    done_today = sum(1 for it in done
                     if _day_of(str(it.get("ts", ""))) == now.date())
    # 成功率的分母只算**真收尾**的：等补充、等审批、等运维都还有下一步，
    # 把它们记成失败，这个数字就会随"有多少人问得含糊"上下浮动，与系统好坏无关。
    settled = len(done) + counts[REJECTED]
    stats = {
        "running": counts[RUNNING],
        "waiting_input": counts[WAITING_INPUT],
        "waiting_approval": counts[WAITING_APPROVAL],
        "waiting_review": counts[WAITING_REVIEW],
        "review_returned": counts[REVIEW_RETURNED],
        "needs_operator": counts[NEEDS_OPERATOR],
        "interrupted": counts[INTERRUPTED],
        "rejected": counts[REJECTED],
        "done": len(done),
        "done_today": done_today,
        # 没有收尾记录时给 None 而不是 0 ——「成功率 0.0%」和"还没有可判的样本"
        # 是两回事，前者是在报一个没发生过的失败
        "success_rate": round(len(done) / settled * 100, 1) if settled else None,
    }

    seen_sources: dict[str, str] = {}
    for it in items:
        sid = str(it.get("source") or "")
        if sid not in seen_sources:
            seen_sources[sid] = str(it.get("source_name") or it.get("source")
                                    or "（未记录数据源）")
    sources = [{"value": sid, "label": name} for sid, name in seen_sources.items()]
    seen_users: list[str] = []
    for it in items:
        who = str(it.get("user") or "")
        if who not in seen_users:
            seen_users.append(who)
    users = [{"value": who, "label": who or "匿名"} for who in seen_users]

    matched = items
    if status != FILTER_ANY:
        matched = [it for it in matched if it.get("status") == status]
    if source != FILTER_ANY:
        matched = [it for it in matched if str(it.get("source") or "") == source]
    if risk != FILTER_ANY:
        matched = [it for it in matched if str(it.get("risk") or "") == risk]
    if user != FILTER_ANY:
        matched = [it for it in matched if str(it.get("user") or "") == user]
    if since != FILTER_ANY:
        matched = [it for it in matched if _within_since(str(it.get("ts", "")), since, now)]

    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    start = (page - 1) * page_size
    return {
        "items": matched[start:start + page_size],
        "total": len(matched),
        # 筛选之前有多少条。页面上「共 N 条（全部发起人）」说的是这个数，
        # 也是"一条都没有"与"筛完没有"两句不同提示的判据
        "total_all": len(items),
        "page": page, "page_size": page_size,
        "stats": stats, "sources": sources, "users": users,
    }


def resumable(path: Path, user: str) -> list[dict[str, Any]]:
    """某个账号名下**尚可续跑**的任务 —— tasks() 里状态仍为中断的那些。

    /api/resume 按 thread_id 从断点继续，只有主人能续 —— 所以这里**仍按
    发起人过滤**，与 tasks() 的"全部可见"有意不同：这个函数回答的是
    "我能续跑哪些"，不是"有哪些线程"。
    """
    return [t for t in tasks(path, user) if t["resumable"]]


def get_audit(path: Path, trace_id: str) -> dict[str, Any] | None:
    """按 trace_id 取完整记录。同 id 多条时取最后一条（重放/重投递场景）。"""
    found = None
    for rec in read_records(path):
        if rec.get("trace_id") == trace_id:
            found = rec
    return found


def trace_chain(rec: dict[str, Any]) -> dict[str, Any]:
    """一条记录的节点链视图（/api/trace 的响应体）。

    执行追踪页此前把这些字段挂在 /api/replay 上，而回放要登录、要开关、
    连真实库的实例默认关着 —— 于是那一页最常见的样子是右半屏全是占位符，
    而节点链本身在审计记录里一直都有，不含 SQL 文本也不含结果行。

    sql_hash 是就地算的：记录里只存 SQL 全文，而追踪页那一格要的是哈希。
    哈希不可逆，给出去不等于给 SQL；但它足以判断"两次查询是不是同一条 SQL"。
    """
    out: dict[str, Any] = {k: rec.get(k) for k in TRACE_FIELDS}
    out["kind"] = rec.get("kind", "ask")
    out["steps"] = [
        {k: s.get(k) for k in STEP_FIELDS if s.get(k) is not None}
        for s in (rec.get("steps") or [])
    ]
    sql = str(rec.get("sql_final") or rec.get("sql_raw") or "")
    out["sql_hash"] = hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None
    return out


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _percentile(values: list[int], q: float) -> int | None:
    """最近秩法取分位。样本少时它就等于某个真实观测值 —— 这是有意的：
    插值会造出一个从没发生过的耗时，而这页要的是"实际最慢的那次有多慢"。
    调用方须同时展示样本量，否则 7 次调用的 P95 会被当成稳定指标读。
    """
    if not values:
        return None
    k = max(0, min(len(values) - 1, round((len(values) - 1) * q)))
    return values[k]


def stats(path: Path, days: int = 30, only_user: str | None = None) -> dict[str, Any]:
    """时间窗内的调用/拦截/成本统计与按日序列。

    trace_complete 按"记录里带步骤级 trace 的占比"如实计算，
    不是写死的 100% —— 页面上那格数字必须经得起对账。

    only_user 的语义与 list_audits 一致，而且**必须一起收敛**：
    列表只给本人、统计却给全量，那张成本卡就是一个按天的聚合泄露 ——
    别人昨天花了多少、被拦了几次，一眼可见。同一道边界只做一半等于没做。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    recent: list[dict[str, Any]] = []
    for rec in read_records(path):
        if only_user is not None and (rec.get("user") or "") != only_user:
            continue
        t = _parse_ts(str(rec.get("ts", "")))
        if t is not None and t >= cutoff:
            recent.append(rec)

    calls = len(recent)
    blocked = sum(1 for r in recent if r.get("rejected_by"))
    with_steps = sum(1 for r in recent if r.get("steps"))
    elapsed = sorted(int(r.get("elapsed_ms") or 0) for r in recent)

    # 模型调用的成败按**节点**算，不是按整次调用算：一次提问里模型可能被调
    # 三四次（判定 / 生成 / 自检 / 反思），其中一次失败后重试成功，整次调用
    # 是成功的，但模型确实失败过一次。按调用算会把这些失败全部抹掉。
    model_steps = [s for r in recent for s in (r.get("steps") or [])
                   if s.get("step") in MODEL_STEPS]
    model_calls = len(model_steps)
    model_failed = sum(1 for s in model_steps if s.get("status") != "ok")

    daily: dict[str, dict[str, Any]] = {}
    by_kind: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for r in recent:
        day = str(r.get("ts", ""))[:10]
        d = daily.setdefault(day, {"date": day, "calls": 0, "cost_cny": 0.0})
        d["calls"] += 1
        d["cost_cny"] = round(d["cost_cny"] + float(r.get("cost_cny") or 0), 6)
        by_kind[r.get("kind", "ask")] = by_kind.get(r.get("kind", "ask"), 0) + 1
        if r.get("rejected_by"):
            by_rule[str(r["rejected_by"])] = by_rule.get(str(r["rejected_by"]), 0) + 1
        # 直查不经模型（model=None）不计入模型维度；老记录无 model 字段，
        # 按调用类型如实归为"未记录"而不是猜一个模型名
        m = r.get("model") or ("（未记录）" if r.get("kind", "ask") == "ask" else None)
        if m:
            e = by_model.setdefault(m, {"calls": 0, "cost_cny": 0.0})
            e["calls"] += 1
            e["cost_cny"] = round(e["cost_cny"] + float(r.get("cost_cny") or 0), 6)

    return {
        "days": days,
        "calls": calls,
        "blocked": blocked,
        "block_rate": round(blocked / calls, 4) if calls else 0.0,
        "cost_cny": round(sum(float(r.get("cost_cny") or 0) for r in recent), 6),
        "tok_in": sum(int(r.get("tok_in") or 0) for r in recent),
        "tok_out": sum(int(r.get("tok_out") or 0) for r in recent),
        "trace_complete": round(with_steps / calls, 4) if calls else None,
        # 窗口内一次模型节点都没有时为 None —— 0/0 不是 0%，也不是 100%
        "model_calls": model_calls,
        "model_failed": model_failed,
        "model_success": round((model_calls - model_failed) / model_calls, 4) if model_calls else None,
        "elapsed_p50_ms": _percentile(elapsed, 0.5),
        "elapsed_p95_ms": _percentile(elapsed, 0.95),
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "by_kind": by_kind,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_cny"])),
    }


def _pctl_of(values: list[int], q: float) -> int | None:
    """按最近秩取分位。样本少时等于某个真实观测值 —— 见 _percentile 的说明。"""
    return _percentile(sorted(values), q)


def quality(path: Path, days: int = 1) -> dict[str, Any]:
    """线上运行质量：按**真实调用**算，不用黄金集分母。

    与 stats() 的分工：stats 服务审计页（流水、成本、按规则分布），
    这里服务质量中心 —— 多出来的是**按节点聚合**，那是设计稿里那张
    「工具/节点」表的数据来源，而它一直只能靠审计记录里的 steps 算出来。

    「成功」的口径写死在这里，不留解释空间：一次调用被护栏拦下（rejected_by
    非空）或执行失败，都算没成功。拦截是护栏干活、不是故障，所以两者分开报 ——
    把拦截混进失败率，会让"护栏越有效、质量看起来越差"。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    # 上一个等长窗口。告警要能说"较昨日上升 8%"这种话 —— 而"上升"只能由
    # 两个窗口相减得出，绝对阈值（P95 > 10s）说不出它。两个窗口必须等长，
    # 否则拿 24 小时比 7 天，涨跌全是窗口长度造成的。
    prev_cutoff = cutoff - timedelta(days=days)
    all_records = read_records(path)
    recent, previous = [], []
    for r in all_records:
        t = _parse_ts(str(r.get("ts", "")))
        if t is None:
            continue
        if t >= cutoff:
            recent.append(r)
        elif t >= prev_cutoff:
            previous.append(r)

    runs = len(recent)
    blocked = sum(1 for r in recent if r.get("rejected_by"))
    # 执行类失败（数据源异常、模型调用失败）与护栏拦截是两回事
    failed = sum(1 for r in recent if r.get("rejected_by") in ("EXEC", "LLM"))
    ok = runs - blocked

    elapsed = [int(r.get("elapsed_ms") or 0) for r in recent]
    tok = [int(r.get("tok_in") or 0) + int(r.get("tok_out") or 0) for r in recent]
    costs = [float(r.get("cost_cny") or 0) for r in recent]

    # ---- 按节点聚合 ----
    nodes = _nodes_of(recent)

    node_rows = [
        {
            "step": name,
            "calls": e["calls"],
            "success_rate": round(e["ok"] / e["calls"], 4) if e["calls"] else None,
            "p50_ms": _pctl_of(e["ms"], 0.5),
            "p95_ms": _pctl_of(e["ms"], 0.95),
            "tok": e["tok"],
            # 设计稿「主要失败原因」列。没失败过就是"—"，不编一个出来
            "fail_reason": (e["fail_notes"].most_common(1)[0][0] if e["fail_notes"] else ""),
            "fails": e["calls"] - e["ok"],
        }
        for name, e in nodes.items()
    ]
    # 按 P95 倒序：这张表是拿来找延迟贡献最大的那一段的
    node_rows.sort(key=lambda d: (d["p95_ms"] or 0), reverse=True)

    return {
        "days": days,
        "runs": runs,
        "ok": ok,
        "blocked": blocked,
        "failed": failed,
        # 成功率的分母是全部调用；拦截单列，不混进失败
        "success_rate": round(ok / runs, 4) if runs else None,
        "block_rate": round(blocked / runs, 4) if runs else None,
        "p50_ms": _pctl_of(elapsed, 0.5),
        "p95_ms": _pctl_of(elapsed, 0.95),
        "avg_tok": round(sum(tok) / runs) if runs else None,
        "cost_cny": round(sum(costs), 6),
        "avg_cost_cny": round(sum(costs) / runs, 6) if runs else None,
        # 拦截按规则分布，多的在前 —— 这张表回答的是"护栏主要在挡什么"
        "by_rule": dict(sorted(
            Counter(str(r["rejected_by"]) for r in recent if r.get("rejected_by")).items(),
            key=lambda kv: -kv[1])),
        # 安全事件 ≠ 全部拦截。NO_SQL（模型没写出 SQL）是链路结果，不是安全事件；
        # 只有护栏规则 R-xx 命中才算 —— 混在一起报会让"安全事件"这一项永远不为 0，
        # 从而失去它唯一的用处：出现就该有人去看。
        "security_events": sum(
            1 for r in recent if str(r.get("rejected_by") or "").upper().startswith("R-")),
        # 本实例开始有审计记录的时间。用来回答"这套服务跑了多久" ——
        # 它不是部署时间（没有任何地方记部署），措辞上必须写成"有记录以来"。
        "first_ts": _first_ts(all_records),
        # 上一个等长窗口的同口径值，供页面算环比。**样本量一并给出** ——
        # 上个窗口只有两三次调用时，P95 的涨跌没有意义，页面据此决定报不报。
        "prev": {
            "runs": len(previous),
            "p95_ms": _pctl_of([int(r.get("elapsed_ms") or 0) for r in previous], 0.95),
            # 「线上平均 Token / 单任务成本」两张卡要出环比，口径与本窗口逐字相同
            "avg_tok": (round(sum(int(r.get("tok_in") or 0) + int(r.get("tok_out") or 0)
                                  for r in previous) / len(previous)) if previous else None),
            "avg_cost_cny": (round(sum(float(r.get("cost_cny") or 0)
                                       for r in previous) / len(previous), 6)
                             if previous else None),
            "nodes": {
                name: {"calls": e["calls"], "p95_ms": _pctl_of(e["ms"], 0.95)}
                for name, e in _nodes_of(previous).items()
            },
        },
        "nodes": node_rows,
        # ---- 以下四组服务「线上质量」页，字段位置照设计稿 ----
        # 工具调用总量与成功率。askdb 的"工具"就是链路节点，分母是节点调用次数，
        # 不是任务数 —— 一次任务会打好几个节点，两者混用会让成功率无从对账
        "tools": _tools_of(nodes),
        # SQL 执行成功率单列。它**不等于结果准确率**，页面上那句话不是客套：
        # SQL 跑通了但口径用错，这里照样是 100%
        "sql": {"calls": nodes.get("execute", {}).get("calls", 0),
                "ok": nodes.get("execute", {}).get("ok", 0)},
        # 自动重试恢复率：attempts > 1 的任务里，最终没被拒的占多少
        "retry": _retry_of(recent),
        # 断点恢复率：断过的线程里，最终正常收尾的占多少
        "resume": _resume_of(recent),
        # 同问题重复查询率 —— 没有"用户觉得答得对不对"的信号时，
        # 短时间内换个问法再问一次是能拿到的最接近的代理指标
        "repeat": _repeat_of(recent),
        # 七段趋势，供设计稿里四张卡的 sparkline 用。段数固定 7，
        # 段长 = 窗口 / 7，因此 24 小时窗口一段是 3.4 小时，30 天窗口一段是 4.3 天
        "series": _series_of(recent, cutoff, days),
    }


def _tools_of(nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """工具（节点）调用总量、成功率，以及失败主要集中在哪个节点。

    设计稿那句"149 次失败 · 数据库工具占 71%"要的就是后半句：
    知道失败最多的是哪一类，才知道该去修哪儿。
    """
    calls = sum(e["calls"] for e in nodes.values())
    ok = sum(e["ok"] for e in nodes.values())
    fails = {name: e["calls"] - e["ok"] for name, e in nodes.items() if e["calls"] > e["ok"]}
    top = max(fails.items(), key=lambda kv: kv[1]) if fails else None
    return {
        "calls": calls,
        "ok": ok,
        "fails": calls - ok,
        "top_fail_step": top[0] if top else "",
        "top_fail_share": round(top[1] / (calls - ok), 4) if top and calls > ok else None,
    }


def _retry_of(records: list[dict[str, Any]]) -> dict[str, Any]:
    """自动重试恢复率。分母是**真的重试过**的任务（attempts > 1），
    不是全部任务 —— 拿全部任务当分母会让这个数永远接近 100%，读不出信息。"""
    retried = [r for r in records if int(r.get("attempts") or 1) > 1]
    recovered = [r for r in retried if not r.get("rejected_by")]
    return {
        "retried": len(retried),
        "recovered": len(recovered),
        "rate": round(len(recovered) / len(retried), 4) if retried else None,
    }


def _resume_of(records: list[dict[str, Any]]) -> dict[str, Any]:
    """断点恢复率。分母是**真的断过**的线程（出现过 INTERRUPTED 的 thread_id），
    分子是这些线程最后已经正常收尾的那些。

    只能按线程判，不能按记录判：续跑写的是新 trace、同一条 thread，
    按记录数算会把"断了一次又续上"记成一半失败。

    中断本身是故障态（异常逃出执行图），窗口内一次都没断过是常态 ——
    那时 rate 为 None，页面要显示"无样本"，不是 0%。
    """
    threads: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        tid = str(r.get("thread_id") or r.get("trace_id") or "")
        if tid:
            threads.setdefault(tid, []).append(r)
    broke = [sorted(rs, key=lambda r: str(r.get("ts", "")))
             for rs in threads.values()
             if any(x.get("rejected_by") in _OPEN_CODES for x in rs)]
    recovered = [rs for rs in broke if not rs[-1].get("rejected_by")]
    return {
        "interrupted": len(broke),
        "recovered": len(recovered),
        "rate": round(len(recovered) / len(broke), 4) if broke else None,
    }


# 换个问法再问一次，算不算"同一件事"的时间窗
_REPEAT_WINDOW = timedelta(minutes=10)


def _repeat_of(records: list[dict[str, Any]]) -> dict[str, Any]:
    """同问题重复查询率：同一个人在 10 分钟内又提交了一次。

    口径写清楚，因为它很容易被读成别的意思：
    - 「同一个人」= 同一 (org_id, user)。匿名调用 user 为空，会被并成一个人 ——
      这会**高估**重复率，页面上要标注，不能当精确值用。
    - 问法一模一样（刷新重跑）和改了问法都计入：两者都指向"上一次没解决问题"。
    """
    by_user: dict[tuple[Any, str], list[datetime]] = {}
    for r in records:
        t = _parse_ts(str(r.get("ts", "")))
        if t is None:
            continue
        by_user.setdefault((r.get("org_id"), str(r.get("user") or "")), []).append(t)
    n = 0
    for times in by_user.values():
        times.sort()
        n += sum(1 for a, b in zip(times, times[1:]) if b - a <= _REPEAT_WINDOW)
    total = len(records)
    return {"n": n, "rate": round(n / total, 4) if total else None,
            "window_min": int(_REPEAT_WINDOW.total_seconds() // 60)}


# sparkline 的段数。设计稿画的就是 7 根柱子
_SERIES_BUCKETS = 7


def _series_of(records: list[dict[str, Any]], cutoff: datetime,
               days: int) -> list[dict[str, Any]]:
    """把窗口等分成 7 段，每段给出四张卡各自需要的那个数。

    空段照样返回（runs=0、比率为 None），不能跳过 —— 跳过会让柱子的横轴
    变成"有数据的那几段"，趋势就是假的。
    """
    span = timedelta(days=days) / _SERIES_BUCKETS
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(_SERIES_BUCKETS)]
    for r in records:
        t = _parse_ts(str(r.get("ts", "")))
        if t is None:
            continue
        idx = int((t - cutoff) / span) if span else 0
        buckets[max(0, min(_SERIES_BUCKETS - 1, idx))].append(r)

    out = []
    for group in buckets:
        nodes = _nodes_of(group)
        calls = sum(e["calls"] for e in nodes.values())
        ok = sum(e["ok"] for e in nodes.values())
        ex = nodes.get("execute", {"calls": 0, "ok": 0})
        out.append({
            "runs": len(group),
            "tool_rate": round(ok / calls, 4) if calls else None,
            "sql_rate": round(ex["ok"] / ex["calls"], 4) if ex["calls"] else None,
            "p95_ms": _pctl_of([int(r.get("elapsed_ms") or 0) for r in group], 0.95),
        })
    return out


def _nodes_of(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按节点把 steps 聚起来。两个窗口共用同一套口径 —— 环比只有在
    分子分母算法逐字相同的前提下才成立。"""
    nodes: dict[str, dict[str, Any]] = {}
    for r in records:
        for st in (r.get("steps") or []):
            name = str(st.get("step", ""))
            if not name:
                continue
            e = nodes.setdefault(
                name, {"calls": 0, "ok": 0, "ms": [], "tok": 0, "fail_notes": Counter()})
            e["calls"] += 1
            if st.get("status") == "ok":
                e["ok"] += 1
            else:
                # 失败原因取这一步自己的 note —— 设计稿那张表最右列问的是
                # "这个工具主要死在什么上"，只有节点自己的 note 答得了
                e["fail_notes"][str(st.get("note") or st.get("status") or "未记录原因")] += 1
            e["ms"].append(int(st.get("ms") or 0))
            e["tok"] += int(st.get("tok_in") or 0) + int(st.get("tok_out") or 0)
    return nodes


def _first_ts(records: list[dict[str, Any]]) -> str | None:
    """最早一条审计记录的时间戳。文件按写入顺序追加，取第一条可解析的即可。"""
    for r in records:
        if _parse_ts(str(r.get("ts", ""))) is not None:
            return str(r.get("ts"))
    return None
