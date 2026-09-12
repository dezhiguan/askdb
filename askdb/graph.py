"""LangGraph 状态机 —— 链路主干。

选择状态机而非线性链的原因：存在条件分支（校验失败回边）与重试计数，
且需支持检查点以便失败复现（技术设计说明书 §5）。

**状态必须全部可序列化。** 运行时依赖（配置、模型客户端、执行器、追踪器）
一律走 configurable 传入，不进状态 —— 否则检查点存不下，
"失败样本可原样复现"这条就落不了地，P3 的失败归因也就无从谈起。

P0 为单步链路；plan / assess 两个节点与重规划回边在 P5 补齐，
届时只需新增节点与条件边，现有节点不动。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from . import clarify, guard, observe, planner, schema_rag
from .config import Config
from .executor import DataSourceError, Executor, MaskUnresolved
from .llm import LlmClient, LlmNotConfigured
from .quota import QuotaExceeded, build_quota
from .audit import MODEL_STEPS, PHASE_STARTED, RESULT_PREVIEW_ROWS, day_tz
from .trace import Tracer, now_iso, write_audit










@dataclass
class AskResult:
    ok: bool
    question: str
    trace_id: str
    org_id: int

    sql_raw: str = ""
    sql_final: str = ""
    reasoning: str = ""
    rules_fired: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    as_of: str = ""
    # EXPLAIN 估算的扫描行数（R-11 的判定依据）。原来只进审计不出接口，
    # 界面因此说不出"返回 3 行是从多少行里筛出来的" —— 而这正是判断
    # 一条查询贵不贵、结果可不可信的关键一维
    explain_rows: int | None = None

    rejected_by: str | None = None
    error: str = ""
    hint: str = ""
    #: 结果范围被收窄过（原查询被 R-11 拦下，重试时模型自行加了过滤条件）。
    #: 不出接口就等于没修：这类答案在页面上与全量结果毫无区别。
    scope_narrowed: bool = False
    #: 收窄前那条被拦下的 SQL 与它的预估扫描量，用于向用户说明差在哪。
    scope_note: str = ""
    #: 护栏放行但需提醒的事项（R-24：相对时间问题配了写死的日期）。
    guard_notes: list[str] = field(default_factory=list)
    #: 空结果 / 全零结果的说明。空结果与"真的是 0"在页面上长得一模一样，
    #: 不写这一句，"昨天没有数据"就会被读成"昨天一单都没有"。
    empty_note: str = ""

    tables_hit: list[str] = field(default_factory=list)
    metrics_hit: list[str] = field(default_factory=list)
    #: 召回是盲选：给模型的表不是按相关度选出来的，答案可能答非所问。
    #: 出接口而不只进审计 —— 事后能查出来，救不了正在看这个数字的人。
    recall_blind: bool = False
    recall_note: str = ""
    #: 召回降级过。出接口的理由与 recall_blind 一字不差：事后能查出来，
    #: 救不了正在看这个数字的人。
    recall_degraded: bool = False
    #: 脱敏判定退化过（SQL 解析不出，整行按敏感处理）。
    mask_degraded: bool = False
    #: 本次实际脱敏的列。
    masked_columns: list[str] = field(default_factory=list)
    #: 问句是纯指代追问。
    anaphoric: bool = False
    #: 推理里的猜测措辞。非空 = 模型自认不确定，可信度必须跟着降。
    hedge_terms: list[str] = field(default_factory=list)
    #: 从推理里抹掉的假陈述。留档，让"我们改了模型的话"这件事本身可审计。
    scrubbed_claims: list[str] = field(default_factory=list)
    #: 用到的缓存/派生计数列。这类列与真实计数会漂移，出现即降可信度。
    derived_columns: list[str] = field(default_factory=list)
    #: 结论里追溯不到任何查询返回值的大额数字（BUG-A5）。非空 = 这些数不是从
    #: 库里来的。出接口而不只进审计：事后查得出来，救不了正在看这个数字的人。
    ungrounded_numbers: list[str] = field(default_factory=list)
    #: 本次口径声明，必出。
    caliber: str = ""
    attempts: int = 1
    step_count: int = 1
    multi_step: bool = False
    sub_steps: list[dict[str, Any]] = field(default_factory=list)
    converged_early: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    # 检查点线程。普通提问 == trace_id；续跑时保持原任务的线程不变，
    # 而 trace_id 每次执行新开 —— 审计里由此能看出"这是第 2 次执行"。
    thread_id: str = ""
    elapsed_ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cost_cny: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["rows"] = [[jsonable(v) for v in r] for r in self.rows]
        return d


def _io_json(obj: object) -> str:
    """把一个对象折成可读 JSON，进 span 的输入/输出。

    与 agent._io_json 同一口径。**取不到就退回 str()，绝不抛**：
    观测字段把主链路弄崩是最坏的结果。截断由 tracer.add 统一做
    （见 trace.clip_io），这里不重复一套上限。
    """
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(obj)


def jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, Decimal):
        return _decimal_str(v)
    return str(v)


def _decimal_str(v: Decimal) -> str:
    """Decimal 转可读文本。

    PostgreSQL 的除法会返回高标度 numeric，而 ``str(Decimal)`` 对这类值走科学计数法：
    比率为 0 时显示成 ``0E-20``，看的人根本认不出这是 0。统一改成定点写法，并去掉
    标度带来的无意义末尾零。
    """
    if not v.is_finite():          # NaN / Infinity 保持原样
        return str(v)
    # normalize: 0E-20 → 0、1.500 → 1.5；但整数会变成 1E+3，再用定点格式化修回来
    return format(v.normalize(), "f")


# --------------------------------------------------------------------------








# 节点
# --------------------------------------------------------------------------









# 模型没出 SQL（NO_SQL）时给用户的"下一步"。原来是一句写死的
# "在 config/tables.yaml 中开放更多表" —— 对匿名访客暴露内部配置路径、也没人改得了，
# 而且对"删库/预测/返回密码"这类被拦的请求同样弹这句，把安全拒绝说成"表不够"，误导。
# 改为按模型 reasoning 的意图分型给友好文案，且**任何身份都不出现内部文件路径**。
_WRITE_MARKERS = ("写操作", "只读", "update", "delete", "insert", "改名", "修改", "删除",
                  "插入", "新建", "更新", "truncate", "drop", "alter", "授权", "权限", "写入")














#: `<某某>.name = '字面量'` 这种**未做规范化**的名称等值。带了 LOWER/REPLACE/TRIM
#: 的不算 —— 那已经是规范化过的写法，零行多半真的是没有。
_NAME_EQ = re.compile(
    r"(?<![\w.(])(?:\w+\.)?(?:name|title|display_name|username|slug|label)\s*=\s*'([^']{1,60})'",
    re.IGNORECASE)
















# --------------------------------------------------------------------------
# 路由 —— 只读状态，不碰运行时依赖
# --------------------------------------------------------------------------

















def compile_with_checkpoint(g: "StateGraph", target: Any = None):
    """给**任意**骨架接上检查点。

    2026-09-12 从 build_graph 里拆出来：老管道删掉之后，用它的只剩 agent 图
    （agentgraph.build_skeleton）。**不许再写第二份 saver 配置** —— 两份就会
    出现"审计在库里、检查点还在某台机器的本地盘上"，而一次失败复现要两者对得上。

    target 三种：None 不接检查点；Path 走 SQLite；Config 由部署决定
    （observability.store: postgres 时落 PostgreSQL）。检查点跟着凭据走同一个
    开关，不单独设一个。
    """
    if target is not None and not isinstance(target, Path):
        from . import auditstore

        cfg = target
        if not auditstore.enabled(cfg):
            return compile_with_checkpoint(g, cfg.checkpoint_db)
        return _build_pg(g)

    checkpoint_db = target
    if checkpoint_db is None:
        return g.compile()
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
    # 多副本共享同一个检查点库时，这两条是能不能跑的分界线：
    #
    #   WAL          默认的 DELETE 模式下写会阻塞读，两个 Pod 同时跑必然互相踩。
    #                WAL 允许"多读 + 单写"并行，读方完全不受写方影响。
    #                前提是同一台机器的本地盘（这里是 hostPath），网络盘上 WAL 不可用。
    #   busy_timeout 抢不到锁时默认**立刻**报 database is locked。给 5 秒等待窗口，
    #                把"直接失败"变成"稍等一下"—— 检查点写入是毫秒级的，
    #                5 秒足够排队，真等满了那是别的地方出了问题。
    #
    # 单副本下这两条也没有坏处，所以不做条件判断，一律打开。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")   # WAL 下的推荐档位，掉电最多丢最后几次写
    saver = SqliteSaver(conn)
    saver.setup()
    compiled = g.compile(checkpointer=saver)
    compiled._askdb_conn = conn          # 持有连接，避免被 GC 关掉
    return compiled


def _build_pg(g):
    """检查点落 PostgreSQL。

    多副本共享检查点这件事，SQLite 那条路是靠"同一台机器的本地盘 + WAL"
    撑住的；换成 PG 之后这个前提不再需要 —— 副本落在哪个节点都一样，
    而这正是原来那套写法唯一守不住的地方。

    行工厂必须是 dict（saver 的硬要求），所以走 pgstore 的第二个池子。
    setup() 幂等，建表建索引，每个进程跑一次即可。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from . import pgstore

    saver = PostgresSaver(pgstore.dict_pool())
    saver.setup()
    return g.compile(checkpointer=saver)


_GRAPH = None
_GRAPH_KEY: str | None = None






def _pg_key() -> str:
    from . import pgstore

    return f"{pgstore.raw_dsn()}|{pgstore.schema()}"






#: 续跑被前置校验挡下时写进审计的 rejected_by。**不是终态** ——
#: 检查点还在，条件恢复之后这条线程仍然可以续跑，所以任务状态里它
#: 与 INTERRUPTED 同档（见 audit._thread_status）。
RESUME_BLOCKED = "RESUME_BLOCKED"


@dataclass
class ResumeBlock:
    """续跑前置校验没过的原因。code 进审计，error/hint 给人看。"""

    code: str
    error: str
    hint: str


def precheck_resume(cfg: Config, values: dict[str, Any],
                    ex: Executor) -> ResumeBlock | None:
    """续跑前重新校验权限、Schema 与数据源连接状态，都过才返回 None。

    三项的顺序是有意的：先判不需要 IO 的权限，再判连接（连不上后面两项
    都问不出来），最后比对表结构。

    **权限**看的是**现在**的可见范围：cfg 已经按调用方当前角色收窄过
    （server._scoped），中断期间被收回的表在这里就落不到白名单里。
    **Schema** 比对白名单声明的列与库里实际的列：中断期间改过表结构的话，
    检查点里那条 SQL 引用的列可能已经不存在 —— 直接跑下去要么报错，
    要么更糟：列名被复用成了别的语义，跑通了但答的是另一件事。
    """
    hit = [str(t) for t in (values.get("tables_hit") or [])]

    revoked = [t for t in hit if t not in cfg.tables]
    if revoked:
        return ResumeBlock(
            code=RESUME_BLOCKED,
            error=f"续跑前校验未通过：这条任务用到的表现在不在可见范围内（{'、'.join(revoked)}）",
            hint="权限或白名单在中断期间收窄了。恢复权限后可以再续跑，检查点仍然保留。",
        )

    try:
        ex.connect()
    except DataSourceError as e:
        return ResumeBlock(
            code=RESUME_BLOCKED,
            error=f"续跑前校验未通过：数据源当前连不上（{e}）",
            hint=e.hint or "数据源恢复后可以再续跑，检查点仍然保留。",
        )

    if hit:
        try:
            actual = ex.describe(hit)
        except Exception as e:        # noqa: BLE001 —— 取不到结构就不放行
            return ResumeBlock(
                code=RESUME_BLOCKED,
                error=f"续跑前校验未通过：读不到表结构（{e}）",
                hint="数据源恢复后可以再续跑，检查点仍然保留。",
            )
        drift: list[str] = []
        for name in hit:
            cols = {str(c.get("name")) for c in (actual.get(name) or [])}
            if not cols:
                drift.append(f"{name}（表已不存在）")
                continue
            gone = [c for c in cfg.tables[name].columns if c not in cols]
            if gone:
                drift.append(f"{name}.{'、'.join(gone)}")
        if drift:
            return ResumeBlock(
                code=RESUME_BLOCKED,
                error=f"续跑前校验未通过：表结构在中断期间变了（{'；'.join(drift)}）",
                hint="检查点里的执行计划基于旧结构，续跑会答错。请重新提问，"
                     "或把白名单与库对齐后再续跑。",
            )
    return None




def _answering_model(steps: Any) -> str:
    """这次链路里**真正出活**的那个模型。

    优先取 generate_sql 那条 —— 答案是那条 SQL 查出来的，它由谁生成，
    这次结果就该记在谁头上。**不能笼统取"最后一条模型 span"**：多步链路里
    判定与自检也各是一次模型调用，而备选只在失败时顶上一次，下一次调用会
    回到主模型。于是"生成切了备选、自检又回到主模型"这种链路，按最后一条取
    就又记成主模型 —— 正是这次要修的那个错，绕一圈原样回来。

    没有 generate_sql（规划失败等）才退回最后一条成功的模型 span；
    一条都没有（直查、命中缓存、老记录）返回空串，交给调用方兜底。
    """
    # **只看真正过模型的节点。** schema_recall 现在也带 model（嵌入模型），
    # 不排除的话，生成失败的链路会退到它头上 —— 审计里就记着"这次由
    # text-embedding-v4 应答"，而嵌入模型一句话都没生成过。
    rows = [st for st in (steps or [])
            if st.get("step") in MODEL_STEPS
            and st.get("status") in ("ok", "fallback") and st.get("model")]
    for st in reversed(rows):
        if st.get("step") == "generate_sql":
            return str(st["model"])
    return str(rows[-1]["model"]) if rows else ""


def _audit_of(result: AskResult, cfg: Config, kind: str,
              explain_rows: Any = None) -> dict[str, Any]:
    """审计记录统一在这里成形 —— ask / resume / 中断三条路共用一个形状。"""
    return {
        "trace_id": result.trace_id, "ts": now_iso(), "kind": kind,
        "thread_id": result.thread_id,
        # **实际应答的模型**，不是配置里声明的主模型。原来这里直接写
        # cfg.llm["model"]，主模型超时切备选跑完，审计里照样记着主模型 ——
        # 页面看不出回退只是表象，库里存的本来就是错的，按模型分摊的成本
        # 也跟着记到了没出活的那个头上。
        "model": _answering_model(result.steps) or cfg.llm.get("model"),
        "org_id": result.org_id, "question": result.question,
        # 结果出自谁的可见范围 —— 少了它，同一个问题在不同角色下拿到不同行数，
        # 事后无从解释
        "role": cfg.role or "ANONYMOUS",
        "user": cfg.user or "",
        # 打的哪个库。多源之后少了它，同一条 SQL 在不同数据源上的结果
        # 事后完全对不上账
        "source": cfg.source_id or "builtin",
        "source_name": cfg.source_name or cfg.path,
        "tables_hit": result.tables_hit, "metrics_hit": result.metrics_hit,
        # 召回是不是盲选、脱了哪几列 —— 两者都必须进审计。
        # 事后复盘一条可疑结果时，第一个要回答的问题就是"模型当时看得见
        # 该看的那张表吗"；脱敏同理，不记就无从证明当时到底脱没脱。
        "recall_blind": result.recall_blind,
        "recall_degraded": result.recall_degraded,
        # 范围被收窄过。与 recall_blind 同一类信息：链路全绿、结果却不可全信，
        # 事后复盘一个对不上的数字时，这是第一个要看的字段。
        "scope_narrowed": result.scope_narrowed,
        "masked_columns": result.masked_columns,
        "mask_degraded": result.mask_degraded,
        # 语义侧的三个信号。与 recall_blind / scope_narrowed 同一类：链路全绿、
        # 结果却不可全信。**必须进审计**，否则追踪页那枚可信度角标与工作台右栏
        # 会算出两个分数 —— 同一次查询两页两个答案，可信侧栏就此作废。
        "hedge_terms": result.hedge_terms,
        "derived_columns": result.derived_columns,
        "anaphoric": result.anaphoric,
        # 我们改过模型的话这件事本身也要可审计
        "scrubbed_claims": result.scrubbed_claims,
        "caliber": result.caliber,
        # 结果被 R-13 截断。与上面两条同类：不进审计的话，事后在追踪页上
        # "只看到前 N 行"和"一共就这么多行"长得一模一样。
        "truncated": result.truncated,
        "sql_raw": result.sql_raw, "sql_final": result.sql_final,
        "rules_fired": result.rules_fired, "rejected_by": result.rejected_by,
        "attempts": result.attempts, "explain_rows": explain_rows,
        "step_count": result.step_count, "multi_step": result.multi_step,
        "converged_early": result.converged_early,
        "rows_returned": result.row_count,
        # 最终结果：答案文本 + 结果列 + **已脱敏**结果行前 N 行。用于登录态的
        # /api/result（追踪详情与任务中心展示"最终结果"）。行来自 result.rows，
        # 已经过 executor 脱敏；这里只截前 N 行、并 jsonable 化（Decimal/时间转字符串）
        # 免得写审计时 json 序列化失败。SQL 全文不进这里 —— 它仍走 replay 那道门。
        "answer": result.reasoning,
        "columns": list(result.columns),
        "rows_preview": [[jsonable(v) for v in r] for r in (result.rows or [])[:RESULT_PREVIEW_ROWS]],
        "elapsed_ms": result.elapsed_ms,
        "tok_in": result.tok_in, "tok_out": result.tok_out,
        "cost_cny": result.cost_cny, "steps": result.steps,
    }








