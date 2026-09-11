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
from collections import Counter, OrderedDict, deque
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .trace import step_failed

# 下面这些入口（list_audits / tasks / stats / quality / get_audit / resumable）
# 的第一个参数既可以是审计文件的 Path，也可以是 Config —— 由 read_records 决定
# 读库还是读文件（2026-09-09 起生产读 PostgreSQL）。它们自己不碰存储，只是把
# 这个参数转手，所以标注成 Any，而不是在六处各写一遍联合类型。

# 出现在流水列表里的字段。白名单式：新加字段须显式列入，
# 避免未来往审计记录里塞了敏感字段后被列表接口顺手带出去。
SUMMARY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "role", "user", "question", "rejected_by",
    "attempts", "rows_returned", "elapsed_ms", "cost_cny",
    "step_count", "multi_step", "source", "source_name",
    # 命中应答缓存的那条记录，耗时/成本/token 全是 0 —— 不标一句"命中缓存"，
    # 流水上它和一次真跑长得一样，只是快得离谱。
    "cached",
)

# /api/trace 的字段白名单：执行追踪页要的是**节点链与计量**。
# 与 REPLAY_FIELDS 的分界是刻意的 —— 这里不给 sql_raw / sql_final / question，
# SQL 文本与问题原文仍然只经 /api/replay 出去（要登录、要开关）。
# cached / cached_from 一起放出去：只有一条 cache 节点的链路，不说明白就是
# 一条"什么也没发生"的调用。cached_from 是首跑那条的 trace_id —— 它本身仍受
# 同一道可见性判定，跳过去看不看得到，与任何一条 trace 同一个规则。
#
# 命中表是个例外，且是**有意的**：schema_recall 那步的 tables（见 STEP_FIELDS）
# 就是 tables_hit 的同一份内容。它出接口不构成新的泄露 —— /api/trace 在返回前
# 已按调用者当下的可见表收窄，凡有一张命中表不在可见范围内，整条记录按 404 挡
# 掉（见 server.trace_chain_api）。能读到这条链路的人，本来就看得见这些表。
# 顶层 tables_hit 仍然不给：一处出口足够，两处会让上面那道收窄有两个地方要记。
TRACE_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "role", "model",
    "tok_in", "tok_out", "step_count", "multi_step", "attempts",
    "elapsed_ms", "cost_cny", "rejected_by", "source", "source_name",
    "cached", "cached_from",
    # 结果可信度那枚角标要判的痕迹。原来是四条机械护栏标志，2026-09-10 之后
    # 加了三条语义信号（猜测措辞、缓存计数列、纯指代追问）—— 那次跑测里
    # 1030 条有 11/12 拿满分，包括模型自己写着"作为占位，口径需人工确认"的
    # 那一条，原因就是语义风险一项都不进分母。
    # 它们出接口不构成新的泄露：hedge_terms 是命中的措辞词、derived_columns
    # 是列名，都不带数据内容。少了它们追踪页那枚角标就与工作台右栏对不上
    # （两处必须同源，否则同一次查询两个分）。
    "recall_blind", "scope_narrowed", "mask_degraded", "truncated",
    "hedge_terms", "derived_columns", "anaphoric", "caliber",
)

# 步骤对象自身也走白名单 —— 记录里的 steps 由各节点自由追加，
# 哪天有人往里塞了 sql 或行样本，这里不会顺手带出去。
# 失败与回退那五个字段一并放行：全是模型名、序号、状态码与我们自己写死的
# 处置短语，**不含任何来自数据或用户的内容**，出接口不构成新的泄露。
# 少了它们，页面上那条 failed span 只剩一句"这里断过"—— 说不清是该退避重试
# 还是该换模型，也说不清链路后来是怎么活下来的。
#
# **error_message 有意不在其中**：厂商的 4xx 消息可能把请求片段回显出来，
# 而提示词里带着表结构与用户的问题。它留在审计记录与 /api/replay 上，
# 与 sql_raw / question 同一道边界。
#
# ---------------------------------------------------------------------------
# input / output：**这两个是本白名单里唯一带内容的字段**，2026-09-12 按产品
# 决定放行。放行意味着 /api/trace（免登录可读）会带出：
#   · 问题原文（clarify / schema_recall 的输入）
#   · 表结构全文（schema_recall 的输出，即喂给模型的提示词素材）
#   · 提示词全文与模型原始响应（所有 MODEL 步）
#   · SQL 全文，含护栏改写前后两版（guard / dry_run / execute 的输入）
#   · **已脱敏的完整结果行**（execute 的输出）
#   · 工具参数与工具返回全文（agentic 链路的 tool_call）
#
# 这是有意为之，不是漏挡 —— 追踪页在此之前「输入摘要」一列除了 token 数
# 全是占位符，因为库里根本没这个数据。要收回去，改这里一行即可（把两个
# 字段从元组里去掉），采集侧不用动：审计记录里仍然记着，只是不出接口。
#
# **仍然守住的那两道**：
#   1. 记录级可见性没变 —— server.trace_chain_api 依旧按调用者当下的可见表
#      收窄，凡有一张命中表不在范围内，整条记录按 404 挡掉。放开的是"你已经
#      能看到的那条链路里，内容展开到底"，不是"让你看到看不到的链路"。
#   2. error_message 依旧不在这里 —— 它是厂商回显，长度与内容都不受我们控制。
#      提示词我们自己拼、自己截断（trace.IO_CAP），两者不是一回事。
STEP_FIELDS = ("step", "status", "ms", "tok_in", "tok_out", "note", "tables",
               "attempt", "attempts_total", "model", "error_code", "disposition",
               "tool", "input", "output")

# 真正过模型的图节点。与前端 traceSteps.ts 的 STEP_TYPE == 'MODEL' 是同一份口径，
# 两边都写一次是因为一个算数、一个只做展示；漂了会让「模型调用成功率」这格
# 与页面上标 MODEL 的那些 span 对不上 —— tests 里钉住了两边一致。
MODEL_STEPS = frozenset({"plan", "generate_sql", "assess", "reflect", "intent", "decide"})


# /api/replay 的字段白名单（判定链路回放接口设计说明 §4.2）。
# rows / schema_prompt 两个字段在设计上**绝不出接口** —— 用白名单而不是
# 黑名单：漏给一个无害字段是体验问题，漏挡一个敏感字段是事故。
REPLAY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "role", "user", "question",
    "tables_hit", "metrics_hit", "sql_raw", "sql_final",
    "rules_fired", "rejected_by", "attempts", "explain_rows",
    "step_count", "multi_step", "converged_early", "rows_returned",
    "elapsed_ms", "tok_in", "tok_out", "cost_cny", "steps",
    "source", "source_name", "cached", "cached_from",
)

# 「最终结果」字段（/api/result 用）。与上面两份白名单**分开**、单独一道门：
# 追踪详情与任务中心要展示"最终结果"（答案 + 已脱敏结果行），这些字段既不在
# 匿名可读的 TRACE_FIELDS 里，也不随 replay 那道 SQL 全文的开关走。
#
# 边界：只放答案文本、结果列名、**已脱敏**的前 N 行与行数/脱敏列。**不含 sql_final /
# sql_raw / question**（SQL 全文仍只走 /api/replay 的 login+replay_api 双门）。
# rows_preview 在写入侧就已经是 executor 脱敏后的行、且截到前 N 行。
RESULT_PREVIEW_ROWS = 20
RESULT_FIELDS = ("answer", "columns", "rows_preview", "rows_returned",
                 "masked_columns", "mask_degraded", "truncated")


def result_block(rec: dict[str, Any]) -> dict[str, Any] | None:
    """从审计记录取「最终结果」块（present-only）。被拦下的记录（rejected_by 非空）
    没有可采信的结果，返回 None —— 与"不展示推测/伪造结果"一致。旧记录没存
    rows_preview/answer 时也返回 None（回退到"暂无结果"，不伪造）。"""
    if rec.get("rejected_by"):
        return None
    if not rec.get("rows_preview") and not rec.get("answer"):
        return None
    return {k: rec[k] for k in RESULT_FIELDS if k in rec}


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


# ===========================================================================
# 读取筛选：一份口径，两个后端
#
# 2026-09-09 加。此前 read_records 是"读全部"，筛选一律在 Python 里对着
# 全量列表做。搬进 PostgreSQL 之后那个形状没有跟着改，于是索引建了一整套
# （askdb_audit 上有 ts / trace_id / username 四个索引）却没有一条查询用得上：
# 审计页要 10 条记录，先把全表读回来。一天 1.5 万条的量级下这是 OOM。
#
# 下推就得让筛选条件同时能变成 SQL 和能对着一条 dict 判真假，而**同一个语义
# 写两遍正是这个仓库反复踩的那类 bug**（列上说 A、原文说 B）。所以这里只描述
# 条件本身：SQL 由 auditstore._where 生成，dict 判定由下面的 matches 做，
# 两者由 tests/test_audit_pushdown.py 钉住必须给出同一个答案。
#
# 有两处折算不能想当然，写在这里免得下次又对不上：
#   · kind 老记录没有这个字段，Python 侧按 "ask" 兜底（rec.get("kind", "ask")），
#     入库时却写成了空串 —— 所以筛 ask 必须同时认空串。
#   · rejected_by 是收尾码，status 那三档由它折算，判据只此一处（_record_status）。
# ===========================================================================

@dataclass(frozen=True)
class AuditFilter:
    """一次审计读取的筛选条件。**全部字段都能下推到 SQL。**

    None 一律表示"这一维不筛"。空串是**合法取值**，不是"不筛"——
    发起人为空是匿名发起、数据源为空是未记录数据源，都是页面上真实存在
    的一档。用空串当哨兵的话，这两档永远选不中。
    """

    #: 发起记录（phase=started）算不算。默认不算：它没有结果、没有成本，
    #: 进了统计就是把每次调用数成两次。只有任务中心显式要。
    include_started: bool = False
    #: 只要这个时刻之后的（闭区间）。时间窗是这套下推里最要紧的一维。
    since: datetime | None = None
    trace_id: str | None = None
    #: 发起人。对应记录里的 user 字段、库里的 username 列。
    username: str | None = None
    #: ask / sql。老记录没有这个字段，见上面那段折算说明。
    kind: str | None = None
    #: 数据源 id。
    source: str | None = None
    #: 收尾档 ok / rejected / interrupted，由 rejected_by 折算。
    status: str | None = None
    #: 只要这些线程的记录。任务中心用它把范围收到"最近 N 条线程"上 ——
    #: 见 tasks() 里那段说明。空元组表示"一条线程都不要"，与 None 不同。
    thread_ids: tuple[str, ...] | None = None


def _thread_of(rec: dict[str, Any]) -> str:
    """这条记录属于哪条线程。**只此一处**，SQL 侧的 COALESCE 与它对应。

    没有 thread_id 的老记录退回 trace_id —— 一次调用自成一条线程，
    这是任务中心一直以来的口径，不是新加的兜底。
    """
    return str(rec.get("thread_id") or rec.get("trace_id") or "")


def matches(rec: dict[str, Any], f: AuditFilter) -> bool:
    """一条记录过不过这套筛选 —— **auditstore._where 的 Python 孪生体**。

    文件后端靠它，库后端在 SQL 里已经筛过、不再走这里。两边必须给出同一个
    答案，由 tests/test_audit_pushdown.py 守着。改这里就得改那边。
    """
    if not (isinstance(rec, dict) and rec.get("trace_id")):
        return False
    if not f.include_started and rec.get("phase") == PHASE_STARTED:
        return False
    if f.trace_id is not None and str(rec.get("trace_id") or "") != f.trace_id:
        return False
    if f.username is not None and str(rec.get("user") or "") != f.username:
        return False
    # `or "ask"` 而不是 `rec.get("kind", "ask")`：后者把"字段缺失"与"字段是空串"
    # 分成两档，而列上折算完只剩空串一档（写入是 str(rec.get("kind") or "")），
    # SQL 再怎么写也分不出来。两边分不出的差别就不该在这里制造 —— 否则
    # 审计列表（信 SQL）与统计（信 matches）会对同一条记录给出不同的归类。
    if f.kind is not None and (rec.get("kind") or "ask") != f.kind:
        return False
    if f.source is not None and str(rec.get("source") or "") != f.source:
        return False
    if f.status is not None and _record_status(rec) != f.status:
        return False
    if f.thread_ids is not None and _thread_of(rec) not in f.thread_ids:
        return False
    if f.since is not None:
        t = _parse_ts(str(rec.get("ts", "")))
        # 时间解析不出来的记录**不放行**，与 _within_since 同一条取舍：
        # 选了"最近 7 天"却混进一条时间不明的记录，比少一条更糟。
        if t is None or t < f.since:
            return False
    return True


def _resolve(src: Any) -> tuple[Any, Path | None]:
    """把入口参数拆成 (库配置 | None, 文件路径 | None)。

    src 是 Config 且凭据库启用时走库，否则一律走文件（Config 就取它的
    audit_log，Path 就是它自己）。这一步单独抽出来，是因为下面三个读取
    入口都要做同一件事，而"读库还是读文件"只该判一次。
    """
    if isinstance(src, Path):
        return None, src
    from . import auditstore

    if auditstore.enabled(src):
        return src, None
    return None, src.audit_log


def _iter_file(path: Path, f: AuditFilter) -> Iterator[dict[str, Any]]:
    """逐行读文件并就地筛。**流式**：整份文件不进内存，只有过筛的记录进。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                      # 撕裂行：跳过，不中断
            if matches(rec, f):
                yield rec


def iter_records(src: Any, f: AuditFilter | None = None) -> Iterator[dict[str, Any]]:
    """按写入顺序流式读出过筛的记录 —— **结果不整份落进内存**。

    需要"全部记录但只做一次遍历"的聚合（任务中心按线程聚合、统计按天分桶）
    该走这里，而不是 read_records：那个会先把列表建出来，一天 1.5 万条的
    量级下，建出来的那一刻就已经是事故。

    库后端走服务端游标（pgstore.iter_rows），**生成器活着就占着一条连接**，
    调用方必须消费完或及时 break —— 池子只有 6 条连接。
    """
    f = f or AuditFilter()
    cfg, path = _resolve(src)
    if cfg is not None:
        from . import auditstore

        yield from auditstore.iter_audit(f)
        return
    yield from _iter_file(path, f)


def read_records(src: Any, *, include_started: bool = False,
                 f: AuditFilter | None = None,
                 limit: int | None = None) -> list[dict[str, Any]]:
    """读出过筛的审计记录，**保持写入顺序**。

    **src 是 Config 就读库，是 Path 就读文件。** 2026-09-09 起生产的凭据落在
    PostgreSQL（见 auditstore 模块开头）；文件那条路留给本机开发与样例配置。
    两边返回的是同一种东西 —— 原样那条 dict。

    **默认滤掉发起记录**（phase=started）：它没有结果、没有成本、没有收尾码，
    进了统计就是把每次调用数成两次、把成功率稀释一半。只有任务中心需要它
    （那一页要回答"有没有一条线程正在跑/跑一半没了"），显式传参取。

    limit 取的是**最新的 N 条**，返回时仍按写入顺序（旧的在前）。取新不取旧
    是唯一说得通的取法：这个函数的调用方要么翻最近的流水，要么找某条 trace，
    没有一处是想看最早那几条。库后端靠 ORDER BY id DESC LIMIT 走索引，
    文件后端靠一个 maxlen 的 deque —— 两边都不会把全部记录建成列表。

    include_started 是 f 之外的独立入参而不是并进去，纯粹为了不动既有调用点；
    两个都给时以 f 为准。
    """
    if f is None:
        f = AuditFilter(include_started=include_started)
    cfg, path = _resolve(src)
    if cfg is not None:
        from . import auditstore

        return auditstore.read_audit(f, limit=limit)
    if limit is None:
        return list(_iter_file(path, f))
    # deque(maxlen=N) 是这里的关键：文件仍然逐行读完（没有别的办法定位"最后
    # N 条"），但同时活着的只有 N 条记录，而不是全部。
    return list(deque(_iter_file(path, f), maxlen=limit))


def _summary(rec: dict[str, Any]) -> dict[str, Any]:
    s = {k: rec.get(k) for k in SUMMARY_FIELDS}
    # 老记录没有 kind 字段：它们全部产生自 /api/ask 链路
    s["kind"] = rec.get("kind", "ask")
    # 角色是后加的字段，老记录没有 —— 如实标"未记录"，别默认成 ANONYMOUS
    s["role"] = rec.get("role") or "（未记录）"
    s["user"] = rec.get("user") or ""
    s["ok"] = not rec.get("rejected_by")
    return s


#: 任务列表的筛选取值里，``all`` 是"不筛"，空串是**一个合法的档**
#: （未记录数据源 / 匿名发起）。用空串当"不筛"的哨兵，这两档就永远选不中 ——
#: /api/audit 的 source 参数踩过同一个坑，那里用 None 区分，这里用 all，
#: 因为界面上的下拉本来就是 all 打头，一路传到底不必再翻译一次。
FILTER_ANY = "all"

def list_audits(
    path: Any, page: int = 1, page_size: int = 10,
    q: str = "", kind: str = "", with_text: bool = True,
    only_user: str | None = None, status: str = "", source: str | None = None,
    user: str | None = None, since: str = FILTER_ANY,
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
    user / since 是筛选条上另外两个下拉：
      · user —— 记录里的发起人。None 才是"不筛"，空串是合法取值（匿名发起）。
        与 only_user 是两回事：那个是**可见范围**（角色决定，退不出去），
        这个是手上的筛选（随时能清）。两层叠加，顺序是先范围后筛选。
        with_text=False 时不接受这个参数（调用方负责挡），理由同 q：
        发起人与问题原文同属"内容"，遮蔽了还能按它筛就是留了一个预言机。
      · since —— 发起时间档，取值同 SINCE_CHOICES。

    返回里额外给 sources 与 users：**当前可见记录里真出现过的**数据源与发起人，
    在其余筛选之前算 —— 否则选中某个源之后，下拉里就只剩这一个选项，
    人就退不回去了。users 在 with_text=False 时为空表：那份名单本身就是内容。
    """
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)

    # ---------------------------------------------------------------------
    # 2026-09-09：下推到 SQL。此前这里是 read_records(path) 取全量、reverse、
    # 一路列表推导筛下来，最后切出 10 条 —— 为了一页记录读全表。
    #
    # 分工按"这一维在不在列上"划，不按"哪个写着方便"：
    #   · only_user / kind / status / source / user  → 都是抽出来的列，进 SQL
    #   · since                                      → 折算成 ts >= 某时刻，进 SQL
    #   · q（关键词）                                → 要匹配问题原文（在 record
    #     jsonb 里，没有索引），留在 Python，但只作用于**已经被上面几维筛窄的
    #     那一份**，而且走流式游标、不整份物化
    #
    # 分面（sources / users）与 total_all 也各自成查询：它们的基数是几十，
    # 却曾经要求把几十万条记录读进内存才能算出来。
    # ---------------------------------------------------------------------
    base = AuditFilter(username=only_user)
    narrowed = AuditFilter(
        username=only_user if user is None else user,
        kind=kind or None,
        status=status or None,
        source=source,
        since=_since_cutoff(since, day_tz(path)),
    )
    # user 与 only_user 撞车时，只有两者相等才可能有记录：可见范围是硬边界，
    # 手上的筛选退不出它。不相等直接置一个永远筛不中的条件，而不是让筛选覆盖范围。
    impossible = (only_user is not None and user is not None and user != only_user)

    cfg, _fpath = _resolve(path)
    if cfg is not None:
        from . import auditstore

        facets = auditstore.audit_facets(base)
        sources = facets["sources"]
        users = ([{"id": w, "name": w or "匿名"} for w in facets["users"]]
                 if with_text else [])
        total_all = auditstore.count_audit(base)
        if impossible:
            total, items = 0, []
        elif q:
            # 关键词只能在 Python 里判，所以这一支流式扫过窄化后的集合，
            # 同时活着的只有命中的那一页（page_size 条）与两个计数器。
            total, items = _scan_page(
                iter_records(path, narrowed), q, with_text, page, page_size)
        else:
            total = auditstore.count_audit(narrowed)
            items = auditstore.page_audit(
                narrowed, offset=(page - 1) * page_size, limit=page_size)
    else:
        # 文件后端（本机开发、样例配置）。走同一套判定，只是数据从文件流出来。
        sources, users, total_all = _facets_from(path, base, with_text)
        total, items = ((0, []) if impossible else _scan_page(
            iter_records(path, narrowed), q, with_text, page, page_size))

    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [_redact(_summary(r), with_text) for r in items],
        # 页面据此显示遮蔽提示，而不是让人以为这些记录本来就没有问题文本
        "text_visible": with_text,
        "sources": sources,
        "users": users,
        "total_all": total_all,
    }


def _since_cutoff(since: str, tz: Any) -> datetime | None:
    """把「发起时间档」折算成一个时刻，好下推成 ts >= %s。

    与 _within_since 是同一套档位，但那边判的是"这条记录属不属于这一档"，
    这边给的是"从哪一刻起算" —— today 一档必须按**声明时区的零点**取，
    直接减 24 小时会把昨天下午的记录也算成今天。
    """
    if since in ("", FILTER_ANY):
        return None
    now = _now_in(tz)
    if since == "today":
        return datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    days = {"7d": 7, "30d": 30}.get(since, 0)
    return now - timedelta(days=days) if days else None


def _q_hit(rec: dict[str, Any], ql: str, with_text: bool) -> bool:
    """关键词命中。发起人与问题原文同属"内容"，一起受 with_text 管：
    只抹显示、仍允许按它搜，等于留了一个预言机（见 list_audits 的说明）。"""
    return (ql in str(rec.get("trace_id", "")).lower()
            or (with_text and ql in str(rec.get("question", "")).lower())
            or (with_text and ql in str(rec.get("user", "")).lower()))


def _scan_page(stream: Iterator[dict[str, Any]], q: str, with_text: bool,
               page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]:
    """流式数总数并切出某一页，**新的在前**。

    记录从流里按写入顺序出来（旧的在前），而页面要新的在前 —— 于是"第 1 页"
    对应的是流的**末尾**。所以这里一遍扫完拿到总数，同时用一个 maxlen 的
    deque 兜住尾部足够多的记录：第 page 页最远也只需要末尾 page*page_size 条。
    同时活着的就是这些，与总量无关。
    """
    ql = q.strip().lower() if q else ""
    need = page * page_size
    tail: deque[dict[str, Any]] = deque(maxlen=need)
    total = 0
    for rec in stream:
        if ql and not _q_hit(rec, ql, with_text):
            continue
        total += 1
        tail.append(rec)
    newest_first = list(tail)[::-1]
    start = (page - 1) * page_size
    return total, newest_first[start:start + page_size]


def _facets_from(path: Any, base: AuditFilter,
                 with_text: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """文件后端的分面与总数：一遍流式扫出来。

    与库后端的 audit_facets 同一口径 —— 数据源名取**最近一条**记录里的写法
    （源改过名之后下拉里该显示新名字），发起人按最近出现的顺序排。
    """
    seen: dict[str, str] = {}
    seen_users: list[str] = []
    total_all = 0
    for r in iter_records(path, base):
        total_all += 1
        sid = str(r.get("source") or "")
        # 流是旧到新，而下拉要"最近用过的在前"：每次出现都先删再追加，
        # 于是字典/列表的插入顺序就是"最后一次出现"的顺序，最后整个倒过来。
        # 只覆盖不重排的话，顺序会变成"第一次出现"，与库后端对不上。
        seen.pop(sid, None)
        seen[sid] = str(r.get("source_name") or r.get("source") or "（未记录数据源）")
        who = str(r.get("user") or "")
        if who in seen_users:
            seen_users.remove(who)
        seen_users.append(who)
    sources = [{"id": sid, "name": name}
               for sid, name in reversed(list(seen.items()))]
    users = ([{"id": w, "name": w or "匿名"} for w in reversed(seen_users)]
             if with_text else [])
    return sources, users, total_all


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


def _now_epoch() -> float:
    """现在（epoch 秒）。**单独一个函数是为了测试能换掉它** ——
    陈旧判定是这个模块里唯一依赖时钟的地方，不抽出来就得在测试里改系统时间。"""
    return datetime.now(timezone.utc).timestamp()


def _age_s(rec: dict[str, Any], now_s: float) -> float:
    """这条记录写下来多久了（秒）。**时间戳读不出来一律当 0** ——
    那等于"刚刚写的"，也就是不判它陈旧。方向是有意选的：宁可让一条真死掉的
    线程多挂一会儿，也不能因为 ts 缺失或格式怪就把正在跑的判成中断
    （那会给出一个「可续跑」入口，点下去续的是一条还在跑的线程）。"""
    ts = rec.get("ts")
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, now_s - dt.timestamp())


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
WAITING_APPROVAL = "waiting_approval"     # 等系统管理员放行（批准后仍需发起人凭票重跑）
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


def stage(rec: dict[str, Any], *, approval_status: str = "",
          review_status: str = "", ops_status: str = "",
          stale: bool = False) -> str:
    """一条记录**当前处在哪一档**，以及言下之意是"下一步该谁动手"。

    三个 *_status 都由调用方从各自的存储取（审计不认识那三套存储，也不该认识
    —— 耦合进来这里就没法单测了）。空串一律表示"还没有结论"。

    ``stale`` 由调用方按时钟判定（见 is_stale_run）：只落了发起记录、而且已经
    过了很久。审计自己不看表 —— 纯函数才好测，时钟一进来就得在测试里冻结它。
    """
    if rec.get("phase") == PHASE_STARTED:
        # 只有发起记录的线程有两种可能：真在跑，或者进程被杀了。审计上分不开，
        # 但**时间能分开** —— 没有哪条查询会跑一刻钟还不收尾（R-17 的 token
        # 上限与执行超时都远在那之前）。超了就不再叫"运行中"：现场要么在检查点
        # 里（可续跑），要么连检查点都没写成（那是执行期故障，该找运维）。
        # 调用方按 graph.is_resumable 核实之后再二选一，见 server 的 /api/tasks。
        #
        # 不做这一步的后果实测过：2026-09-11 线上 8 条线程停在「运行中」，
        # 全部是一个多小时前被杀的进程，没有任何机制会再看它们一眼。
        return INTERRUPTED if stale else RUNNING
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
        # 运维给过结论就不再挂在队列上。两种结论都是终局，都落回"已拦截"：
        # RESOLVED 的下一步在发起人手上（原样重试），WONTFIX 是真的没有下一步。
        # 界面靠 ops_status 把这两种和"护栏拦下"分开讲，别在这里再多开一档 ——
        # 状态档每多一个，前端就要多一处 if，而它们的下一步动作是同一个。
        return REJECTED if ops_status else NEEDS_OPERATOR
    if code == "NO_SQL":
        return WAITING_INPUT
    if approval_status == "REQUESTED":
        # 目前只有 R-11 会开审批单；判据用"有没有未决审批"而不是硬编码规则号，
        # 将来哪条规则接上审批，这里不用改。
        return WAITING_APPROVAL
    if approval_status == "APPROVED":
        # **已批准但还没用掉的票，仍然算"等待审批"这一档。**
        #
        # 2026-09-11 之前这里只认"有没有未决单"，于是批准的那一刻任务就从
        # 「等待审批」掉进「已拦截」—— 发起人刚被通知批下来了，回到界面看到的
        # 却是一个终结态，而那张票还在 approvals 里躺着等人用。闭环就断在这里。
        #
        # 不为它新开一档：对发起人来说这仍然是同一件事的同一个阶段（"我在等这条
        # 查询能跑"），变的只是下一步该谁动手 —— 那句话由 next_actor 讲，
        # 由 approval_status 区分，不需要状态码跟着分裂。
        return WAITING_APPROVAL
    return REJECTED


def _thread_status(last: dict[str, Any], *, approval_status: str = "",
                   review_status: str = "", ops_status: str = "",
                   stale: bool = False) -> str:
    """一条线程现在处于什么状态 —— 看它**最后一条**记录。

    续跑写新 trace 但 thread 不变，所以线程的当前状态永远由最后一条决定；
    归属才看第一条（见 tasks 的说明）。
    """
    return stage(last, approval_status=approval_status,
                 review_status=review_status, ops_status=ops_status,
                 stale=stale)


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
_RISK_MEDIUM = {"R-08", "R-11", "R-17", "R-20", "QUOTA", "NO_EVIDENCE", "UNGROUNDED"}

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
    "NO_EVIDENCE": "没有任何一次查询执行成功，拒绝给出带数字的结论",
    "UNGROUNDED": "结论里的数字追溯不到查询结果，拒绝给出",
    "DATASOURCE": "数据源不可达",
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
    WAITING_APPROVAL: "等系统管理员放行：批准后由你自己凭票重跑",
    NEEDS_OPERATOR: "等运维：数据源连不上或执行期故障，恢复后可重试",
    INTERRUPTED: "可续跑：现场还在检查点里",
}

#: 审批已批准、票还没用掉时的那一句。**与 _NEXT_ACTOR[WAITING_APPROVAL] 是
#: 两句话，不能合并**：两者状态码相同（见 stage 里那段注释），而下一步该谁
#: 动手正好相反 —— 一个在等管理员，一个在等发起人自己。这一列存在的全部
#: 意义就是把这种差别讲清楚。
NEXT_ACTOR_APPROVED = "已批准：回到这条任务点「凭票重跑」，票是一次性的"


def _recent_threads(path: Any, f: AuditFilter, max_threads: int, *,
                    owned_by: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """最近 max_threads 条线程的记录，按线程分好组，**内存与总量无关**。

    **选线程与取记录是两步，用的筛选条件不同。** owned_by 只参与第一步：
    挑出"这个人参与过的"线程，然后**整条**取回来。第二步再按人筛的话，
    同一条线程上别人写的记录会缺失，于是 owner（看首条）、attempts_on_thread、
    最后一条的收尾状态全都算错 —— 而 owner 决定续跑入口对谁开。

    两个后端同一个结果，路数不同：

      · 库后端先在 SQL 里把线程定下来（recent_thread_ids 按 max(id) 排序取前 N），
        再只取这些线程的记录。离开数据库的就只有这 N 条线程。
      · 文件后端只能顺着流走，所以边读边淘汰：每见到一条记录就把它的线程
        挪到末尾，超过 N 条就丢掉最久没动静的那条。流是按时间顺序的，
        因此"最久没动静"与库后端的 max(id) 最小是同一件事。

    淘汰的是**整条线程**而不是单条记录，理由同上。

    文件后端不实现 owned_by 的两步选法（它按全局近况淘汰，再由调用方按
    owner 过滤）：那条路只服务本机开发与样例配置，线程数远不到上限，
    两种选法给出的是同一批线程。生产走的是库后端。
    """
    threads: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    cfg, _ = _resolve(path)
    if cfg is not None:
        from . import auditstore

        select_f = f if owned_by is None else replace(f, username=owned_by)
        tids = auditstore.recent_thread_ids(select_f, limit=max_threads)
        if not tids:
            return {}
        for rec in iter_records(path, replace(f, thread_ids=tuple(tids))):
            threads.setdefault(_thread_of(rec), []).append(rec)
        return dict(threads)

    for rec in iter_records(path, f):
        tid = _thread_of(rec)
        if not tid:
            continue
        if tid in threads:
            threads.move_to_end(tid)
        threads.setdefault(tid, []).append(rec)
        if len(threads) > max_threads:
            threads.popitem(last=False)
    return dict(threads)


#: 任务中心一次最多看多少条线程。
#:
#: 2026-09-09 加。此前这一页没有任何上限：把全部审计记录读进内存、按
#: thread_id 聚合、再整体排序。线程只有几千条，记录却是几十万条 ——
#: 日访问十万级下光这一页就能把 Pod 打死。
#:
#: 2000 是按"这一页实际在回答什么"定的：它问的是"最近发生了什么、有没有
#: 卡住的"，不是"开站以来的全部作业"。真要翻更早的，走审计流水页（那一页
#: 是真分页，翻多久都行）。
#:
#: **这个上限必须被页面说出来**（server 把它放进 window 字段），
#: 否则就是一次静默收窄 —— 看的人会把"最近 2000 条线程里没有"读成"没有"。
TASKS_MAX_THREADS = 2000


def tasks(path: Any, only_user: str | None = None, *,
          max_rows: int = 0, max_scan_rows: int = 0,
          approval_status: dict[str, str] | None = None,
          max_threads: int = TASKS_MAX_THREADS,
          review_status: dict[str, str] | None = None,
          ops_status: dict[str, str] | None = None,
          stale_after_s: int = 0) -> list[dict[str, Any]]:
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
    # 这一页**要**发起记录：一条线程只落了发起、没落收尾，说明它要么正在跑、
    # 要么跑一半进程没了 —— 两种都得看得见，而这正是原来整片丢失的那一档。
    f = AuditFilter(include_started=True)
    threads = _recent_threads(path, f, max_threads, owned_by=only_user)

    approvals = dict(approval_status or {})
    reviews = dict(review_status or {})
    ops = dict(ops_status or {})
    # 「运行中」的陈旧线判定在这里取一次**当前时刻**，不是每条记录各取一次：
    # 一次列表里的几千条必须按同一个"现在"判，否则翻页时同一条线程会在
    # 两次请求之间横跳。
    now_s = _now_epoch() if stale_after_s > 0 else 0.0
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
        item["approval_status"] = approvals.get(trace, "")
        item["ops_status"] = ops.get(trace, "")
        item["stale"] = bool(
            stale_after_s > 0
            and last.get("phase") == PHASE_STARTED
            and _age_s(last, now_s) > stale_after_s
        )
        item["status"] = _thread_status(
            last, approval_status=item["approval_status"],
            review_status=reviews.get(trace, ""),
            ops_status=item["ops_status"], stale=item["stale"])
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
        #
        # 已批准的审批单是同一个状态码下的另一句话（见 NEXT_ACTOR_APPROVED）：
        # 状态没变，但等的人从管理员换成了发起人自己。
        item["next_actor"] = (
            NEXT_ACTOR_APPROVED
            if (item["status"] == WAITING_APPROVAL
                and item["approval_status"] == "APPROVED")
            else _NEXT_ACTOR.get(item["status"], "")
        )
        # 归属如实给出去。空串 = 匿名发起，不是"丢了" —— 页面要能说清这一点。
        item["owner"] = owner
        # 风险档是折算出来的，不是记录里的字段 —— 理由一并给出，页面可解释
        item["risk"], item["risk_why"] = _risk(last, max_rows, max_scan_rows)
        out.append(item)

    out.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return out


#: 发起时间档，与界面上那四项一一对应。写在这里而不是在 server 上，
#: 是为了让"合法取值"只有一份定义 —— 两份就会漂。
SINCE_CHOICES = ("all", "today", "7d", "30d")

TASK_STATUSES = (
    RUNNING, DONE, WAITING_REVIEW, REVIEW_RETURNED, REJECTED,
    WAITING_INPUT, WAITING_APPROVAL, NEEDS_OPERATOR, INTERRUPTED,
)

RISK_LEVELS = ("HIGH", "MEDIUM", "LOW")


def day_tz(src: Any = None) -> Any:
    """按哪个时区算"一天"。

    **必须显式声明，不能跟着进程走。** 线上容器的时钟是 UTC，直接拿
    datetime.now() 的日期当"今天"，北京时间要到早上八点才翻页 —— 于是
    「今日查询」在整个上半天显示的都是昨天那个数，而看的人以为是今天的。
    存 UTC 是对的（带偏移量、跨时区不歧义），错的是展示时不折算回来。

    部署方在 observability.day_utc_offset_hours 里声明（写小时数而不是
    Asia/Shanghai 这种名字：精简镜像里往往没有 tzdata，按名字取时区会在
    生产上直接抛异常，而这件事没有任何本地测试能提前发现）。
    没声明就取进程本地时区 —— 本机开发所见即所得，行为与改动前一致。
    """
    raw = getattr(src, "raw", None)
    if isinstance(raw, dict):
        v = (raw.get("observability") or {}).get("day_utc_offset_hours")
        if v is not None:
            try:
                return timezone(timedelta(hours=float(v)))
            except (TypeError, ValueError):
                pass                     # 配置写坏了就退回本地，别让页面整个起不来
    return datetime.now().astimezone().tzinfo


def _now_in(tz: Any) -> datetime:
    return datetime.now(tz) if tz is not None else datetime.now().astimezone()


def _day_of(ts: str, tz: Any = None) -> date | None:
    """记录落在哪一天。**tz 决定日界**；不给就按记录自己的偏移量算
    （老行为，仅用于没有配置在手的调用点）。"""
    t = _parse_ts(ts)
    if t is None:
        return None
    return (t.astimezone(tz) if tz is not None else t).date()


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
        # 折算到 now 所在时区再比日期 —— now 由调用方按声明时区构造，
        # 两边不在同一个时区上比，"今天"就会差出八小时
        return t.astimezone(now.tzinfo).date() == now.date()
    days = {"7d": 7, "30d": 30}.get(since, 0)
    if not days:
        return True
    # 记录带时区、now 也带（_now 用 astimezone），减法才成立
    return (now - t) <= timedelta(days=days)


def paginate_tasks(
    items: list[dict[str, Any]], *, page: int = 1, page_size: int = 10,
    status: str = FILTER_ANY, source: str = FILTER_ANY,
    risk: str = FILTER_ANY, user: str = FILTER_ANY, since: str = FILTER_ANY,
    q: str = "", tz: Any = None,
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

    q 是筛选条上的关键词，同时匹配问题原文、线程 id 与 trace id。**它必须在
    这里筛**，不能由前端在当前这一页上做 —— 那样搜的是十行，搜不到的东西
    看起来就像不存在。

    这三个数原来都在浏览器里算，代价是每次打开都要把全部线程发过去
    （实测一次一千四百多条）。搬到这里之后出网的只有当前这一页，
    而页面上那几个数字一个不少。
    """
    # tz 由调用方按 observability.day_utc_offset_hours 传进来。不给就退回本地
    # 时区 —— 但在生产（容器时钟为 UTC）那等于按 UTC 计日，「今日完成」会到
    # 北京时间早上八点才翻页
    now = _now_in(tz)

    counts = Counter(str(it.get("status") or "") for it in items)
    done = [it for it in items if it.get("status") == DONE]
    # 时间解析不出来的不计入今天 —— 与 _within_since 同一条口径，
    # 宁可少算一条，也不要把一条时间不明的记录报成"今日完成"
    done_today = sum(1 for it in done
                     if _day_of(str(it.get("ts", "")), now.tzinfo) == now.date())
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
    needle = q.strip().lower()
    if needle:
        matched = [
            it for it in matched
            if needle in str(it.get("question") or "").lower()
            or needle in str(it.get("thread_id") or "").lower()
            or needle in str(it.get("trace_id") or "").lower()
        ]

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


def resumable(path: Any, user: str) -> list[dict[str, Any]]:
    """某个账号名下**尚可续跑**的任务 —— tasks() 里状态仍为中断的那些。

    /api/resume 按 thread_id 从断点继续，只有主人能续 —— 所以这里**仍按
    发起人过滤**，与 tasks() 的"全部可见"有意不同：这个函数回答的是
    "我能续跑哪些"，不是"有哪些线程"。
    """
    return [t for t in tasks(path, user) if t["resumable"]]


def get_audit(path: Any, trace_id: str) -> dict[str, Any] | None:
    """按 trace_id 取完整记录。同 id 多条时取最后一条（重放/重投递场景）。

    下推到 WHERE trace_id = %s（askdb_audit_trace_idx 正为此而建）。
    2026-09-09 之前这里是把全部记录读回来再逐条比对 —— 为了找一条记录
    读几十万条，而索引就在那儿。
    """
    got = read_records(path, f=AuditFilter(trace_id=trace_id), limit=1)
    return got[-1] if got else None


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
    # 空列表一并省掉：tables 只有 schema_recall 那一步填，其余步骤留个 []
    # 会把每条响应撑大一圈，而前端对"没有"和"空"的处理本来就是同一条。
    # 判据仍以 None 为主 —— 换成真值判断会把 ms=0 这类合法零值一起丢掉。
    out["steps"] = [
        {k: s.get(k) for k in STEP_FIELDS
         if s.get(k) is not None and s.get(k) != []}
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


def stats(path: Any, days: int = 30, only_user: str | None = None) -> dict[str, Any]:
    """时间窗内的调用/拦截/成本统计与按日序列。

    trace_complete 按"记录里带步骤级 trace 的占比"如实计算，
    不是写死的 100% —— 页面上那格数字必须经得起对账。

    only_user 的语义与 list_audits 一致，而且**必须一起收敛**：
    列表只给本人、统计却给全量，那张成本卡就是一个按天的聚合泄露 ——
    别人昨天花了多少、被拦了几次，一眼可见。同一道边界只做一半等于没做。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=days)

    # 日界按**声明的时区**算，不是记录字符串的前十位。
    #
    # 原来直接切前十位，等于跟着记录自己的偏移量走（线上是 UTC）：北京时间
    # 凌晨到早八点之间，这些记录还算在"昨天"，于是页面上的「今日查询」在
    # 整个上半天显示的都是昨天的数。
    tz = day_tz(path)
    daily: dict[str, dict[str, Any]] = {}
    by_kind: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_model: dict[str, dict[str, Any]] = {}

    # **一遍流式聚合，不建 recent 列表。**（2026-09-09）
    #
    # 时间窗与发起人下推到 SQL，剩下的在一次遍历里累加。原来是先把窗口内
    # 的记录全部收进 recent，再对它做七八轮 sum/列表推导 —— 30 天窗口在
    # 日访问十万级下就是四十几万条 dict 同时活着，这一页自己就能把 Pod 打死。
    #
    # 现在同时活着的只有几个计数器、几个按天/按模型的小字典，以及 elapsed
    # 这一个 int 列表（分位数要排序，绕不开；但它是 int 不是 dict，
    # 四十几万条也就十几 MB）。
    calls = blocked = with_steps = 0
    cost_total = 0.0
    tok_in_total = tok_out_total = 0
    model_calls = model_failed = 0
    elapsed: list[int] = []
    for r in iter_records(path, AuditFilter(since=cutoff, username=only_user)):
        calls += 1
        if r.get("rejected_by"):
            blocked += 1
        if r.get("steps"):
            with_steps += 1
        elapsed.append(int(r.get("elapsed_ms") or 0))
        cost_total += float(r.get("cost_cny") or 0)
        tok_in_total += int(r.get("tok_in") or 0)
        tok_out_total += int(r.get("tok_out") or 0)

        # 模型调用的成败按**节点**算，不是按整次调用算：一次提问里模型可能被调
        # 三四次（判定 / 生成 / 自检 / 反思），其中一次失败后重试成功，整次调用
        # 是成功的，但模型确实失败过一次。按调用算会把这些失败全部抹掉。
        for st in (r.get("steps") or []):
            if st.get("step") in MODEL_STEPS:
                model_calls += 1
                # 按三档口径判，不是"等于 ok"。切备选成功那条 span 的状态是
                # fallback —— 按等于 ok 判，模型一旦被备选救回来，成功率反而
                # 往下掉；而它真正的失败（那次超时）现在自己就是一条 span，
                # 不需要再从成功的这条身上找补。
                if step_failed(str(st.get("status") or "")):
                    model_failed += 1

        d0 = _day_of(str(r.get("ts", "")), tz)
        day = d0.isoformat() if d0 else str(r.get("ts", ""))[:10]
        d = daily.setdefault(day, {"date": day, "calls": 0, "cost_cny": 0.0})
        d["calls"] += 1
        d["cost_cny"] = round(d["cost_cny"] + float(r.get("cost_cny") or 0), 6)
        by_kind[r.get("kind", "ask")] = by_kind.get(r.get("kind", "ask"), 0) + 1
        if r.get("rejected_by"):
            by_rule[str(r["rejected_by"])] = by_rule.get(str(r["rejected_by"]), 0) + 1
        # 直查不经模型（model=None）不计入模型维度；老记录无 model 字段，
        # 按调用类型如实归为"未记录"而不是猜一个模型名。
        # 缓存命中同样不进这一维：它的 model 字段写的是 "cache"，那不是一个
        # 模型，跟着记一笔会在「按模型」里凭空多出一行，并把前端拿 by_model
        # 求和当分母的「平均 Token」按未发生的调用摊薄。
        # **按 step 归因，不是按记录**。一条链路可能同时烧了三个模型：
        # 生成用主模型、召回用嵌入模型、主模型失败时还切过备选。记录级
        # 只有一个 model 字段，按它分摊的话，嵌入与备选那两笔永远挂在
        # 主模型头上 —— 成本页上「按模型」那张表因此是错的。
        #
        # 老记录的 step 上没有 model（这个字段是 2026-09-10 才落的），
        # 退回记录级那一个，与改造前一致；两种记录混在同一个窗口里也不会
        # 重复计 —— 每条记录只走其中一条路。
        if not r.get("cached"):
            steps = r.get("steps") or []
            # step 上有 model 或有金额，才走按步归因。老记录两样都没有
            # （model 是 2026-09-10 才落到 step 上的），退回记录级那一条路。
            by_step = any(st.get("model") or st.get("cost_cny") for st in steps)
            if by_step:
                for st in steps:
                    m = st.get("model")
                    c = float(st.get("cost_cny") or 0)
                    # **这一步算不算一次模型调用，看的是节点本身，不是它有没有
                    # 记下模型名。** step 级 cost_cny 早就在落盘，step 级 model
                    # 是 2026-09-10 才加的：中间这段时间的记录，每一条的
                    # generate_sql 都是"有金额、无模型名"。原来只在 `if m` 时
                    # 加次数，于是这些记录的钱补挂上去了、次数一次都没加 ——
                    # 生产上因此长出「qwen3.8-flash 6 次 ¥1.64」这种自相矛盾的
                    # 行：¥1.64 实际来自一千二百多次调用，单次成本被算成
                    # ¥0.27（真实值 ¥0.0013），差两个数量级。
                    #
                    # 用 MODEL_STEPS 判而不是"有金额就算"：失败的那次调用金额
                    # 是 0，但它确实调过；反过来，将来某个非模型节点若带上金额
                    # 又没记模型名，只补钱不计次，不会虚增。这也让这张表的次数
                    # 与「模型调用成功率」的分母 model_calls 同源。
                    is_call = bool(m) or st.get("step") in MODEL_STEPS
                    if not is_call and not c:
                        continue
                    # **带金额却没记模型的步骤，钱不能凭空消失**：挂回记录级
                    # 那个模型名。成本表必须满足「各行之和 = 总额」，
                    # 否则它就是一张对不上账的表，而对不上账的成本表
                    # 比没有更坏 —— 看的人不会知道少的是哪一笔。
                    key = str(m) if m else str(r.get("model") or "（未记录）")
                    e = by_model.setdefault(key, {"calls": 0, "cost_cny": 0.0})
                    if is_call:
                        e["calls"] += 1
                    e["cost_cny"] = round(e["cost_cny"] + c, 6)
            else:
                m = r.get("model") or (
                    "（未记录）" if r.get("kind", "ask") == "ask" else None)
                if m:
                    e = by_model.setdefault(m, {"calls": 0, "cost_cny": 0.0})
                    e["calls"] += 1
                    e["cost_cny"] = round(
                        e["cost_cny"] + float(r.get("cost_cny") or 0), 6)

    elapsed.sort()
    return {
        "days": days,
        "calls": calls,
        "blocked": blocked,
        "block_rate": round(blocked / calls, 4) if calls else 0.0,
        "cost_cny": round(cost_total, 6),
        "tok_in": tok_in_total,
        "tok_out": tok_out_total,
        "trace_complete": round(with_steps / calls, 4) if calls else None,
        # 窗口内一次模型节点都没有时为 None —— 0/0 不是 0%，也不是 100%
        "model_calls": model_calls,
        "model_failed": model_failed,
        "model_success": round((model_calls - model_failed) / model_calls, 4) if model_calls else None,
        "elapsed_p50_ms": _percentile(elapsed, 0.5),
        "elapsed_p95_ms": _percentile(elapsed, 0.95),
        # **窗口内每一天都要有一条，没记录的补 0。**
        # 缺天不补的后果不是"少一根柱子"：页面取的是这个序列的最后一条当
        # 「今日」、倒数第二条当「昨日」，一旦今天还没人查，最后一条就是
        # 最近有数据的那天 —— 于是「今日查询」显示的是昨天甚至上周的数字，
        # 而且不会归零。
        "daily": _fill_days(daily, _now_in(tz), days),
        "by_kind": by_kind,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_cny"])),
    }


def _fill_days(daily: dict[str, dict[str, Any]], now: datetime,
               days: int) -> list[dict[str, Any]]:
    """把窗口内缺席的日子补成 0，并保证**最后一条就是今天**。

    窗口起点与 stats 的 cutoff 对齐（now - days），终点固定是今天 ——
    "今天还没有任何调用"是一个要如实说出来的状态，不是"这一天不存在"。
    """
    out: list[dict[str, Any]] = []
    start = (now - timedelta(days=days)).date()
    today = now.date()
    seen = set()
    cur = start
    while cur <= today:
        key = cur.isoformat()
        seen.add(key)
        out.append(daily.get(key) or {"date": key, "calls": 0, "cost_cny": 0.0})
        cur += timedelta(days=1)
    # 窗口之外的记录（时间戳解析不出来、或时钟漂到未来）照样列出来，
    # 别让它们从统计里凭空消失
    out.extend(v for k, v in daily.items() if k not in seen)
    return sorted(out, key=lambda d: d["date"])


def _pctl_of(values: list[int], q: float) -> int | None:
    """按最近秩取分位。样本少时等于某个真实观测值 —— 见 _percentile 的说明。"""
    return _percentile(sorted(values), q)


def quality(path: Any, days: int = 1) -> dict[str, Any]:
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
    # **只读两个窗口，不读全部历史。**（2026-09-09）
    #
    # 原来是 read_records(path) 拿全量再切窗口 —— 这一页默认 days=1，
    # 为了一天的数据把开站以来的每一条都读进内存。下推 since=prev_cutoff
    # 之后，进来的就只有这两个等长窗口本身。
    recent, previous = [], []
    for r in iter_records(path, AuditFilter(since=prev_cutoff)):
        t = _parse_ts(str(r.get("ts", "")))
        if t is None:
            continue
        if t >= cutoff:
            recent.append(r)
        else:
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
        "first_ts": _first_ts(path),
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
            if not step_failed(str(st.get("status") or "")):
                e["ok"] += 1
            else:
                # 失败原因取这一步自己的 note —— 设计稿那张表最右列问的是
                # "这个工具主要死在什么上"，只有节点自己的 note 答得了
                e["fail_notes"][str(st.get("note") or st.get("status") or "未记录原因")] += 1
            e["ms"].append(int(st.get("ms") or 0))
            e["tok"] += int(st.get("tok_in") or 0) + int(st.get("tok_out") or 0)
    return nodes


def _first_ts(src: Any) -> str | None:
    """有记录以来最早那条的时间戳。

    2026-09-09 由"接收全量列表"改为"自己按写入顺序流式取头几条"：调用方
    （quality）原来为了这一个字段把全部历史读进内存，而这里要的只是**第一条
    能解析出时间的记录**。库后端走服务端游标，取到就 break，实际只发生一次
    FETCH；文件后端读到第一条就停。
    """
    stream = iter_records(src)
    with closing(stream):
        return _first_parseable_ts(stream)


def _first_parseable_ts(stream: Iterator[dict[str, Any]]) -> str | None:
    for i, r in enumerate(stream):
        ts = str(r.get("ts", ""))
        if _parse_ts(ts) is not None:
            return ts
        if i >= 200:
            # 开头连着两百条都没有可解析的时间，那不是"还没找到"，是这份
            # 流水的开头坏了 —— 继续扫下去只会把整份读完，而它正是这次要消灭的形状。
            break
    return None
