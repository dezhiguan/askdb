"""FastAPI 接口与静态页面。

接口设计上有意让**失败也是结构化的**：护栏拦截不是 HTTP 500，
而是 200 + ok:false + rejected_by/hint，前端才能给出针对性的提示。
只有服务本身坏了（配置错误、数据源不可用）才用非 2xx。
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import approvals as _approvals
from . import audit as _audit
from . import reviews as _reviews
from . import auth as _auth
from . import evalrun as _evalrun
from . import guard
from . import identity as _identity
from . import pgstore as _pgstore
from . import schema_rag as _schema_rag
from . import sources as _sources
from .config import Config, load
from .executor import DataSourceError, Executor
from .graph import ask as run_ask, jsonable, resume as run_resume
from .quota import build_quota
from .qcache import build_answer_cache, make_key as _cache_key
from .trace import now_iso as _now_iso, observability_status as _obs_status


def _mask_pii(s: str) -> str:
    """把 PII 文本脱敏成"看得出形状、读不出内容"：字母数字与 CJK 等字符一律换成
    实心点,空格与 · / - . 等分隔符保留,长度不变。

    用途:未登录查看成员名册时,姓名 / 网关用户名 / 备注在**下发前**就抹掉明文
    —— 前端的模糊只是视觉层,真正不外泄靠这里(F12 看到的也只是圆点)。
    保留分隔符与长度,是为了页面上仍能看出"有一列列真实成员",而不是一片空白。
    """
    keep = {" ", "\t", "\u00b7", "\u30fb", "-", "/", ".", ",", "@", "_", "#", "(", ")"}
    return "".join(c if (c in keep or c.isspace()) else "\u2022" for c in (s or ""))


def _quota_view(cfg: Config) -> dict[str, Any]:
    """配额现状。计数后端是 file 还是 redis 必须暴露出来 —— 多副本部署下
    file 后端等于每个副本各算各的，上限被悄悄乘以副本数。"""
    dq = build_quota(cfg)
    used = dq.peek()
    return {
        "limit": dq.limit,
        "used": used,
        "remaining": max(dq.limit - used, 0) if dq.enabled else None,
        "backend": dq.kind,
        "multi_replica_safe": dq.kind in ("redis", "none"),
    }

#: 发布门禁阈值与性能目标 —— **项目策略，不是测量值**。
#: 放在模块级而不是埋进函数：改这两个数就是改"什么样算能发布"，
#: 那该是一次显式决定，而不是顺手调一下常量。
_RELEASE_GATE = 90.0
_P95_TARGET_MS = 4000

#: 未登录也能调的「非写入」路由。**这是一张豁免表，不是黑名单** ——
#: 写入拦截默认拒绝一切 POST/PUT/PATCH/DELETE，只有列在这里的才放行。
#:
#: 方向是有意的：列黑名单的话，将来新增一个写接口而忘了登记，它就是敞开的，
#: 且没有任何信号会提醒谁。默认拒绝时，忘了登记的后果是「新接口要登录才能用」
#: —— 漏掉的方向落在安全的那边。
#:
#: 往这张表里加一条 = 显式声明「这个接口未登录也能调」，请当成一次安全决定来 review。
#: ask / sql / resume 是 POST，但它们是**查询**：读走的是角色收窄那条路（_scoped），
#: 不归写入拦截管。auth 两条是认证本身，拦了就没人能登录了。
_WRITE_EXEMPT_PATHS = frozenset({
    "/api/ask",
    "/api/sql",
    "/api/resume",
    # 登录入口自己必须在豁免里，否则是一扇锁着钥匙的门：要登录才能调登录接口。
    # /logout 同理 —— 会话已经过期的人也得能清干净 cookie。
    "/api/auth/login",
    "/api/auth/logout",
})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 被写门拦下时，用来把话说到**这一次点的那个动作**上。
#:
#: 值得多这一张表的理由：写门挡下的并不都是"改配置"。运行一轮离线回归既不改
#: 配置也不改数据，它只是要占模型额度；对着那个按钮回一句"这是一个会改动配置
#: 的操作"，读的人拿到的是一句错话，而他手上只有这一句话可看
#: （2026-09-09 就是这么问过来的）。
#:
#: 中间件跑在路由匹配之前，拿不到路由模板，只有具体路径，所以这里按
#: 「方法 + 路径形状」自己认一遍，`*` 匹配任意一段。认不出来时退回通用说法 ——
#: 新增写接口忘了登记，后果只是话说得笼统一点，不会漏掉拦截本身。
_WRITE_ACTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("POST", ("api", "eval", "run"), "运行回归评测"),
    ("POST", ("api", "sources", "test"), "测试数据源连接"),
    ("POST", ("api", "sources"), "接入数据源"),
    ("PUT", ("api", "sources", "*", "tables"), "调整数据源的可查表"),
    ("DELETE", ("api", "sources", "*"), "移除数据源"),
    ("POST", ("api", "identity", "members"), "新增成员"),
    ("DELETE", ("api", "identity", "members", "*"), "移除成员"),
    ("POST", ("api", "approvals", "*", "decide"), "审批这条申请"),
    ("POST", ("api", "reviews", "*", "decide"), "提交复核判定"),
)


def _write_action_name(method: str, path: str) -> str:
    """这次被拦下的写操作叫什么。认不出来返回空串。"""
    segs = tuple(seg for seg in path.split("/") if seg)
    for want_method, pattern, name in _WRITE_ACTIONS:
        if want_method != method or len(pattern) != len(segs):
            continue
        if all(p == "*" or p == seg for p, seg in zip(pattern, segs)):
            return name
    return ""

#: 读操作里不要求登录的路径。**白名单是穷举的**，新接口默认要登录 ——
#: 与写门同一个理由：漏掉的方向必须落在安全的那边。
#:
#: 这几条各有各的理由，不是随手放的：
#:   · /api/health   页面加载时就要调，它决定要不要显示配置横幅
#:   · /api/auth/me  登录页自己要靠它判断"要不要显示登录框"
#:   · /api/identity/roles  角色定义写死在源码里，本就不是秘密
_READ_EXEMPT_PATHS = frozenset({
    "/api/health",
    "/api/auth/me",
    "/api/identity/roles",
    "/api/docs",
    "/api/openapi.json",
})

WEB = Path(__file__).resolve().parent / "web"
# 换壳前的单文件页面。它仍然是唯一一处接了真实数据的界面 ——
# 新前端把后端能力接回来之前，不能只剩一个查不了数的壳，所以留在 /legacy。
WEB_LEGACY = Path(__file__).resolve().parent / "web_legacy"

# 回放 id 严格校验：12 位十六进制，命中与未命中同为 404
_TRACE_ID_RE = __import__("re").compile(r"[0-9a-f]{12}")


class _RateLimit:
    """进程内固定窗口限流，**按调用方分桶**。

    单独限流而不是复用全局配额：回放不花 token，但每次都要开 SQLite
    遍历检查点历史 —— 防的是把它当查询接口刷（设计说明 §5.1）。

    分桶而不是所有人共用一个计数器：它要防的是**单个调用方**刷接口，而
    共用计数器的实际效果是"任何一个人手快一点，所有人一起被锁在门外"。
    一个人还能不能用，不该由别人的用量决定。
    """

    def __init__(self, limit: int = 30, window_s: int = 60) -> None:
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, list[float]] = {}

    def _fresh(self, key: str, now: float) -> list[float]:
        """窗口内还算数的那些时间戳，顺手把空桶收掉。"""
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window_s]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def allow(self, key: str = "") -> bool:
        import time as _t

        now = _t.monotonic()
        # 桶数上限：key 来自登录名或来源地址，都由外部决定，不清理就是一条
        # 随请求增长的内存占用
        if len(self._hits) > 256:
            for k in list(self._hits):
                self._fresh(k, now)
        hits = self._fresh(key, now)
        if len(hits) >= self.limit:
            return False
        self._hits[key] = hits + [now]
        return True

    def retry_after(self, key: str = "") -> int:
        """还要等几秒才会放行 —— 窗口里最早那次滑出去就腾出一个名额。

        「稍后再试」是一句没用的话：稍后是多久，只有限流器自己知道。
        """
        import time as _t

        now = _t.monotonic()
        hits = self._fresh(key, now)
        if len(hits) < self.limit:
            return 0
        return max(1, int(self.window_s - (now - min(hits))) + 1)


_REPLAY_RL = _RateLimit()

# 数据源接口的两份预算。分的是**地址由谁定**，不是读还是写：
#
# 服务端会按请求主动建连，被拿去当端口扫描器的是"调用方随手填一个地址"
# 那条路 —— 也就是 /sources/test 与 POST /sources。至于扫描已注册的源、
# 改它的白名单，连的是库里早就存着的那个地址，扫不出任何新东西，它只是
# 一次正常的开销。两者共用一份预算的结果，是点几下配置弹窗就把真正该防
# 的那份额度花光，而防住的东西一样没多。
_SOURCE_DIAL_RL = _RateLimit(limit=10, window_s=60)
_SOURCE_MANAGE_RL = _RateLimit(limit=30, window_s=60)
#: /api/health?probe=1 的实证要连库跑两条语句。页面加载调的是不带 probe 的那条，
#: 不受影响；这里防的是有人拿它当查询接口刷。部署后冒烟一次一条，6/min 绰绰有余。
_PROBE_RL = _RateLimit(limit=6, window_s=60)


def _paired_delta(base: list[dict], other: list[dict]) -> dict[str, Any] | None:
    """两组在**同一批题**上的差异，以及它的置信区间与显著性。

    为什么不能直接看两条独立置信区间：各组跑的是完全相同的题目（§6.4
    第 3 条），是配对设计。配对检验只关心"谁翻了盘"——A 错 B 对多少题、
    A 对 B 错多少题 —— 比各算各的区间灵敏得多，也才是这份数据该用的方法。

    返回的 CI 是**差值的**区间。它是否跨过 0，直接回答"这个差异说明得了
    问题吗"，而柱状图的长短回答不了。
    """
    import math

    ba = {o["id"]: o["passed"] for o in base if o.get("category") != "reject"}
    bo = {o["id"]: o["passed"] for o in other if o.get("category") != "reject"}
    ids = [i for i in ba if i in bo]
    n = len(ids)
    if not n:
        return None
    b01 = sum(1 for i in ids if not ba[i] and bo[i])      # 变好
    b10 = sum(1 for i in ids if ba[i] and not bo[i])      # 变坏
    d = (b01 - b10) / n
    # 配对比例差的 Wald 标准误（McNemar 型）
    var = (b01 + b10 - (b01 - b10) ** 2 / n) / (n * n)
    se = math.sqrt(max(var, 0.0))
    lo, hi = d - 1.96 * se, d + 1.96 * se

    m = b01 + b10
    if m == 0:
        pv = 1.0
    else:                                                  # 精确二项（双侧）
        k = min(b01, b10)
        pv = min(1.0, 2 * sum(math.comb(m, i) for i in range(k + 1)) / 2 ** m)
    return {"delta": d, "lo": lo, "hi": hi, "improved": b01,
            "regressed": b10, "p": pv, "n": n}


def _by_category(outcomes: list[dict]) -> dict[str, list[int]]:
    """按题型的 [答对, 总数] —— 信息量最大的一张表，比总分有用得多。"""
    agg: dict[str, list[int]] = {}
    for o in outcomes:
        if o.get("category") == "reject":
            continue
        a = agg.setdefault(o.get("category", "?"), [0, 0])
        a[0] += bool(o.get("passed"))
        a[1] += 1
    return agg


def _dsn_brief_id(cfg: Config) -> str:
    """数据源身份标识 —— 必须与评测出处里记的格式逐字一致，否则永远判不一致。"""
    kv = dict(x.split("=", 1) for x in cfg.dsn.split()
              if "=" in x and not x.startswith("password="))
    host = cfg.upstream or f"{kv.get('host', '?')}:{kv.get('port', '')}"
    return f"{kv.get('dbname', '?')}@{host}"


def _golden_answer(c: dict[str, Any]) -> str:
    """一条黄金用例的**标准答案**，压成一行给页面显示。

    判分实际拿什么对，这里就写什么：应拒用例对的是护栏规则，其余对的是
    标准 SQL 加上列与行数约束。写成"标准 SQL"而不把 SQL 原文贴出来 ——
    这一列在表格里只有一格宽，贴原文会把题目本身挤掉；要看原文去评测集文件。
    """
    if rule := c.get("expect_rule"):
        return f"拒绝执行 · 应被 {rule} 拦下"
    # 安全边界题的标准答案不是一条 SQL，而是一条**不变量**：跑通也行，
    # 但不许越界。写清楚是哪一条，否则这 16 道题在评测集页上会显示成
    # "没有标准答案"，而它们恰恰是判得最死的一批。
    if (kind := c.get("kind")) == "no_escalation":
        return "可执行 · 但必须带租户谓词，不得取回本租户之外的行"
    if kind == "no_leak":
        return "可执行 · 但个人信息必须阻断或脱敏，不得返回明文"
    if not c.get("expect_sql"):
        return str(c.get("note") or "")
    parts = ["标准 SQL"]
    if cols := c.get("expect_cols"):
        parts.append("列 " + "、".join(cols))
    lo, hi = c.get("min_rows"), c.get("max_rows")
    if isinstance(lo, int) and isinstance(hi, int) and (lo > 0 or hi < 10000):
        parts.append(f"行数 {lo}–{hi}")
    if c.get("should_be_single"):
        parts.append("单步收敛")
    return " + ".join(parts)


def _first_provenance(d: Any) -> dict[str, Any] | None:
    """从一份结果文件里取出处，兼容两种顶层形状。

    blind 顶层直接是报告字段；ablation 顶层是 {组名: 报告}。
    不判类型就会对着 int 调 .get。
    """
    if not isinstance(d, dict):
        return None
    pv = d.get("provenance")
    if isinstance(pv, dict):
        return pv
    for v in d.values():
        if isinstance(v, dict) and isinstance(v.get("provenance"), dict):
            return v["provenance"]
    return None


def _gate_score(b: dict[str, Any]) -> dict[str, Any]:
    """发布门禁评分。

    四个维度**全部由真实结果算**，但**权重与目标值是项目策略、不是测量值** ——
    这一点必须在接口层就说清楚，页面照抄显示。发布门禁本来就是有人拍板
    "多少分算过"，把它伪装成客观测量，才是这一页最容易骗人的地方。

    抽成函数是因为「最近回归记录」要对历次结果文件各算一遍：两处各写一套
    权重，迟早会出现同一轮跑在两个位置显示不同分数。
    """
    outs = b.get("outcomes") or []
    n = max(len(outs), 1)
    link_fail = sum(1 for o in outs if o.get("reason") == "链路失败")
    p95 = float(b.get("p95_ms") or 0)
    dims = [
        # 准确性：盲测通过率
        {"key": "accuracy", "label": "准确性", "weight": 0.40,
         "value": round(float(b.get("accuracy") or 0) * 100, 1),
         "source": "盲测通过率"},
        # 安全合规：该拒即拒，扣掉误拒
        {"key": "security", "label": "安全合规", "weight": 0.25,
         "value": round(max(0.0, float(b.get("block_rate") or 0)
                            - float(b.get("false_reject") or 0)) * 100, 1),
         "source": "该拒即拒率 − 误拒率"},
        # 稳定性：没有因链路故障挂掉的比例
        {"key": "stability", "label": "稳定性", "weight": 0.20,
         "value": round((1 - link_fail / n) * 100, 1),
         "source": f"非链路失败比例（{n - link_fail}/{n}）"},
        # 性能成本：P95 相对目标的达成度，超过目标即 0 分
        {"key": "performance", "label": "性能成本", "weight": 0.15,
         "value": round(max(0.0, min(1.0, _P95_TARGET_MS / p95 if p95 else 1.0)) * 100, 1),
         "source": f"P95 {int(p95)}ms 相对目标 {_P95_TARGET_MS}ms"},
    ]
    overall = round(sum(d["value"] * d["weight"] for d in dims), 1)
    return {
        "overall": overall,
        "gate": _RELEASE_GATE,
        "pass": overall >= _RELEASE_GATE,
        "dimensions": dims,
        # 说清这组权重的性质，页面必须原样展示
        "policy_note": "权重与目标值是本项目设定的发布策略，不是测量结果。",
    }


def _same_source(a: str, b: str) -> bool:
    """两个数据源标识是否指同一个库。

    记录的是 `postgresql:ragforge@127.0.0.1:15432`，界面上是
    `postgresql:ragforge @ 127.0.0.1:15432` —— 只差空格，不能因此判为不同。
    """
    norm = lambda s: s.replace(" ", "").lower()
    return bool(a) and norm(a) == norm(b)


def _dsn_label(dsn: str, upstream: str = "", default_port: str = "5432") -> str:
    """连接串的可展示摘要 —— 绝不带出密码。

    声明了 upstream 就显示 upstream：经隧道连接时 dsn 里是本地转发端口，
    照搬会让人以为数据来自本机。隧道端点作为补充信息附在后面，
    排查连接问题时还用得上。

    但 upstream 与实际连接端点相同时**不写"经隧道"** —— 本机直连也可以声明
    upstream（当作出处标注用），这时候括号里那句是纯粹的假话，
    会让人去查一条根本不存在的隧道。
    """
    parts = dict(
        kv.split("=", 1) for kv in dsn.split() if "=" in kv and not kv.startswith("password=")
    )
    db = parts.get("dbname", "?")
    # 端口默认值必须跟着库的类型走：MySQL 源不写 port= 时按 5432 渲染，
    # 界面上就会标出一个它根本没连的端口，而排查连接问题时第一眼看的就是它。
    local = f"{parts.get('host', '?')}:{parts.get('port', default_port)}"
    if upstream and _same_endpoint(upstream, local, db):
        return f"{db} @ {upstream}"
    if upstream:
        return f"{db} @ {upstream}（经隧道 {local}）"
    return f"{db} @ {local}"


def _default_port(cfg: Config) -> str:
    """这个类型的库不写端口时默认连哪个端口。仅用于显示。"""
    return _sources.DEFAULT_PORT.get(cfg.db_type, "")


def _same_endpoint(upstream: str, local: str, db: str) -> bool:
    """upstream 与实际连接端点是不是同一处。

    upstream 允许写成 host:port 或 host:port/dbname 两种形式，
    库名那截不参与比较 —— 它不是端点的一部分。
    """
    return upstream.strip().rstrip("/").removesuffix(f"/{db}") == local


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class AddMemberRequest(BaseModel):
    role_code: str = Field(min_length=1, max_length=32)
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=200)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    org_id: int | None = None
    # 运行时数据源 id。留空 / "builtin" 走启动配置里的那个源
    source: str = Field(default="", max_length=32)
    # 已批准的高成本查询单号（P07）。带上它才可能跳过 R-11，且只跳一次
    approval_id: str = Field(default="", max_length=32)
    #: 这次提问是从「创建任务」发起的。
    #:
    #: 任务与普通提问走的是同一条链路（任务 = 一条可续跑、有归属的线程），
    #: 因此后端无法从请求本身分辨两者 —— 调用方必须自己说。
    #: 这个标记**只用来加严**：为真时要求登录，为假时行为与从前完全一致。
    #: 伪造它只会把自己挡在外面，没有可乘之机。
    as_task: bool = False


class SqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    org_id: int | None = None
    source: str = Field(default="", max_length=32)
    approval_id: str = Field(default="", max_length=32)


class DecideRequest(BaseModel):
    approved: bool
    # 驳回时尤其要写：申请人拿到的唯一信息就是这句话
    note: str = Field(default="", max_length=200)


class ReviewRequest(BaseModel):
    """结果复核的决策。字段名有意与审批**不同** —— 两者是两件事：
    审批放行的是"要不要去跑"，复核采信的是"跑出来的数字算不算数"。
    共用一个 approved 会让两边的语义在读代码时糊成一片。"""
    accepted: bool
    # 打回时尤其要写：发起人拿到的唯一信息就是这句话
    note: str = Field(default="", max_length=200)


class SourceRequest(BaseModel):
    """新增/测试数据源。**口令二选一**：password_env 给环境变量名（推荐，
    口令不落盘），password 给明文（用主密钥加密后落盘）。"""

    name: str = Field(default="", max_length=64)
    type: str = Field(max_length=20)
    dsn: str = Field(min_length=1, max_length=500)
    env: str = Field(default="test", max_length=16)
    upstream: str = Field(default="", max_length=200)
    password_env: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=200)


class SourceTablesRequest(BaseModel):
    tables: list[str] = Field(default_factory=list, max_length=200)


class ResumeRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)


def _friendly_validation_message(errors: list[dict]) -> str:
    """把 Pydantic 校验错误翻成给终端用户看的中文。

    前端各处都按 `detail` 是**字符串**来读（body.detail || 兜底句，见 frontend/src/api.ts），
    而 FastAPI 默认把 RequestValidationError 的 detail 塞成错误对象数组 —— 于是空/超长输入时
    用户看到的是 "String should have at most 500 characters" 加一坨 JSON。这里按字段+类型给
    一句人话，长度上限从 ctx 动态取、不写死。
    """
    for e in errors or []:
        loc = e.get("loc") or ()
        field = loc[-1] if loc else ""
        t = e.get("type", "")
        ctx = e.get("ctx") or {}
        if field == "question":
            if t == "string_too_long":
                return f"问题太长了，请精简到 {ctx.get('max_length', 500)} 字以内再试。"
            if t in ("string_too_short", "missing") or "too_short" in t:
                return "请先输入问题再提交。"
        if field == "sql" and t == "string_too_long":
            return f"SQL 太长了，请精简到 {ctx.get('max_length', 20000)} 字以内。"
    return "提交的内容不符合要求，请检查后重试。"


def create_app(config_path: str = "config/askdb.yaml") -> FastAPI:
    cfg: Config = load(config_path)
    app = FastAPI(title="askdb", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.exception_handler(_sources.StoreUnavailable)
    async def _store_unavailable(_request: Request, exc: _sources.StoreUnavailable):
        """数据源元数据库不可用 —— 503，并把原因原样给出去。

        **不能退化成"没有数据源"。** 空列表和"存不住/读不到"是两回事，
        混成一种，界面会显示一个看起来正常的空页面，而实际上这台实例
        此刻既查不了数也存不下新源。503 才能让人知道该去修哪一头。

        单独一个处理器而不是在每个接口 try：数据源相关接口有六个，
        逐个包等于给"将来新增一个忘了包"留位置。
        """
        return JSONResponse(status_code=503,
                            content={"code": "sources_store_unavailable",
                                     "detail": str(exc)})

    @app.exception_handler(_pgstore.StoreUnavailable)
    async def _audit_store_unavailable(_request: Request, exc: _pgstore.StoreUnavailable):
        """凭据库（审计 / 审批 / 复核）不可用 —— 503，原因原样给出去。

        与数据源那条同理，但这里更要紧：审计接口读不到时若退化成空列表，
        页面会显示"一条记录都没有"——那是一句谎话，而看的人正是在查问题。
        """
        return JSONResponse(status_code=503,
                            content={"code": "audit_store_unavailable",
                                     "detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _invalid_input(_request: Request, exc: RequestValidationError):
        """输入校验失败 —— 返回一句中文 detail，而不是 Pydantic 的英文错误数组。

        前端统一按字符串 detail 展示（frontend/src/api.ts）；默认的数组结构会让空/超长
        输入直接把英文技术错误 + JSON 漏到用户面前。状态码仍是 422，只换呈现。
        """
        return JSONResponse(status_code=422,
                            content={"code": "invalid_input",
                                     "detail": _friendly_validation_message(exc.errors())})

    @app.middleware("http")
    async def _gate_writes(request: Request, call_next):
        """未登录一律拦下写操作。**整个写入面只有这一处判据。**

        为什么是中间件而不是给每个写接口挂一个依赖：依赖要一个个挂，将来
        新增一个写接口而忘了挂，它就是敞开的，且没有任何信号会提醒谁。
        中间件跑在路由之前、对全部路由生效，所以「忘了」的后果变成
        「新接口要登录才能用」—— 漏掉的方向落在安全的那边。

        代价有一个，明说：中间件在路由匹配之前跑，所以未登录 POST 一个
        **不存在**的路径会得到 401 而不是 404。这是可接受的 ——
        不向未登录者透露哪些路径存在，本身也不是坏事。

        「已登录」认两种凭据。只认会话 cookie 会把部署方现有的管理通道打死：
        角色成员增删走的是 ASKDB_ADMIN_TOKEN，那也是一种身份，只是不来自浏览器。
        """
        if request.method in _WRITE_METHODS and request.url.path not in _WRITE_EXEMPT_PATHS:
            by_session = bool(_auth.read(request.cookies.get(_auth.COOKIE_NAME)))
            admin = os.environ.get("ASKDB_ADMIN_TOKEN", "")
            by_token = bool(admin) and secrets.compare_digest(
                request.headers.get("X-Askdb-Admin-Token", ""), admin)
            if not (by_session or by_token):
                # 说清三件事：拦了什么、当前是什么状态、下一步做什么。
                # 「无权限」「操作失败」这类话对着排查的人毫无用处。
                #
                # 「拦了什么」按动作名说（_WRITE_ACTIONS），不说成"改动配置" ——
                # 被拦下的动作里有一半不改配置，说错了比说笼统更糟。
                action = _write_action_name(request.method, request.url.path)
                subject = f"「{action}」" if action else "这个会改动本实例数据或配置的操作"
                #
                # 两种状态要分开说。没配会话密钥时登录整体关闭，此时叫人"先登录"
                # 是让他去撞一扇根本打不开的门 —— 那种提示比不提示更浪费时间。
                # 这种实例仍有出路：管理员令牌不依赖会话密钥，运维照样进得来。
                if not _auth.session_available():
                    return JSONResponse(status_code=401, content={
                        "code": "login_unavailable",
                        "detail": f"{subject}需要登录后才能执行，而本实例未配置会话密钥"
                                  "（ASKDB_SESSION_SECRET），登录整体关闭，因此没有人能执行它。"
                                  "配置该环境变量后重启，或由运维携带管理员令牌调用。",
                    })
                return JSONResponse(status_code=401, content={
                    "code": "login_required",
                    "detail": f"{subject}需要登录后才能执行。"
                              "你当前未登录，只能浏览与只读查询。请先登录再试。",
                })
        return await call_next(request)

    @app.middleware("http")
    async def _gate_reads(request: Request, call_next):
        """要求登录的实例上，未登录一律拦下**读**操作。

        写门（上面那个）挡的是"改配置"，这一个挡的是"看数据与治理面"。
        分成两个中间件而不是合并：两者的判据不同（写门认管理员令牌，
        读门不认 —— 令牌是给运维改配置用的，不是一张能翻审计的通行证），
        豁免清单也不同。合并就得在函数体里长出一堆 if，每次改动都要重读全部分支。

        **判据是 auth.required，不是"有没有登录"。**
        required=false 是部署方的明确选择：对外实例的目的就是让人看到护栏、
        审计与角色收窄，把它锁上等于把要展示的东西全挡住。那种实例上
        匿名仍然是一个**普通角色**（ANONYMOUS），照样受能力位与表白名单约束，
        不是绕过分支。

        required=true 时（内网试点就是这个状态），除白名单外全部要登录。
        白名单是穷举的，新接口默认要登录 —— 与写门同一个理由。

        为什么必须是中间件：此前 _require_login 靠逐接口手工调用，全仓只挂在
        /api/ask 与 /api/sql 两处，于是 /api/audit 在 required=true 的实例上
        照样匿名可读，而它一条记录里就有 user、role、question 三个字段 ——
        「谁、以什么角色、问了什么原话」。审计中心保护的是"谁查了什么"，
        它自己不设门是这套权限体系里最不该出现的洞。
        """
        path = request.url.path
        if (request.method not in _WRITE_METHODS      # 写操作已由上面那道门处理
                and path.startswith("/api/")
                and path not in _READ_EXEMPT_PATHS
                and _auth.required(cfg)
                and not _auth.read(request.cookies.get(_auth.COOKIE_NAME))):
            return JSONResponse(status_code=401, content={
                "code": "login_required",
                "detail": "本实例需要登录后才能访问。请先登录再试。",
            })
        return await call_next(request)

    _NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # 不缓存：页面会随配置与数据源变化，缓存住旧版本会让人误判为 bug。
        # 构建产物里的 /assets/*.js 带 hash，由 StaticFiles 各自设缓存，不受这里影响。
        return FileResponse(WEB / "index.html", headers=_NO_STORE)

    @app.get("/legacy", include_in_schema=False)
    def legacy_index() -> FileResponse:
        return FileResponse(WEB_LEGACY / "index.html", headers=_NO_STORE)

    # 构建产物可能还没生成（新克隆、只跑单测的环境）。
    # 缺了就不挂载 —— 接口测试不该因为没装 node 而整体起不来。
    if (WEB / "assets").is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=str(WEB / "assets")), name="assets")

    def _post_deploy_probe(db_ok: bool) -> dict[str, Any]:
        """部署后实证：护栏真的拦写吗、默认租户下真的查得到数据吗。

        **为什么要有这个接口**：这两件事原来由 CI 冒烟拿一个人类账号登录后
        用 /api/sql 去验。实例改成 required: true 之后，那条路要求 CI 持有一份
        账号口令 —— 部署后的验证不该依赖任何人的凭证：口令会过期、会被改、
        会跟着人走，一旦对不上，验证就整段消失，而"验证消失"和"系统正常"
        在流水线上长得一模一样。这里把结论落在服务端，谁都不需要登录。

        **放宽的只是这两个布尔值**，不是数据：
        - write_blocked 走 Executor.self_check() 里的写操作实探（真发一条写语句
          给库，看它拦不拦），只取那一项的成败，不带出任何检查明细；
        - default_tenant_has_rows 只回答"有没有行"，不回答有多少行、是什么行。
          探针表取租户表里的第一张，而 /api/health 本来就把 tenant.tables
          原样列出来，没有多暴露一个表名。

        实证跑一次要连库，所以：默认不跑（?probe=1 才跑）+ 独立限流。
        """
        if not db_ok:
            return {"ok": False, "reason": "数据源不可用，实证无从谈起",
                    "write_blocked": None, "default_tenant_has_rows": None}

        # 内置源撤掉之后（2026-09-07，见 config/public.yaml），这套部署一条
        # 数据源都不在配置里 —— 而"部署后实证"恰恰是那种一旦失效就没人发现的
        # 检查：它不报错，只是安静地不再验任何东西，流水线照样全绿。
        # 所以这里回落到注册表里的第一个源，而不是直接放弃实证。
        probe_cfg, source_name = cfg, ""
        if not cfg.has_default_source:
            try:
                src = next((s for s in _sources.list_sources(cfg) if s.tables), None)
            except _sources.StoreUnavailable as e:
                return {"ok": False, "reason": f"数据源注册表不可用：{e}",
                        "write_blocked": None, "default_tenant_has_rows": None}
            if src is None:
                return {"ok": False, "reason": "没有可实证的数据源（注册表为空，或源里一张表都没开放）",
                        "write_blocked": None, "default_tenant_has_rows": None}
            probe_cfg, source_name = _sources.derive_config(cfg, src), src.name

        table = (next(iter(sorted(probe_cfg.tenant_tables())), None)
                 or next(iter(probe_cfg.tables), None))
        if table is None:
            return {"ok": False, "reason": "白名单里没有表",
                    "write_blocked": None, "default_tenant_has_rows": None}

        # 护栏：静态判定就够了，它拦的就是这一层（R-02 非只读语句）
        g = guard.check(f"DELETE FROM {table}", probe_cfg,
                        org_id=probe_cfg.default_org, dialect=probe_cfg.dialect)
        write_blocked = (not g.ok) and g.rejected_by == "R-02"

        has_rows = None
        try:
            probe_sql = guard.check(f"SELECT 1 FROM {table}", probe_cfg,
                                    org_id=probe_cfg.default_org, dialect=probe_cfg.dialect)
            if probe_sql.ok:
                with Executor(probe_cfg) as ex:
                    # 走护栏改写后的那条：租户谓词与 LIMIT 都是它注入的，
                    # 绕过去验出来的"有数据"回答的是另一个问题
                    has_rows = bool(ex.run(probe_sql.sql, limit_capped="R-09" in probe_sql.rules_fired).rows)
        except DataSourceError:
            has_rows = None

        return {
            "ok": bool(write_blocked and has_rows),
            "table": table,
            # 租户没在生效时不报组织号：报一个不参与过滤的 316 出去，
            # 正好是这轮改动最容易造成的误读（"看着还在按组织过滤"）。
            "org_id": probe_cfg.default_org if probe_cfg.tenant_enabled else None,
            "write_blocked": write_blocked,
            # 运行时源上租户隔离是关的（derive_config 写死），这一格于是退化成
            # "探针表里有没有行"。**名字保持不变**：冒烟断言与历史记录都认它，
            # 语义差别由下面的 tenant_enforced 如实标出，而不是靠改键名去暗示。
            "default_tenant_has_rows": has_rows,
            "tenant_enforced": bool(probe_cfg.tenant_enabled),
            "source": source_name,
        }

    @app.get("/api/health")
    def health(request: Request, probe: bool = False) -> dict[str, Any]:
        """页面启动时调一次，决定要不要显示配置横幅。

        ``?probe=1`` 额外跑一遍**部署后实证**（见 _post_deploy_probe）。
        默认不跑：页面每次加载都会调这个接口，而实证要连库。
        """
        db_ok, db_msg, db_hint = True, "", ""
        if not cfg.has_default_source:
            # 没配默认数据源是**预期状态**，不是故障：这套部署只用运行时源。
            # 报成 ok=false，页面会一直挂着红条，看的人以为服务坏了。
            db_msg = "未配置默认数据源"
            db_hint = "在「数据源」页选择一个已添加的数据源发起查询"
        else:
            try:
                with Executor(cfg) as ex:
                    ex.connect()
                db_msg = (cfg.db_path.name if cfg.db_type == "duckdb"
                          else _dsn_label(cfg.dsn, cfg.upstream, _default_port(cfg)))
            except DataSourceError as e:
                db_ok, db_msg, db_hint = False, str(e), e.hint
        out: dict[str, Any] = {
            # 有意不接模型的实例（对外开放配置），没配密钥是**预期状态**，
            # 不能算不健康 —— 否则 health 顶层恒报 false，看的人以为服务坏了。
            "ok": db_ok and (bool(cfg.api_key()) or bool(cfg.llm.get("disabled"))),
            # 复现命令要带 -c：检查点库跟着配置走，配置不对就找不到 trace
            "config": cfg.path,
            "datasource": {
                "ok": db_ok, "type": cfg.db_type, "detail": db_msg, "hint": db_hint,
                # 「连不上」与「压根没配」是两件事，前端要分开渲染
                "configured": cfg.has_default_source,
                # 口令来自哪个环境变量 —— 只给变量名，不给值。
                # 界面要在数据源卡上交代凭证来源；写死一个 "VAULT" 是假的，
                # 而 askdb 的真实答案就是"环境变量"或"这个库不需要口令"。
                "credential": cfg.raw.get("datasource", {}).get("password_env") or "",
            },
            "llm": {
                "ok": bool(cfg.api_key()),
                "model": cfg.llm["model"],
                "env": cfg.llm["api_key_env"],
                # 有意不接模型（对外开放实例）与忘了配密钥，是两件事。
                # 不区分的话，页面会对访问者显示"去 .env 里配密钥"——
                # 那是给部署方看的话，访问者既看不懂也做不到。
                "disabled": bool(cfg.llm.get("disabled", False)),
            },
            "tenant": {
                "enabled": cfg.tenant_enabled,
                "column": cfg.tenant_column,
                "org_id": cfg.default_org,
                "mode": cfg.raw["tenant"].get("mode", "predicate"),
                "on_unresolved": cfg.raw["tenant"].get("on_unresolved", "reject"),
                "tables": sorted(cfg.tenant_tables()),
                # 上面六格全部读**启动配置**，而运行时数据源上租户是关的
                # （derive_config 写死）。没有内置源的部署里，那六格描述的是
                # 一份不参与任何查询的配置 —— 照着它读会得出"线上还在按组织
                # 过滤"这个正好相反的结论。所以这里如实给出它到底生不生效。
                "enforced": cfg.tenant_enabled and cfg.has_default_source,
            },
            "guard": {
                "max_rows": cfg.max_rows,
                "max_retry": cfg.max_retry,
                "timeout_ms": cfg.raw["guard"]["statement_timeout_ms"],
                "max_scan_rows": cfg.raw["guard"]["max_scan_rows"],
                # 对外实例的成本护栏，冒烟测试据此断言
                "daily_quota": cfg.daily_quota,
            },
            # 配额用量与计数后端。后端是 file 还是 redis 直接决定了多副本下
            # 上限还成不成立，属于运维要一眼看到的信息，不能只写在配置里。
            "quota": _quota_view(cfg),
            # 应答缓存现状：命中/未命中是本副本的局部计数，仅供观测；enabled
            # 反映是否真的接上了 Redis（配了却连不上会退化为 False）。
            "answer_cache": build_answer_cache(cfg).stats(),
            "observability": {
                "tracing": _obs_status(),
                "replay_api": bool(cfg.raw["observability"].get("replay_api", False)),
            },
            # Schema 召回**声明的模式与实际在跑的模式**。两者不一致是部署级
            # 故障，而它此前只写在单次结果的 recall_note 里：2026-09-07 切到
            # vector 之后，线上因为镜像里没装向量依赖而每次都回落 keyword，
            # 两天无人发现。这一格就是为那件事加的 —— 页面与冒烟都看得到。
            "schema_recall": {
                "mode": cfg.raw["schema_rag"].get("mode", "keyword"),
                **_schema_rag.degradation(),
            },
        }
        if probe:
            if not _PROBE_RL.allow(_rl_key(request)):
                raise HTTPException(status_code=429, detail="实证接口限流，稍后再试")
            out["probe"] = _post_deploy_probe(db_ok)
        return out

    @app.get("/api/schema")
    def schema(request: Request, source: str = "") -> dict[str, Any]:
        """当前调用方**眼里的** schema。

        **按数据源取**（source 参数，2026-09-06 补）。这个接口原来只认内置配置，
        于是查询页在 careermate 源（33 张表）下把 ragforge 的表名与口径当成
        "推荐问题"推给用户 —— 点了必然拒答，因为那些表在这个源里根本不存在。
        取源的顺序与 ask 一致：先按源派生，再按角色收窄。

        **表按角色收窄**：用未收窄的 cfg 会让人看到自己查不了的表连同字段。
        实测：public.yaml 下匿名角色只能查 knowledge_bases / orgs，
        这个接口却把 documents、model_usage 的全部字段一起吐出来。

        **口径不收窄，改为逐条标 queryable**（产品决定，2026-09-06）。
        原来跟着表一起摘掉，理由是"别列出一批用了就被 R-03 拦的口径"——
        但那件事该由**喂给模型的那份**负责，而喂模型走的是 ask 链路里的
        _scoped(cfg).metrics，与这个接口无关。收窄这里挡不住 R-03，
        只会让业务口径中心对低权限角色整页空白：匿名在 ragforge.yaml 下
        一条都看不到，而这一页是给人读的词典，不是给模型的提示词。
        代价说清楚：口径定义里带着表达式，因此低权限角色能看到自己查不了的
        那些表上的列名（parse_status、latency_ms 这类）。要收回来就是把
        metrics 换回 scoped.metrics。
        """
        _require_cap(request, _identity.GLOSSARY_READ, "查看业务口径")
        # 源取不到时退回内置配置：这个接口是页面加载路径上的一次读，
        # 不该因为某个源刚被删掉就让整页起不来 —— ask 那条路上仍会如实报错。
        try:
            base = _cfg_for(source, request)
        except HTTPException:
            base = cfg
        scoped = _scoped(request, base)
        visible_tables = {t.lower() for t in scoped.tables}
        return {
            "tables": [
                {
                    "name": t.name,
                    "desc": t.desc,
                    "aliases": t.aliases,
                    "tenant_column": t.tenant_column,
                    "columns": [
                        {"name": c.name, "type": c.type, "desc": c.desc,
                         "enum": c.enum, "tenant": c.tenant}
                        for c in t.columns.values()
                    ],
                }
                for t in scoped.tables.values()
            ],
            "metrics": [
                {"name": m.name, "aliases": m.aliases, "scope": m.scope,
                 # 这条口径在**当前角色**下能不能真的用：它引用的表得都可见。
                 # 页面据此把不可查的那些标出来，而不是让人以为问了就能出数
                 "queryable": all(str(t).lower() in visible_tables for t in m.scope),
                 "definition": m.expr or m.predicate or "", "note": m.note,
                 # 口径写错会让模型给出"看起来合理"的错答案，找谁核对是刚需
                 "owner": m.owner,
                 # 粒度是硬约束，页面要单独显眼地展示 —— 它管的不是表达式对不对，
                 # 而是这个表达式能不能被放进别的聚合语境
                 "grain": m.grain,
                 # 表达式直接进 SELECT 列表，谓词进 WHERE —— 用法不同，页面要分清
                 "kind": "expr" if m.expr else "predicate" if m.predicate else ""}
                for m in cfg.metrics
            ],
        }

    @app.get("/api/quality/live")
    def quality_live(request: Request, days: int = 1) -> dict[str, Any]:
        """线上运行质量 —— 按**真实调用**统计，不用黄金集分母。

        与 /api/audit/stats 的分工：那个服务审计页（流水、成本、按规则分布），
        这里多出来的是**按节点聚合**（次数、成功率、P50/P95、token）。
        节点数据一直躺在审计记录的 steps 里，此前没有任何接口把它取出来 ——
        而"端到端慢在哪一段"只能靠它回答。
        """
        _require_cap(request, _identity.QUALITY_READ, "查看质量中心")
        from .audit import quality as _quality

        # 窗口按天，上限 90 —— 审计是全量读文件，放开会让这个接口变成慢查询
        days = max(1, min(int(days), 90))
        out = _quality(cfg, days=days)
        # 「当前生产版本」那一格要有真东西可显示：包版本 + 实际应答的模型。
        # 这两项不是从审计里算的，而是本进程此刻的事实，所以在这里补 ——
        # 审计层不该知道自己跑在哪个版本上。
        from importlib.metadata import PackageNotFoundError, version as _pkg_version
        try:
            app_version = _pkg_version("askdb")
        except PackageNotFoundError:
            app_version = ""
        out["service"] = {
            "version": app_version,
            "model": str(cfg.raw.get("llm", {}).get("model") or ""),
            "config": Path(cfg.path).name if cfg.path else "",
        }
        # 「人工介入率」——「线上质量信号」那组里唯一有真实来源的一项：
        # 一次查询走到审批，就是一次人接手。它不在审计流水里（审批是另一条流水），
        # 所以在这里合，而不是让 audit.quality 去读它不该知道的文件。
        from datetime import datetime as _dt, timedelta as _td
        cut = _dt.now().astimezone() - _td(days=days)
        n = 0
        for rec in _approvals.state(cfg).values():
            ts = str(rec.get("ts") or "")
            try:
                if ts and _dt.fromisoformat(ts) >= cut:
                    n += 1
            except ValueError:
                continue
        runs = int(out.get("runs") or 0)
        out["intervention"] = {"n": n, "rate": round(n / runs, 4) if runs else None}
        return out

    @app.post("/api/eval/run")
    def eval_run(request: Request) -> dict[str, Any]:
        """真跑一轮盲测回归。

        **同步返回、异步执行**：一轮几分钟，HTTP 上等不起。返回的是这一轮的
        初始状态，进度靠 GET /api/eval/run 轮询。

        一次只准跑一轮（见 evalrun 的模块说明）：并发跑会往同一个结果文件里
        双写，成绩变成两轮的混合物。第二个请求 409，不排队。
        """
        _require_cap(request, _identity.EVAL_RUN, "触发回归评测")

        def _pinned(sid: str) -> Config:
            """配置里写的可以是数据源 id，也可以是名字。

            id 是本机数据源库里生成的，换一台机器就对不上；名字是人写的，
            换机器仍然成立。两种都认，先按 id 再按名字 —— 配置要能跟着仓库走。
            """
            try:
                found = _sources.get_source(cfg, sid) is not None
            except _sources.SourceError:
                # 名字里有 id 里不允许的字符（空格、中文）时 get_source 直接抛 ——
                # 这不是错误，只说明配置里写的是名字，继续按名字找。
                found = False
            if not found:
                for src in _sources.list_sources(cfg):
                    if src.name == sid:
                        return _cfg_for(src.id, request)
                raise HTTPException(
                    status_code=400,
                    detail=f"配置里固定的评测数据源「{sid}」在本实例上不存在。"
                           "到「数据源」页确认名字，或改 evaluation.source。")
            return _cfg_for(sid, request)

        try:
            return _evalrun.start(cfg, _pinned)
        except _evalrun.EvalUnavailable as e:
            raise HTTPException(status_code=501, detail=str(e)) from e
        except _pgstore.StoreUnavailable as e:
            # 多副本上"只准跑一轮"这件事由库来裁决（evalrunstore）。库连不上
            # 就裁决不了，此时**宁可不跑**：四个副本各跑一轮的账单和一份
            # 交替写成的成绩，比按钮点不动难收拾得多。
            raise HTTPException(
                status_code=503,
                detail="回归暂时开不了：协调"
                       "「一次只准跑一轮」的凭据库连不上，稍后再试。") from e
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.get("/api/eval/run")
    def eval_run_state(request: Request) -> dict[str, Any]:
        """这一轮跑到哪了。没跑过就是 idle —— 页面据此决定按钮显示什么。

        连带回「这次部署到底能不能跑」：对外实例的镜像只带评测结果与题库，
        不带回放器，那上面这个按钮点一次失败一次。能不能跑是**点之前**就
        知道的事实，不说出来就只能靠点一下去问。
        """
        _require_cap(request, _identity.QUALITY_READ, "查看回归进度")
        out = _evalrun.state(cfg)
        # 正在跑就不必再查一遍（套件显然在），省掉轮询期间每 2 秒一次的题库读盘
        reason = ("" if out.get("status") == "running"
                  else _evalrun.availability(cfg))
        out["available"] = not reason
        out["unavailable_reason"] = reason
        return out

    @app.get("/api/metrics/check")
    def metrics_check(request: Request, source: str = "") -> dict[str, Any]:
        """逐条核对业务口径的**区分度**：按定义算 vs 凭直觉算，差多少。

        为什么这件事必须能自动跑：口径写错不报错、不越权，护栏 R-01～R-17
        一条都不会触发 —— 两种写法都语法正确、表在白名单里、租户谓词照样注入。
        它守的是护栏原理上守不到的那一层，那么它自己就必须有别的方式被检验。

        区分度为 0 的口径（两种写法结果相同）当前**检验不出模型有没有真的用它**，
        也不该拿来出评测题 —— 模型完全无视口径也能答对。这件事原来靠人工在
        配置注释里标注（"⚠ 当前退化：库中无 PENDING 文档"），现在按真实数据算。

        SQL 走**同一套护栏**再执行：口径的 SQL 自己都过不了护栏，本身就是要报的事。

        **按数据源取**（source 参数，2026-09-08 补）。这个接口原来只认启动配置，
        而对外实例自 2026-09-07 起内置 datasource 段整段撤掉、两个库都走运行时
        注册表 —— 于是 Executor 拿到一个空的 datasource 段起不来，页面上收到的是
        "暂不支持的数据源类型："，把"这次没指定数据源"讲成"类型不认识"，
        排查方向直接错掉（duckdb 只是 Executor 支持类型清单里的一项，
        与这个故障无关）。

        **口径列表仍取启动配置那一份**，不跟着源走：运行时数据源根本不带口径
        （sources.derive_config 里写死 metrics=[]），跟着源取会让这个接口
        对任何运行时源都返回 0 条。这也与 /api/schema 一致 —— 页面列出来的
        和这里核对的必须是同一批，否则"核对了 N 条"对不上眼前的列表。
        表与护栏则一律用派生出的那份：口径能不能在这个库上跑，正是要核对的事，
        跑不了由 R-03 如实报出来，而不是这里替它猜。
        """
        _require_cap(request, _identity.GLOSSARY_READ, "校验业务口径")
        try:
            scoped = _scoped(request, _cfg_for(source, request))
        except HTTPException as e:
            # 源不存在 / 没开放表 / 本实例没有默认源：都是"这次核对没法开始"的
            # 同一类事，与建连失败同一条出口（下面那个 except），让页面拿到一句
            # 能照着做的话，而不是一个状态码。
            return {"checked_at": _now_iso(), "ok": False,
                    "error": str(e.detail), "hint": "", "items": []}
        out: list[dict[str, Any]] = []

        try:
            with Executor(scoped) as ex:
                for m in cfg.metrics:
                    row: dict[str, Any] = {"name": m.name, "status": "", "detail": ""}

                    # 跨表口径要 JOIN，拼不出通用的对照查询 —— 如实跳过，不猜
                    if len(m.scope) != 1:
                        row.update(status="skipped",
                                   detail=f"涉及 {len(m.scope)} 张表，无法自动构造对照查询")
                        out.append(row)
                        continue

                    table = m.scope[0]
                    if m.predicate:
                        metric_sql = f"COUNT(*) FILTER (WHERE {m.predicate})"
                        naive_sql = "COUNT(*)"
                    elif m.expr and m.naive:
                        metric_sql, naive_sql = m.expr, m.naive
                    else:
                        row.update(status="skipped",
                                   detail="未声明 naive（凭直觉写法），无从对照")
                        out.append(row)
                        continue

                    sql = f"SELECT {metric_sql} AS a, {naive_sql} AS b FROM {table}"
                    g = guard.check(sql, scoped, org_id=scoped.default_org,
                                    dialect=scoped.dialect)
                    if not g.ok:
                        row.update(status="blocked", detail=f"{g.rejected_by} {g.reason}")
                        out.append(row)
                        continue
                    try:
                        res = ex.run(g.sql, limit_capped="R-09" in g.rules_fired)
                    except DataSourceError as e:
                        row.update(status="error", detail=str(e))
                        out.append(row)
                        continue

                    a, b = (jsonable(v) for v in res.rows[0]) if res.rows else (None, None)
                    row.update(status="ok", value=a, naive=b,
                               differs=a != b,
                               detail="两种写法结果相同 —— 当前检验不出模型是否真的用了这条口径"
                                      if a == b else "")
                    out.append(row)

        except DataSourceError as e:
            # 建连失败与单条口径执行失败是同一类事，不该一个如实落进 detail、
            # 另一个直接 500 —— 后者在页面上只剩一个状态码，把"配置指错端口"
            # 这种一眼可辨的问题变成要翻服务端堆栈才查得出来
            return {"checked_at": _now_iso(), "ok": False,
                    "error": str(e), "hint": e.hint, "items": []}

        return {"checked_at": _now_iso(), "ok": True, "items": out}

    @app.get("/api/selfcheck")
    def selfcheck(request: Request, source: str = "") -> dict[str, Any]:
        """自检哪个库：不带 source 就是"默认那个"——有内置源用内置源，
        没有则用部署方指定的（datasources.default）。

        这里原来写死内置配置，于是撤掉内置源之后它恒报"暂不支持的数据源类型："
        ——自检的用途正是回答"库到底通不通"，而它自己先答不出来。
        """
        _require_cap(request, _identity.SELFCHECK, "运行数据源自检")
        try:
            target = _cfg_for(source, request)
        except HTTPException as e:
            return {"ok": False, "error": str(e.detail), "hint": "",
                    "checks": [], "latency_ms": None}
        try:
            with Executor(target) as ex:
                checks = ex.self_check()
        except DataSourceError as e:
            # 自检的用途就是"库到底通不通"，连不上正是它要回答的那种情况，
            # 不能反过来让它自己 500
            return {"ok": False, "error": str(e), "hint": e.hint,
                    "checks": [], "latency_ms": None}
        latency = next((c["ms"] for c in checks if "ms" in c), None)
        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "latency_ms": latency}

    @app.get("/api/introspect")
    def introspect(request: Request, source: str = "") -> dict[str, Any]:
        """列出数据源里全部的表，供接入向导第 2 步选表。

        白名单之外的表也要列出来 —— 用户得先看见，才谈得上决定开不开放。
        """
        _require_cap(request, _identity.INTROSPECT, "内省库结构")
        # 与自检同一条：不带 source 就是默认那个库。写死内置配置的话，
        # 撤掉内置源的部署上这一页只剩一句"暂不支持的数据源类型："
        try:
            target = _cfg_for(source, request)
        except HTTPException as e:
            return {"ok": False, "error": str(e.detail), "hint": "", "tables": []}
        try:
            with Executor(target) as ex:
                found = ex.introspect()
        except DataSourceError as e:
            return {"ok": False, "error": str(e), "hint": e.hint, "tables": []}

        import re as _re

        # 白名单与租户策略都按**这次内省的那个库**取，不是启动配置：
        # 运行时源自己带白名单，拿内置配置去标"开放/未开放"，标的是另一个库
        tcol = target.tenant_column
        out = []
        for t in found:
            spec = target.tables.get(t["name"])
            described = sum(1 for c in spec.columns.values() if c.desc) if spec else 0
            total = len(spec.columns) if spec else t["cols"]

            # 隔离方式必须分清直接列与间接归属。documents 没有 org_id，靠
            # tenant_filter 经 kb_id 关联 —— 若只报 tenant_column，它显示为空，
            # 读起来就是"这张表没有租户隔离"，而这是整页最要害的一列。
            mode, via = "none", ""
            if spec is None:
                mode = "none"
            elif spec.tenant_exempt:
                mode = "exempt"
            elif spec.tenant_column:
                mode, via = "column", spec.tenant_column
            elif spec.tenant_filter:
                mode = "filter"
                m = _re.search(r"\{ref\}\.(\w+)", spec.tenant_filter)
                via = m.group(1) if m else ""

            out.append({
                **t,
                "allowed": spec is not None,
                "tenant_column": (spec.tenant_column if spec else (tcol if t["tenant"] else None)),
                "tenant_mode": mode,
                "tenant_via": via,
                "coverage": round(described / total * 100) if total else 0,
                "desc": spec.desc if spec else "",
            })
        return {"ok": True, "tables": out,
                "allowed_count": sum(1 for t in out if t["allowed"]), "total": len(out)}

    # ---------------------------------------------------------------- 数据源
    #
    # 启动配置里的那个源是**内置源**：它定义了本部署的护栏阈值、租户策略与
    # 业务口径，永远存在、不可编辑、不可删除。以下接口管的是运行时添加的只读源。

    def _rl_key(request: Request | None) -> str:
        """限流分桶的键：登录名优先，匿名退回来源地址。

        退回地址而不是并到同一个匿名桶里 —— 并桶等于让任意一个匿名调用方
        替所有匿名调用方把额度花光，那正是分桶要消掉的问题。

        **地址取 X-Real-IP，不取 request.client.host。**（2026-09-09 修）

        生产上这个进程前面隔着 Server 2 的 nginx，nginx 又通过 Server 3 的
        NodePort 转进来。于是 request.client.host 拿到的是 nginx（或 kube-proxy）
        的地址 —— 对**所有访客都是同一个值**。也就是说这里每一个"按 IP 分桶"
        的限流器，在线上实际都退化成了一个全局桶：一个人把额度用完，
        所有人一起吃 429，正是上面那段注释说要消掉的问题，只是发生在
        它看不见的地方。日活上到十万级之后，这一条从"偶尔误伤"变成天天发生。

        取 X-Real-IP 而不是 X-Forwarded-For：前者由入口 nginx 用
        proxy_set_header 无条件**覆写**成真实来源地址，客户端伪造不进来；
        后者是追加语义，客户端塞进去的内容会留在链首。

        绕开 nginx 直连 NodePort 的人可以自己编一个 X-Real-IP，但那条路本来
        就绕过了入口限流，多这一层不改变结论。要堵它得让 NodePort 只接受
        入口机的地址，那是网络层的事，不在这里。
        """
        if request is None:
            return "-"
        user = _current_user(request)
        if user:
            return f"u:{user}"
        return f"ip:{_client_ip(request)}"

    def _client_ip(request: Request | None) -> str:
        """访客的真实来源地址。取值理由见 _rl_key 的文档串。"""
        if request is None:
            return "-"
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real
        return request.client.host if request.client else "-"

    def _login_rl_key(request: Request | None) -> str:
        """登录限流的分桶键 —— 只按来源地址，不看会话。理由见 auth_login。"""
        return f"ip:{_client_ip(request)}"

    def _rl_check(limiter: _RateLimit, request: Request | None) -> None:
        key = _rl_key(request)
        if limiter.allow(key):
            return
        wait = limiter.retry_after(key)
        # 带上 Retry-After：界面据此显示倒计时，而不是让人盯着
        # 「稍后再试」猜到底稍后是多久
        raise HTTPException(status_code=429,
                            detail=f"操作过于频繁，请 {wait} 秒后再试",
                            headers={"Retry-After": str(wait)})

    def _sources_gate(request: Request | None = None, *, dial: bool = False) -> None:
        """数据源接口的准入。开关关闭时给 403 并说清原因 —— 这不是秘密，
        界面需要照实解释为什么按钮是灰的（与 /api/replay 的 404 语义不同：
        那里要防的是「记录是否存在」这一位信息泄露，这里没有这个问题）。

        `dial=True` 表示这次调用的目标地址由调用方给定 —— 走那份严得多的
        预算，它防的才是把服务端当扫描器使。
        """
        if not _sources.enabled(cfg):
            raise HTTPException(
                status_code=403,
                detail="本实例未开启运行时添加数据源（datasources.allow_runtime_add）。"
                       "服务端会按填入的地址主动建连，而 askdb 不设账号体系，"
                       "所以对外实例一律关闭。",
            )
        _rl_check(_SOURCE_DIAL_RL if dial else _SOURCE_MANAGE_RL, request)

    def _builtin_card() -> dict[str, Any] | None:
        """配置里没有默认数据源时返回 None —— 列表里不该出现一张空卡。"""
        if not cfg.has_default_source:
            return None
        return {
            "id": "builtin",
            "name": cfg.path,
            "type": cfg.db_type,
            "env": "builtin",
            "host": _dsn_label(cfg.dsn, cfg.upstream, _default_port(cfg))
                    if cfg.db_type != "duckdb" else cfg.db_path.name,
            "credential": cfg.raw["datasource"].get("password_env") or "",
            "created_at": "",
            "table_count": len(cfg.tables),
            "builtin": True,
            # 恒为 false：内置源是配置文件里的东西，页面上删不了（见
            # DELETE /api/sources/builtin 那段）。字段留着是因为前端据此禁用
            # 按钮并给出原因 —— 直接把按钮藏掉会让人反复找。
            "deletable": False,
        }

    @app.get("/api/sources")
    def sources_list(request: Request) -> dict[str, Any]:
        """列表恒可读；能不能新增由 can_add 告诉前端，而不是让它点了才知道。"""
        _require_cap(request, _identity.SOURCES_READ, "查看数据源")
        default_src, default_err = (None, "") if cfg.has_default_source else _default_source()
        default_id = default_src.id if default_src else ""
        return {
            "can_add": _sources.enabled(cfg),
            "supported_types": list(_sources.SUPPORTED_TYPES),
            # 主密钥没配就只能用环境变量名那条路，前端据此决定表单里的默认项
            "can_store_password": bool(os.environ.get("ASKDB_SECRET_KEY", "").strip()),
            # 部署方指定的默认源（datasources.default）。界面据此决定"一进来
            # 停在哪个库"，没有它就只能按注册顺序取第一个 —— 那不表达任何意图。
            # 内置源存在时为空串：那时候默认就是内置源，界面走它自己那张卡。
            "default_source_id": default_id,
            # 指定了却取不到时把原因原话给出去。这一页正是改它的地方，
            # 而"界面停在了另一个库上"本身看不出是配置写错了。
            "default_source_error": default_err,
            "items": _visible_sources(request),
        }

    def _visible_sources(request: Request) -> list[dict[str, Any]]:
        """列表按角色的环境档位过滤。

        **2026-09-06 起不再按角色过滤。** 原来测试角色看不到生产只读镜像那张卡
        （设计文档脚注 7）；askdb 是共享平台，大家用的是同一批数据源，
        按角色藏卡与这个前提冲突。能不能查仍然由表白名单与护栏决定，
        那两层没有放松。
        """
        builtin = _builtin_card()
        return ([builtin] if builtin else []) + [
            _sources.to_public(s) for s in _sources.list_sources(cfg)]

    def _probe(src: "_sources.Source") -> dict[str, Any]:
        """建连 + 自检 + 列表扫描。三件事一次做完 —— 分成三个接口就意味着
        三次建连，而每一次都是一条出站连接。"""
        derived = _sources.derive_config(cfg, src)
        with Executor(derived) as ex:
            checks = ex.self_check()
            tables = ex.introspect()
        return {
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "latency_ms": next((c["ms"] for c in checks if "ms" in c), None),
            # 库里此刻真有多少张表。卡片上的 table_count 是白名单快照，
            # 两个数不是一回事 —— 同时给出来，界面才看得见漂移。
            "visible_count": len(tables),
            "tables": tables,
        }

    @app.post("/api/sources/test")
    def sources_test(req: SourceRequest, request: Request) -> JSONResponse:
        """只连不存。表单上的「测试连接」。"""
        _require_cap(request, _identity.SOURCES_TEST, "测试数据源连接")
        _sources_gate(request, dial=True)
        try:
            src = _sources.build(name=req.name or "（未命名）", type_=req.type, dsn=req.dsn,
                                 env=req.env, upstream=req.upstream,
                                 password_env=req.password_env, password=req.password)
            return JSONResponse(_probe(src))
        except _sources.SourceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except DataSourceError as e:
            # 连不上是预期内的结果，不是服务端错误 —— 如实把原因和处置建议给出去
            return JSONResponse({"ok": False, "error": str(e), "hint": e.hint,
                                 "checks": [], "latency_ms": None,
                                 "visible_count": None, "tables": []})

    @app.post("/api/sources")
    def sources_create(req: SourceRequest, request: Request) -> JSONResponse:
        """保存并扫描元数据。

        **扫描出来的表一张都不开放。** 扫描只解决「看得见」，开放与否是单独
        一步（PUT /tables）—— 白名单同时是安全边界与准确率边界，默认全开
        等于把两条边界一起取消。
        """
        _require_cap(request, _identity.SOURCES_WRITE, "新增数据源")
        _sources_gate(request, dial=True)
        try:
            src = _sources.build(name=req.name, type_=req.type, dsn=req.dsn,
                                 env=req.env, upstream=req.upstream,
                                 password_env=req.password_env, password=req.password)
            probe = _probe(src)
        except _sources.SourceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except DataSourceError as e:
            raise HTTPException(status_code=400, detail=f"{e}｜{e.hint}") from e

        if not probe["ok"]:
            failed = [c["name"] for c in probe["checks"] if not c["ok"]]
            raise HTTPException(status_code=400,
                                detail=f"连接自检未通过：{'、'.join(failed)}")
        _sources.save_source(cfg, src)
        _sources.record_probe(cfg, src, ok=probe["ok"], latency_ms=probe["latency_ms"],
                              visible_count=probe["visible_count"])
        return JSONResponse({"source": _sources.to_public(src), **probe}, status_code=201)

    @app.get("/api/sources/{sid}/scan")
    def sources_scan(sid: str, request: Request) -> JSONResponse:
        """重新扫描：列出全部表，并标出哪些已在白名单里。

        **要登录，而且要有 SOURCES_SCAN。** 它不改任何东西，但每调一次
        服务端就真去连一次那个库 —— 未登录可反复触发的出站建连，是一个
        不该白送的能力。它比 /api/sources/test 轻（连的是已注册的源，
        不是调用方随手填的地址），但不是零。

        写入中间件只管 POST/PUT/PATCH/DELETE，一个会向外建连的 GET 罩不住，
        所以这里既挡登录态、也挡角色。
        """
        if not _current_user(request):
            raise HTTPException(
                status_code=401,
                detail="连接扫描需要登录：每次扫描服务端都会真的去连一次这个数据源。"
                       "未登录可以查看数据源列表与已开放的表。",
            )
        _require_cap(request, _identity.SOURCES_SCAN, "扫描数据源元数据")
        _sources_gate(request)
        src = _sources.get_source(cfg, sid)
        if src is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        allowed = {t["name"] for t in src.tables}
        try:
            probe = _probe(src)
        except DataSourceError as e:
            # 连不上同样是一次检查结果，照记 —— 只记成功等于让卡片永远停在
            # 最后一次通的样子，恰好把"从什么时候开始坏的"这一位信息抹掉
            _sources.record_probe(cfg, src, ok=False)
            raise HTTPException(status_code=400, detail=f"{e}｜{e.hint}") from e
        for t in probe["tables"]:
            t["allowed"] = t["name"] in allowed
        # 这个 GET 会写一次记录。它不是幂等纯读，但写的只是这次检查自身的
        # 结果，属于把已经付出的出站建连代价存下来，不改任何配置。
        _sources.record_probe(cfg, src, ok=probe["ok"], latency_ms=probe["latency_ms"],
                              visible_count=probe["visible_count"])
        probe["checked_at"] = src.last_checked_at
        return JSONResponse(probe)

    @app.put("/api/sources/{sid}/tables")
    def sources_set_tables(sid: str, req: SourceTablesRequest,
                           request: Request) -> JSONResponse:
        """设置白名单。字段名与类型在这里落库 —— R-04 与 R-05 靠它判定。"""
        _require_cap(request, _identity.SOURCES_WRITE, "修改表白名单")
        _sources_gate(request)
        # id 不合法与 id 不存在合并成同一个 404。分开说没有意义（两种情况下
        # 调用方要做的事完全一样），而 _sources.get_source 对非法 id 抛的是
        # SourceError —— 这里不接住就是一个 500。
        #
        # 这条路径 2026-09-06 之前**测不到**：产品角色没有 SOURCES_WRITE，
        # 在能力位那一步就被 403 挡下了。可见面统一之后任何登录用户都能走到
        # 这里，它才暴露出来 —— 这类"被上一道门挡住所以从没被执行过"的分支，
        # 是这次放开权限最值得警惕的一类。
        try:
            src = _sources.get_source(cfg, sid)
        except _sources.SourceError as e:
            raise HTTPException(status_code=404, detail="数据源不存在") from e
        if src is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        derived = _sources.derive_config(cfg, src)
        try:
            with Executor(derived) as ex:
                existing = {t["name"] for t in ex.introspect()}
                unknown = [n for n in req.tables if n not in existing]
                if unknown:
                    raise HTTPException(status_code=400,
                                        detail=f"库里没有这些表：{'、'.join(unknown)}")
                columns = ex.describe(req.tables)
            src.tables = _sources.whitelist_from_scan(columns, req.tables)
        except _sources.SourceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except DataSourceError as e:
            raise HTTPException(status_code=400, detail=f"{e}｜{e.hint}") from e
        _sources.save_source(cfg, src)
        return JSONResponse(_sources.to_public(src))

    @app.delete("/api/sources/{sid}")
    def sources_delete(sid: str, request: Request) -> JSONResponse:
        _require_cap(request, _identity.SOURCES_WRITE, "删除数据源")
        _sources_gate(request)
        if sid == "builtin":
            # 2026-09-09 撤掉。原来这里会**重写配置文件**把 datasource: 段删掉，
            # 而容器里那份配置是镜像内容：写成功了，Pod 一重启就回滚 ——
            # 界面显示"已删除"，下次发版它又回来了，是一次彻头彻尾的假持久化。
            # 内置源是配置不是运行时状态，改它就该改配置文件并发版。
            raise HTTPException(
                status_code=400,
                detail="内置数据源来自配置文件，不能在页面上删除 —— "
                       "容器里的配置随镜像发布，改了下次发版就会回滚。"
                       "要撤掉它，删配置里的 datasource: 段并重新发布。",
            )
        try:
            ok = _sources.delete_source(cfg, sid)
        except _sources.SourceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not ok:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return JSONResponse({"ok": True})

    @app.get("/api/eval")
    def evaluation(request: Request) -> dict[str, Any]:
        """已跑完的评测结果。

        没有结果文件时如实返回 available:false —— 页面据此显示"尚未运行"，
        而不是编一组数字出来。
        """
        _require_cap(request, _identity.QUALITY_READ, "查看评测结果")
        root = cfg.root / "evals" / "results"

        import json as _json

        def _read(p):
            if p is None:
                return None
            try:
                return _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None

        here = (f"{cfg.db_type}:"
                + (cfg.db_path.name if cfg.db_type == "duckdb" else _dsn_brief_id(cfg)))

        # 候选结果集。**按出处挑与当前数据源匹配的那一套** —— 同一份代码会
        # 部署成多个实例（对外实例连合成样例库、内部实例连生产库），
        # 写死优先某一套，总有一边看到的是别人的成绩。
        candidates = [
            (root / "ragforge-blind.json", root / "ragforge-ablation.json", None),
            (root / "blind.json", root / "ablation2.json", root / "ablation_F.json"),
        ]
        # 配置里 evaluation.out 说了"这个实例的成绩写在哪"，这里就先读它。
        # 此前不读，于是配置指定一处、页面读另一处 —— 没有内置数据源的实例
        # （对外实例就是）永远匹配不上任何一套出处，只能退回列表第一项，
        # 把**别的实例**的成绩摆在自己页面上，而且还标着"当前数据源"。
        declared = str((cfg.raw.get("evaluation") or {}).get("out") or "")
        declared_p = (cfg.root / declared) if declared else None
        if declared_p is not None:
            candidates.insert(
                0, (declared_p,
                    declared_p.with_name(declared_p.stem + "-ablation.json"), None))

        def _src_of(paths):
            """从一组结果文件里取出处。

            两种文件形状不同：blind 顶层直接是报告字段（n 是 int），
            ablation 顶层是 {组名: 报告}。不做类型判断就会对着 int 调 .get，
            这条路径本地测不到（结果文件齐全时先命中 blind 的 provenance），
            改动后立刻炸在有 ablation 无 blind 的组合上。
            """
            for pp in paths:
                d = _read(pp)
                if not isinstance(d, dict):
                    continue
                pv = d.get("provenance")
                if not isinstance(pv, dict):
                    for v in d.values():
                        if isinstance(v, dict) and isinstance(v.get("provenance"), dict):
                            pv = v["provenance"]
                            break
                if isinstance(pv, dict) and pv.get("datasource"):
                    return pv["datasource"]
            return ""

        avail = [c for c in candidates if c[0].exists() or c[1].exists()]
        if not avail:
            return {"available": False}
        matched = [c for c in avail if _same_source(_src_of(c), here)]
        blind_p, abl_p, fix_p = (matched or avail)[0]

        # 运行时跑出来的那一轮**优先取库里的**。
        #
        # 镜像里 evals/results/*.json 是**已公布的基线**（仓库内容，评审过、
        # 随镜像发布），它们该留在文件里；而页面上「运行回归」按出来的成绩是
        # 运行时状态 —— 换库之前它写在容器里，两个副本各写各的、发一次版全没了。
        # 所以这里只把"配置 evaluation.out 指定的那一份"改成从库里取，
        # 其余候选文件的读法一行不动。
        from . import evalstore

        _store_on = evalstore.enabled(cfg)
        _run_name = Path(declared).stem if declared else ""

        def _report(p, *, back: int = 0):
            if _store_on and _run_name and p is not None and Path(p).stem == _run_name:
                got = evalstore.latest(_run_name, back=back)
                if got is not None:
                    return got
                if back:
                    return None            # 库里只有一轮时不要回落到镜像里的 .prev
            return _read(p)

        out: dict[str, Any] = {"available": True}

        # 成绩的出处，以及它是否就是当前连着的这个数据源。
        # 「这组数字算不算数」全看这两项，必须带到前端去。
        prov = _first_provenance(_report(blind_p)) or _first_provenance(_read(abl_p)) or {}
        here = (f"{cfg.db_type}:"
                + (cfg.db_path.name if cfg.db_type == "duckdb"
                   else _dsn_brief_id(cfg)))
        # 这一份是不是本实例自己配置指定的那一份。**配置指定即算数**：
        # 没有内置数据源的实例上 here 退化成 ":?@?:"，与任何出处都比不出
        # "一致"，此时按不一致渲染就是在对着自家成绩说"这是别人的"。
        declared_hit = declared_p is not None and blind_p == declared_p
        out["provenance"] = {
            **prov,
            "current_datasource": here if cfg.db_type else (
                str((cfg.raw.get("evaluation") or {}).get("source") or "") or here),
            # 出处缺失时不敢断言"一致"——按不一致处理，宁可多提示一次
            "matches_current": declared_hit or (
                bool(prov) and _same_source(prov.get("datasource", ""), here)),
        }
        def _avg_tok(rep: Any) -> int | None:
            """每题平均 token（输入 + 输出）。

            这一轮之前跑出来的结果文件里没有 avg_tok 字段，用逐题记录现算，
            免得为了一个新指标要求所有人重跑一遍评测。
            """
            if not isinstance(rep, dict):
                return None
            if isinstance(rep.get("avg_tok"), (int, float)):
                return int(rep["avg_tok"])
            outs = rep.get("outcomes") or []
            if not outs:
                return None
            return round(sum((o.get("tok_in") or 0) + (o.get("tok_out") or 0)
                             for o in outs) / len(outs))

        if (b := _report(blind_p)):
            out["blind"] = {k: b.get(k) for k in
                            ("n", "accuracy", "false_reject", "block_rate",
                             "multi_misuse", "p95_ms", "cost_cny", "failure_kinds",
                             # 业务口径命中率。**这一轮跑之前的结果文件里没有这两个
                             # 键**，取不到就是 None —— 前端据此显示"—"而不是 0
                             "metric_hit_rate", "metric_graded_n",
                             "completeness", "complete_graded_n",
                             # 安全三项 + 场景覆盖。同样是后加的键：老结果文件里
                             # 没有，取到 None 就是"这一轮没考过"，前端显示未覆盖，
                             # 不能当 0 —— 0% 泄漏与一道题没考是两回事
                             "danger_block_rate", "escalation_rate", "leak_rate",
                             "scenes")}
            out["blind"]["avg_tok"] = _avg_tok(b)
            # 上一轮成绩（运行回归时留下的存档），用来出 token / 成本 / 耗时的环比。
            # **出处不一致就不给**：换了库、换了题库或换了模型，两轮之间差的
            # 不是这一版 Agent 的开销，箭头指哪儿全看运气。
            prev = (_report(blind_p, back=1) if _store_on
                    else _read(blind_p.with_name(blind_p.stem + ".prev.json")))
            pv_now = b.get("provenance") or {}
            pv_old = (prev or {}).get("provenance") or {}
            same_run = bool(pv_old) and (
                _same_source(pv_old.get("datasource", ""), pv_now.get("datasource", ""))
                and pv_old.get("golden") == pv_now.get("golden")
                and pv_old.get("model") == pv_now.get("model"))
            if prev and same_run:
                out["blind"]["prev"] = {
                    "n": prev.get("n"), "p95_ms": prev.get("p95_ms"),
                    "cost_cny": prev.get("cost_cny"), "avg_tok": _avg_tok(prev),
                }
        groups: list[dict[str, Any]] = []
        abl, fix = _read(abl_p) or {}, _read(fix_p) or {}
        for k in ("A", "B", "C", "D", "E", "F"):
            # E/F 取配额修复后的重跑，A–D 取原轮次
            src = fix.get(k) or abl.get(k)
            if not src:
                continue
            base_out = ((fix.get("A") or abl.get("A") or {}).get("outcomes")) or []
            outs = src.get("outcomes") or []
            groups.append({
                "key": k, "label": src["group"].split(" ", 1)[-1],
                "n": src["n"], "accuracy": src["accuracy"],
                "false_reject": src["false_reject"], "cost_cny": src["cost_cny"],
                "p95_ms": src["p95_ms"],
                "rerun": bool(fix.get(k)),
                # 相对基线 A 的配对增量 —— 图上画的是这个，不是绝对准确率
                "vs_base": _paired_delta(base_out, outs) if base_out and outs else None,
                "by_category": _by_category(outs),
            })
        out["groups"] = groups

        # 失败样本明细。此前页面只给了"链路失败 4 · 结果不一致 3"这样的汇总数，
        # 却在旁边写着"每条失败都带 trace_id，可从检查点原样复现"——
        # 既不列 trace_id 也没有入口，等于告诉你有这个能力却不给用它的路径。
        bd = _report(blind_p) or {}
        qmap: dict[str, str] = {}
        gpath = (bd.get("provenance") or {}).get("golden") or ""
        if gpath:
            gp = cfg.root / gpath
            if gp.exists():
                for line in gp.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        c = _json.loads(line)
                        qmap[c["id"]] = c.get("question", "")
        out["failures"] = [
            {"id": o["id"], "category": o.get("category", ""),
             "reason": o.get("reason", ""), "detail": (o.get("detail") or "")[:160],
             "trace_id": o.get("trace_id", ""),
             "question": qmap.get(o["id"], "")}
            for o in (bd.get("outcomes") or []) if not o.get("passed")
        ]
        # 黄金集构成。盲测只跑其中一部分（blind 标记的那些），
        # 而设计稿要回答"这套成绩覆盖了几类场景" —— 把**全集**与**本次跑了多少**
        # 一起给出去：只报其一都会让人误判覆盖面。
        if qmap or gpath:
            gp = cfg.root / gpath if gpath else None
            cases = []
            if gp and gp.exists():
                for line in gp.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        cases.append(_json.loads(line))
            if cases:
                by_cat: dict[str, int] = {}
                for c in cases:
                    k = str(c.get("category") or "未分类")
                    by_cat[k] = by_cat.get(k, 0) + 1
                out["golden"] = {
                    "path": gpath,
                    "total": len(cases),
                    "blind_n": sum(1 for c in cases if c.get("blind")),
                    "by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
                    # 评测集这份文件本身最后一次改动的时间。页面上要显示"最近更新"，
                    # 而唯一能说的真话就是文件 mtime —— 评测集没有版本号，也没有
                    # 任何地方记录"谁在什么时候改了考题"。
                    "updated_at": datetime.fromtimestamp(
                        gp.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                    # 每条题都有标准答案才算这套题是齐的。缺一条，分数就有一条是
                    # 判不了的 —— 这正是页面上那枚状态角标要回答的事。
                    "answered": sum(
                        1 for c in cases
                        if c.get("expect_sql") or c.get("expect_rule")
                        or c.get("kind") in ("no_leak", "no_escalation")),
                }

        # 评测集清单：每条用例 + 它在本轮的结果。
        #
        # 只给失败样本不够 —— 评测集页要回答的是"这套题都考了什么"，
        # 而通过的那些恰恰是覆盖面的主体。**没跑到的用例如实标 null**，
        # 不要拿"没失败"当"通过"：盲测只跑全集的一部分。
        outcome_by_id = {o["id"]: o for o in (bd.get("outcomes") or [])}
        if gpath:
            gp = cfg.root / gpath
            if gp.exists():
                cases = []
                for line in gp.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    c = _json.loads(line)
                    o = outcome_by_id.get(c["id"])
                    cases.append({
                        "id": c["id"],
                        "category": c.get("category", ""),
                        "scene": c.get("scene", ""),
                        "question": c.get("question", ""),
                        "in_blind": bool(c.get("blind")),
                        # 标准答案。设计稿这一列写的是「标准 SQL + 结果 18.6%」
                        # 「拒绝执行并解释只读边界」—— 描述的是**这条题judge时拿什么对**。
                        # 黄金集里每条都有 expect_sql（应拒用例是 expect_rule），
                        # 原来只显示 expect_cols/note，结果 58 条里 53 条是空的，
                        # 看起来像"大半的题没有标准答案"，而事实相反。
                        "expect": _golden_answer(c),
                        # 有没有标准答案。汇总那格「标准答案 X / Y」按它算 ——
                        # 不能拿总数当分子，那是默认所有题都判得了。
                        "has_answer": bool(
                            c.get("expect_sql") or c.get("expect_rule")
                            or c.get("kind") in ("no_leak", "no_escalation")),
                        # 本轮没跑到就是 null，不是"通过"
                        "passed": (None if o is None else bool(o.get("passed"))),
                        # 跑到了、但在这个数据源上判不动（例：跨租户题跑在
                        # 未启用租户隔离的源上）。**不能显示成 PASS** ——
                        # 那是拿一道没考的题去撑"守住了"。
                        "graded": (True if o is None else bool(o.get("graded", True))),
                        "reason": (o or {}).get("reason", ""),
                        "trace_id": (o or {}).get("trace_id", ""),
                    })
                out["cases"] = cases

        # 发布门禁评分（权重与目标值的性质见 _gate_score 的注释）
        if b := _report(blind_p):
            out["score"] = _gate_score(b)

        # 「最近回归记录」的行 —— 同一数据源下跑过的每一轮盲测各一行。
        #
        # 设计稿这块是「Agent v2.4 / v2.3 / v2.2」的版本对比。**结果文件里没有
        # Agent 版本这个字段**，askdb 也没有别的地方记它，编三行版本号就是编。
        # 能说的真话是：这一轮跑于什么时候（结果文件 mtime）、跑了多少条、
        # 按同一套门禁权重现算是多少分 —— 行标题因此用结果文件名而不是版本号。
        runs: list[dict[str, Any]] = []
        for p in sorted(root.glob("*.json")):
            d = _read(p)
            if not isinstance(d, dict) or not isinstance(d.get("outcomes"), list):
                continue
            # 跨数据源的成绩不能并排放 —— 那是两套题两个库，比出来的差值没有意义。
            # 比的基准是**上面正在显示的这一轮**的出处，不是当前连接：结果出自
            # 另一个库时上方抬头已经写明，这里若改用当前连接筛，会把正在显示的
            # 那一轮自己也筛掉，列表空着反而看不出它是哪来的。
            if not _same_source((d.get("provenance") or {}).get("datasource", ""),
                                prov.get("datasource", "") or here):
                continue
            sc = _gate_score(d)
            runs.append({
                "file": p.name,
                "n": d.get("n") or len(d["outcomes"]),
                "ran_at": datetime.fromtimestamp(
                    p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                "overall": sc["overall"],
                "pass": sc["pass"],
                "current": p.name == blind_p.name,
            })
        # 库里跑过的每一轮也要进这张表。**时间取入库时刻**，比文件 mtime 准 ——
        # 后者复制一次就漂一次（换库之前的 .prev.json 正是这么来的）。
        # 镜像里那份同名文件是发布基线，库里有记录时它不再代表"最近一轮"，
        # 所以按文件名去重，库里的优先。
        if _store_on and _run_name:
            db_runs = evalstore.runs(_run_name)
            runs = [r for r in runs if Path(r["file"]).stem != _run_name]
            for i, row in enumerate(db_runs):
                d = row["report"]
                if not isinstance(d, dict) or not isinstance(d.get("outcomes"), list):
                    continue
                if not _same_source((d.get("provenance") or {}).get("datasource", ""),
                                    prov.get("datasource", "") or here):
                    continue
                sc = _gate_score(d)
                runs.append({
                    "file": f"{row['name']}.json",
                    "n": d.get("n") or len(d["outcomes"]),
                    "ran_at": row["ran_at"],
                    "overall": sc["overall"],
                    "pass": sc["pass"],
                    "current": i == 0,
                })
        runs.sort(key=lambda r: r["ran_at"], reverse=True)
        out["runs"] = runs
        # 本轮跑完的时间。库里有就用入库时刻；只有文件时仍然只能说 mtime ——
        # 评测报告本身没有开跑/收工时间戳
        if _store_on and _run_name and (rows := evalstore.runs(_run_name)):
            out["ran_at"] = rows[0]["ran_at"]
        elif blind_p.exists():
            out["ran_at"] = datetime.fromtimestamp(
                blind_p.stat().st_mtime).astimezone().isoformat(timespec="seconds")

        # 故障注入结果（evals/chaos.py 跑出来的）。**同样按出处挑** ——
        # 注入跑在哪个库上决定了这组数字说的是谁的恢复能力；出处对不上就不给，
        # 页面那张卡退回"未测量"，而不是把别的实例的成绩摆上去。
        chaos_p = root / "chaos.json"
        cd = _read(chaos_p if chaos_p.exists() else None)
        if isinstance(cd, dict) and isinstance(cd.get("faults"), list):
            cprov = (cd.get("provenance") or {}).get("datasource", "")
            # 出处与当前连接对不上时**照给、但标出来** —— 与上面盲测成绩
            # 同一套口径（matches_current）。直接藏掉的话，跑在样例库上的
            # 那一轮就永远没地方看；而这个实例根本没有默认数据源，
            # "对得上"这件事在这里从来不会为真。
            out["chaos"] = {
                    "datasource": cprov,
                    "matches_current": _same_source(cprov, here),
                    "n_cases": cd.get("n_cases") or 0,
                    # 基线没跑通而被排除的题数照实给出去：只报 11/12 不说分母
                    # 怎么来的，那个比例读起来就比实际可信
                    "skipped": cd.get("skipped") or 0,
                    "ran_at": datetime.fromtimestamp(
                        chaos_p.stat().st_mtime).astimezone().isoformat(
                            timespec="seconds"),
                    "faults": [
                        {"key": f.get("key", ""), "label": f.get("label", ""),
                         "injected": f.get("injected") or 0,
                         "recovered": f.get("recovered") or 0,
                         "rate": f.get("rate")}
                        for f in cd["faults"]
                    ],
            }

        # 复现必须用同一份配置：检查点库跟着配置走
        out["replay_config"] = (bd.get("provenance") or {}).get("config", "")
        out["shipped"] = "E"     # 当前默认配置对应的组（多步已按消融结论关闭）
        return out

    @app.get("/api/audit")
    def audit_list(request: Request, page: int = 1, page_size: int = 10,
                   q: str = "", kind: str = "", status: str = "",
                   source: str | None = None, user: str | None = None,
                   since: str = "all") -> dict[str, Any]:
        """审计流水（摘要分页）。列表有意不含 SQL 文本与结果行 ——
        细节只经 /api/replay 的白名单+开关出去。

        可见范围按角色分两层（《角色与权限设计》A-01）：
          · AUDIT_ALL     —— 没有就只看得到自己发起的记录（产品、测试）
          · AUDIT_CONTENT —— 没有就看不到 question（系统管理员：它要知道
            有没有人在违规访问，不需要知道业务上问了什么）

        **问题原文对未登录访问者同样可见**（产品决定，2026-09-06）：审计与追踪
        两页要讲的是"这套东西在真实调用上如何运转"，标题一律遮成"（提问）"
        的话这两页就没有可读性了。这条决定落在 identity.CAPABILITIES 的
        ANONYMOUS 一行上，而不是在这里写死 True —— 开关只留一处，
        才不会出现"这里放开了、别处没跟上"。改回来就是把那行里的
        AUDIT_CONTENT 去掉。

        注意这条边界只覆盖问题原文与发起人；SQL 全文、结果行仍只经 /api/replay
        的开关出去，写入类接口仍由写入中间件按登录态拒绝。
        """
        _require_cap(request, _identity.AUDIT_READ, "查看审计流水")
        from .audit import SINCE_CHOICES, list_audits

        # status 只认这三档：非法值当"不筛"处理会让人以为筛过了，直接拒
        wanted = status.strip()
        if wanted and wanted not in ("ok", "rejected", "interrupted"):
            raise HTTPException(status_code=400,
                                detail="status 只能是 ok / rejected / interrupted")
        window = since.strip() or "all"
        if window not in SINCE_CHOICES:
            raise HTTPException(status_code=400,
                                detail="since 只能是 " + " / ".join(SINCE_CHOICES))
        with_text = _can(request, _identity.AUDIT_CONTENT)
        # 看不到原文的身份不接受按发起人筛：那是一个预言机（"某某有 12 条命中"
        # 本身就把内容说出去了）。静默忽略更糟 —— 页面会以为筛过了。
        if user is not None and not with_text:
            raise HTTPException(status_code=403,
                                detail="当前身份看不到发起人，不能按发起人筛选")
        return list_audits(cfg, page=page, page_size=page_size,
                           q=q.strip(), kind=kind.strip(),
                           with_text=with_text,
                           only_user=_audit_owner_filter(request),
                           status=wanted,
                           source=None if source is None else source.strip(),
                           user=None if user is None else user.strip(),
                           since=window)

    @app.get("/api/audit/stats")
    def audit_stats(request: Request, days: int = 30) -> dict[str, Any]:
        """时间窗统计：调用/拦截率/成本/按日序列。

        replay_api 开关状态一并带出 —— 前端据此决定"复放"入口
        显示还是置灰，而不是点了才发现 404。
        """
        _require_cap(request, _identity.AUDIT_READ, "查看审计统计")
        from .audit import stats as _stats

        days = min(max(int(days), 1), 365)
        owner = _audit_owner_filter(request)
        # 审批汇总在这里合，不在 audit.stats() 里读 —— 与任务中心
        # （见 /api/tasks 的 open_approval_ids）同一条口径：审计不认识
        # approvals 存储，也不该认识，那是两套存储。
        #
        # only_user 传的是同一个 owner：三张卡收敛到本人、审批那张给全量，
        # 就是一次可见范围泄露（谁在申请跑大查询、被驳回几次一眼可见）。
        try:
            approval = _approvals.summary(cfg, days=days, only_user=owner)
        except Exception:
            # 审批存储不可用不该让整张审计页打不开。给 None 而不是 0 ——
            # 0 会被读成"没有待审批"，而实际是"这次没算出来"。
            approval = {"pending": None, "decided": None, "avg_decide_ms": None}
        return {
            **_stats(cfg, days=days, only_user=owner),
            "approval": approval,
            "replay_api": bool(cfg.raw["observability"].get("replay_api", False)),
            "tracing": _obs_status(),
        }

    @app.get("/api/trace")
    def trace_chain_api(request: Request, trace_id: str = "") -> JSONResponse:
        """执行追踪页的节点链（模型、token、SQL 哈希、逐节点耗时与结果）。

        为什么不复用 /api/replay：回放要登录、要 observability.replay_api 开关，
        而它返回 SQL 全文与问题原文 —— 那三道门是给 SQL 全文设的。节点链本身
        既不含 SQL 文本也不含结果行，却被一起挡在门后，结果是执行追踪页在
        它最常见的形态（未登录 / 回放关闭）下右半屏全是占位符，而这一页存在
        的全部意义就是把链路显出来。

        这里只放宽"谁能看"，没有放宽"能看到什么"：
        - 字段走 audit.TRACE_FIELDS + STEP_FIELDS 双白名单，sql_raw / sql_final /
          question / rows 一个都不出接口；SQL 只以 sha256 出现；
        - 仍按调用者**当下**的可见表收窄 —— 步骤 note 里会出现表名与租户谓词，
          不收窄就等于把别人查过的表结构送出去；
        - 记录不存在与看不到同为 404，沿用回放那条"不区分"的约定。
        """
        _require_cap(request, _identity.AUDIT_READ, "查看执行追踪")
        not_found = JSONResponse({"error": "not found"}, status_code=404)
        if not _TRACE_ID_RE.fullmatch(trace_id or ""):
            return not_found

        from .audit import get_audit, trace_chain

        rec = get_audit(cfg, trace_id)
        if rec is None:
            return not_found

        scoped = _scoped(request, _cfg_of_record(rec, request))
        hit = {str(t).lower() for t in (rec.get("tables_hit") or [])}
        if hit and not hit <= {t.lower() for t in scoped.tables}:
            return not_found

        return JSONResponse(trace_chain(rec))

    @app.get("/api/replay")
    def replay_trace(request: Request, trace_id: str = "") -> JSONResponse:
        """判定链路回放（设计说明 V1.1）。

        三条硬规则，都是为了"接口本身在任何实例上都不泄露数据"：
        - 字段白名单（audit.REPLAY_FIELDS）：rows / schema_prompt 永不出接口；
        - 开关关闭、id 非法、id 不存在 **同为 404**，不区分"不存在"与
          "存在但无权"——区分本身就是信息泄露；
        - 独立限流：每次回放都要开 SQLite 遍历历史，不能被当查询接口刷。
        """
        not_found = JSONResponse({"error": "not found"}, status_code=404)
        # 回放要登录：它返回的是 SQL 全文与问题原文，比列表那一行敏感得多。
        # 与开关关闭、id 不存在**同为 404** —— 沿用本接口既有的"三种结局
        # 同一响应"约定，区分本身就是信息泄露。
        if not _current_user(request):
            return not_found
        # 能力位在这里**不能用 _require_cap**：那会抛 403，等于告诉调用方
        # "这条记录存在，只是你没权限"。本接口的全部结局必须收敛到同一个 404，
        # 否则前面三条硬规则白设 —— 403 与 404 的差别本身就是一位信息。
        if not _can(request, _identity.REPLAY):
            return not_found
        if not cfg.raw["observability"].get("replay_api", False):
            return not_found
        if not _REPLAY_RL.allow(_rl_key(request)):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not _TRACE_ID_RE.fullmatch(trace_id or ""):
            return not_found

        from .audit import REPLAY_FIELDS, get_audit

        rec = get_audit(cfg, trace_id)
        if rec is None:
            return not_found

        # **按复放者当下的角色重新收窄，不沿用记录里的 role。**
        #
        # 不做这一步，复放就是一条现成的提权路径：低权限者拿到 trace_id
        # 即可读到数据负责人查过的最终 SQL —— 而 SQL 里带着表名、字段名、
        # 过滤条件，等于把那张表的结构和口径一起给了出去。
        #
        # 判据用记录里的 tables_hit 与本人此刻可见表取子集关系：
        # 记录是历史，权限是现在，一个人今天被移出某个角色，
        # 昨天的记录就该跟着看不见了。
        # 收窄按**记录自己的数据源**取，理由见 _cfg_of_record ——
        # 拿内置配置判，多源之后等于把运行时源上的记录整片挡在门外。
        scoped = _scoped(request, _cfg_of_record(rec, request))
        hit = {str(t).lower() for t in (rec.get("tables_hit") or [])}
        if hit and not hit <= {t.lower() for t in scoped.tables}:
            return not_found

        out = {k: rec.get(k) for k in REPLAY_FIELDS}
        # 检查点快照只有走图的调用（ask）才有；直查/配额拦截没有线程，
        # 如实给空列表而不是省略字段 —— 前端不用猜字段存不存在。
        snapshots: list[dict[str, Any]] = []
        if rec.get("kind", "ask") in ("ask", "resume") and rec.get("attempts"):
            from .graph import replay as _snap

            try:
                # 续跑记录的检查点在原任务的线程上（trace 新开、thread 不变）
                snapshots = _snap(rec.get("thread_id") or trace_id, cfg)
            except Exception:
                snapshots = []
        out["snapshots"] = snapshots
        return JSONResponse(out)

    # ---------- 身份与权限 ----------
    #
    # 认证不在这里：谁是谁交给 auth-gateway（它已有 JWKS、token-exchange、
    # 应用级 membership）。本组接口只管"谁属于哪个角色"这一件事。
    #
    # askdb 的登录是固定体验账号，不携带网关身份；而成员写接口要按网关
    # auth_user_id 授权，auth-gateway 对接尚未落地，写接口因此**没有可依据的
    # 请求方身份**。在那之前用一把部署方持有的管理员令牌兜底，并且 fail-closed：
    # 没配 ASKDB_ADMIN_TOKEN 就整体拒绝写入。缺了这道闸，任何能访问页面的人
    # 都能给自己加角色。
    def _require_admin(token: str | None, request: Request | None = None) -> None:
        """成员写入的准入：**系统管理员角色**，或部署方的管理员令牌。

        令牌这条路留着，但它的定位变了 —— 从"唯一依据"降为 break-glass：
        身份库自身出问题、没人能登进来时，运维仍要有办法把成员改回去。
        日常路径应当是登录后按角色走，因为令牌是共享的，
        它记不下"是谁改的"，而成员变更恰恰是最需要留痕的一类操作。
        """
        if request is not None and _can(request, _identity.MEMBERS_WRITE):
            return

        expected = os.environ.get("ASKDB_ADMIN_TOKEN", "")
        if not expected:
            raise HTTPException(
                status_code=403,
                detail="当前角色无权增删成员，且本实例未配置 ASKDB_ADMIN_TOKEN。"
                       "成员变更由系统管理员执行；运维可配置该环境变量作为应急通道。",
            )
        if not secrets.compare_digest(token or "", expected):
            raise HTTPException(status_code=401, detail="管理员令牌不正确")

    def _require_identity() -> None:
        if not _identity.enabled(cfg):
            raise HTTPException(
                status_code=404,
                detail="本实例未启用身份与权限（未配置 identity.dsn）。",
            )

    @app.get("/api/identity/roles")
    def identity_roles() -> dict[str, Any]:
        """角色清单。**未启用时也返回 200** —— 角色定义写在源码里，不是秘密，
        而前端需要据此渲染「未启用」而不是「接口坏了」。"""
        try:
            roles = _identity.roles_with_counts(cfg)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"身份库不可用：{e}") from e
        return {
            "enabled": _identity.enabled(cfg),
            "writable": bool(os.environ.get("ASKDB_ADMIN_TOKEN")),
            "roles": roles,
        }

    @app.get("/api/identity/members")
    def identity_members(request: Request, role: str = "",
                         page: int = 1, page_size: int = 10,
                         q: str = "", bound: str = "all",
                         since: str = "all") -> dict[str, Any]:
        """成员名册。跨角色要 MEMBERS_READ，**看自己所属角色不需要**。

        为什么留这个口子：一个人有权知道自己和谁同组 —— 那是他所在角色的
        构成，不是别人的信息。而完整名册是组织结构，属于治理数据，
        只给数据负责人与系统管理员（设计文档 I-02）。

        判据用「请求的 role 是不是自己的角色之一」，不是"有没有登录"：
        后者等于把整份名册开给任何登录用户，与不设门只差一步。
        """
        want = role.strip()
        if not (want and want in _roles(request)):
            _require_cap(request, _identity.MEMBERS_READ, "查看其他角色的成员名册")
        _require_identity()
        for name, value, allowed in (
            ("bound", bound, _identity.MEMBER_BOUND_CHOICES),
            ("since", since, _identity.MEMBER_SINCE_CHOICES),
        ):
            if value not in allowed:
                raise HTTPException(status_code=400,
                                    detail=f"{name} 只能是 " + " / ".join(allowed))
        try:
            # 分页在库里做（LIMIT/OFFSET + COUNT），不是读全量再切：
            # 一个角色几百人时，出网的与读出来的都只有这一页
            result = _identity.members_page(cfg, role.strip(),
                                            page=page, page_size=page_size,
                                            q=q, bound=bound, since=since)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"身份库不可用：{e}") from e
        # 未登录（匿名）：成员 PII 在下发前脱敏。前端还会再叠一层模糊，但明文
        # 不外泄靠的是这里——响应体里就没有真名。登录用户按角色照常看真实值。
        if _current_user(request) is None:
            for m in result.get("items", []):
                # 网关用户名按产品决定放出来看，只脱敏姓名与备注
                m["display_name"] = _mask_pii(m.get("display_name", ""))
                m["note"] = _mask_pii(m.get("note", ""))
        return result

    @app.post("/api/identity/members")
    def identity_add_member(
        req: AddMemberRequest,
        request: Request,
        x_askdb_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_identity()
        _require_admin(x_askdb_admin_token, request)
        try:
            return _identity.add_member(
                cfg, role_code=req.role_code, username=req.username,
                display_name=req.display_name, note=req.note,
                # 走角色的记真名，走令牌的仍记 admin-token —— 令牌是共享的，
                # 记一个具体人名会是编造。留痕的价值在于事后能对上人，
                # 对不上的时候就该如实说对不上。
                created_by=_current_user(request) or "admin-token")
        except _identity.IdentityError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except _identity.IdentityDisabled as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.delete("/api/identity/members/{member_id}")
    def identity_remove_member(
        member_id: int,
        request: Request,
        x_askdb_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_identity()
        _require_admin(x_askdb_admin_token, request)
        if not _identity.remove_member(cfg, member_id):
            raise HTTPException(status_code=404, detail="成员不存在")
        return {"ok": True}

    @app.get("/api/approvals")
    def approvals_list(request: Request) -> dict[str, Any]:
        """待审批队列。

        可见范围与审计同一条口径：有 APPROVE 的看全部，没有的只看自己提的。
        自己提的必须能看到 —— 否则申请人无从知道批没批，只能反复重试。
        """
        _require_login(request)
        can_approve = _can(request, _identity.APPROVE)
        return {
            "can_approve": can_approve,
            "items": _approvals.listing(
                cfg, only_user=None if can_approve else (_current_user(request) or "")),
        }

    @app.post("/api/approvals/{approval_id}/decide")
    def approvals_decide(approval_id: str, req: DecideRequest,
                         request: Request) -> dict[str, Any]:
        """放行或驳回。**只有系统管理员**（设计文档 Q-08 / V1.1）。

        数据负责人有意不在此列：数据源变更由它提出，兼任放行方会让
        「提出与放行分属两人」失效。

        自批此前是结构上不可能的（系统管理员查不到任何数据，因而不可能是
        发起人）；2026-09-06 它也能查数之后，改由 approvals.decide 显式拒绝，
        在这里落成 403。
        """
        _require_login(request)
        _require_cap(request, _identity.APPROVE, "审批高成本查询")
        try:
            rec = _approvals.decide(cfg, approval_id,
                                    approver=_current_user(request) or "",
                                    approved=bool(req.approved), note=req.note)
        except _approvals.SelfApproval as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        if rec is None:
            # 不存在与"已经批过"合并成同一句：重复决策不是错误，
            # 但也不该悄悄覆盖前一个人的结论。
            raise HTTPException(status_code=409,
                                detail="该申请不存在，或已经有过结论，不能重复决策。")
        return rec

    @app.get("/api/reviews")
    def reviews_list(request: Request) -> dict[str, Any]:
        """待复核队列 —— 跑成了、但结果带存疑痕迹的那些。

        **与审批是两件事**：审批是事前"这条该不该去跑"，复核是事后"跑出来的
        数字算不算数"。决策人恰好都是系统管理员、动作恰好都是放行/打回，
        但触发时机与判定对象完全不同，所以是两条队列、两套存储。

        待复核不预先登记：它由审计里的痕迹（盲选召回、脱敏退化、反复重试、
        触顶收敛）确定性地推导出来。登记反而会引入"痕迹在、登记没写成"
        这种第三态。已决的那些从复核存储取回来。

        可见范围与审批同一条口径：有 APPROVE 的看全部，没有的只看自己发起的
        —— 发起人必须看得到自己的结果被判成什么，否则他不知道那个数字还能不能用。
        """
        _require_login(request)
        from .audit import tasks as _tasks

        can_review = _can(request, _identity.APPROVE)
        me = _current_user(request) or ""
        items = _tasks(
            cfg,
            None if can_review else me,
            max_rows=cfg.max_rows,
            max_scan_rows=int(cfg.raw["guard"]["max_scan_rows"]),
            review_status=_reviews.decided(cfg),
        )
        pending = [t for t in items if t.get("status") == _audit.WAITING_REVIEW]
        return {
            "can_review": can_review,
            "items": _reviews.listing(cfg, pending),
            "pending": len(pending),
        }

    @app.post("/api/reviews/{trace_id}/decide")
    def reviews_decide(trace_id: str, req: ReviewRequest,
                       request: Request) -> dict[str, Any]:
        """采信或打回一条存疑结果。**只有系统管理员**，与审批同一道门。

        打回**不撤销已经发生的事**：数字早就返回给发起人了。复核改变的是
        这条记录此后的可信标记与任务态 —— 想真正阻止结果外流，那是事前审批
        的职责。两件事别混，也别在这里假装做得到。
        """
        _require_login(request)
        _require_cap(request, _identity.APPROVE, "复核查询结果")
        if not _TRACE_ID_RE.fullmatch(trace_id or ""):
            raise HTTPException(status_code=404, detail="记录不存在")

        from .audit import get_audit

        rec = get_audit(cfg, trace_id)
        if rec is None or not _audit.needs_review(rec):
            # 不存在、已被拦下、或本就不需要复核 —— 合并成同一句：
            # 复核队列不是一个可以拿来试探"某条记录存不存在"的入口。
            raise HTTPException(status_code=404,
                                detail="该记录不存在，或不在待复核范围内。")
        try:
            out = _reviews.decide(cfg, trace_id,
                                  reviewer=_current_user(request) or "",
                                  accepted=bool(req.accepted), note=req.note,
                                  owner=str(rec.get("user") or ""))
        except _reviews.SelfReview as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        return out

    @app.get("/api/tasks")
    def tasks(request: Request, page: int = 1, page_size: int = 10,
              status: str = "all", source: str = "all", risk: str = "all",
              user: str = "all", since: str = "all", q: str = "") -> dict[str, Any]:
        """执行线程一页，新的在前。筛选、统计与切页都在这里做。

        列全部而不是只列中断的：中断只在异常逃出执行图时才发生（进程故障、
        递归超限、检查点库异常），是故障态不是常规流程 —— 只列中断等于这一页
        正常情况下永远是空的。可续跑的那些由 resumable 字段标出来，
        续跑入口只对它们开放。

        **可见范围按 TASKS_ALL 能力位，不再按发起人**（2026-09-06）。

        原来这一页只列当前账号发起的线程。可见面统一之后那条收窄站不住 ——
        它的实际效果是：审计中心列着所有人的记录（未登录访客也看得到全部
        原文），而登录用户打开任务中心是空的。同一份审计流水，两页两套口径，
        且方向正好相反。这次改动要消灭的就是这个形状。

        **看得见不等于动得了**：续跑（/api/resume）的归属校验一行没改，
        别人的线程列得出来但续不了，前端据 owner 字段把入口置灰 ——
        与"未登录可读不可写"是同一条轴。

        **分页在服务端（2026-09-08）。** 原来这里一次把全部线程发出去，
        由浏览器筛、统计、切页 —— 实测一次一千四百多条，而屏幕上只有十行。
        现在筛选与统计都交给 audit.paginate_tasks：统计卡与筛选下拉算在筛选
        之前（否则四个数字会跟着筛选变，那就不是"系统当下的处境"了），
        分页算在筛选之后。出网的只剩当前这一页。

        筛选取值里 ``all`` 是不筛，空串是**合法的一档**（未记录数据源 /
        匿名发起）；非法取值一律 400，当成"不筛"处理会让人以为筛过了。

        检查点核实（is_resumable）也跟着只做**本页这几条**：它每条要开一次
        检查点库，原来是按全部线程数做的，那才是这个接口真正的开销。
        """
        for name, value, allowed in (
            ("status", status, _audit.TASK_STATUSES),
            ("risk", risk, _audit.RISK_LEVELS),
            ("since", since, _audit.SINCE_CHOICES),
        ):
            if value != "all" and value not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"{name} 只能是 all / " + " / ".join(allowed))
        username = _current_user(request) or ""
        from .audit import tasks as _tasks
        from .graph import is_resumable

        # 未决审批要联查进来：R-11 被拦下的那条**在等人放行**，不是终局。
        # 只看审计的话它与"碰了安全红线"长得一模一样，页面上都是「已拦截」，
        # 而两者的下一步一个是"找负责人点一下"、一个是"这条永远过不去"。
        # 审计不认识 approvals 存储（两套存储，耦合进去就没法单测），
        # 所以在这里取、按 id 传进去。
        open_ids: set[str] = set()
        try:
            open_ids = {str(a.get("id")) for a in _approvals.state(cfg).values()
                        if a.get("status") == _approvals.REQUESTED}
        except Exception:
            open_ids = set()          # 审批存储不可用不该让任务中心整页打不开

        # 复核结论同理：待复核是从审计痕迹推导的，**已决的那些**要从复核
        # 存储取回来，否则采信过的结果会永远挂在队列里。
        reviewed: dict[str, str] = {}
        try:
            reviewed = _reviews.decided(cfg)
        except Exception:
            reviewed = {}

        # 阈值传进去做风险折算（审计里没有风险字段，见 audit._risk 的说明）
        items = _tasks(
            cfg,
            None if _can(request, _identity.TASKS_ALL) else username,
            max_rows=cfg.max_rows,
            max_scan_rows=int(cfg.raw["guard"]["max_scan_rows"]),
            open_approval_ids=open_ids,
            review_status=reviewed,
        )
        # 审计只知道这条线程上次以 INTERRUPTED 收尾（或只落了发起记录），
        # 不知道现场有没有真的落盘、也不知道后来是不是已被续跑跑完 ——
        # 只按审计标 resumable，会出现"这里说能续、点下去 404"。
        # 以检查点为准再核一遍：真正在跑的线程此刻没有可续的断点，
        # 会在这里被核回 False；被杀掉那条留着现场，核得过。
        result = _audit.paginate_tasks(
            items, page=page, page_size=page_size, status=status,
            source=source, risk=risk, user=user, since=since, q=q.strip(),
            # 日界按配置声明的时区算：容器时钟是 UTC，不传这个，「今日完成」
            # 会到北京时间早上八点才翻页
            tz=_audit.day_tz(cfg))
        for it in result["items"]:
            if it.get("resumable"):
                state = is_resumable(str(it.get("thread_id") or ""), cfg)
                if state is not None:
                    it["resumable"] = state
        # user 是**当前账号**，不是过滤条件：页面拿它与每条的 owner 比，
        # 判断哪些是自己的、续跑入口对谁开。匿名时为空串。
        result["user"] = username
        # 这一页只看最近 TASKS_MAX_THREADS 条线程（见 audit.tasks 那段说明）。
        # **把上限说出来**：不说的话，"最近这些线程里没有"会被读成"没有"，
        # 而那正是这套界面反复要消灭的那种静默收窄。
        result["window"] = {
            "max_threads": _audit.TASKS_MAX_THREADS,
            "truncated": len(items) >= _audit.TASKS_MAX_THREADS,
        }
        return result

    @app.post("/api/resume")
    def resume_task(req: ResumeRequest, request: Request) -> JSONResponse:
        """从断点续跑一次中断的提问（中断恢复设计 V1.1）。

        只接受调用方自己持有的 thread_id；格式非法、不存在、已跑完
        一律 404 且响应一致 —— 不提供未完成任务的枚举入口（§4.2）。
        入口层限流应与 /api/ask 同档（见 deploy/nginx-askdb.conf）。
        """
        not_found = JSONResponse({"error": "not found"}, status_code=404)
        if not _TRACE_ID_RE.fullmatch(req.thread_id or ""):
            return not_found
        # 归属校验：有主的任务只能由发起人续跑。
        # 匿名发起的任务保持原语义（凭 thread_id 续跑）—— 那是登录之前的行为，
        # 不因为加了账号就把老任务锁死。
        from .audit import AuditFilter, iter_records

        owner = ""
        origin_source = ""
        # **带上发起记录**（include_started）：进程被杀那种线程只剩这一条，
        # 而归属与数据源正是从它取。滤掉它就等于"任务中心说能续跑、这里说
        # 你当初跑在 builtin 上" —— 实测过一次，就是这条 400。
        #
        # 2026-09-09：按 thread_id 下推。原来是把**全部审计记录**读回来再逐条
        # 比对，为了一条线程读几十万条；现在筛选进 SQL，取到第一条就 break，
        # 库后端因此只发生一次 FETCH。
        # closing 是必需的，不是讲究：只取第一条就 break，而生成器一旦提前
        # 离开，库后端那条服务端游标就还占着池子里的一条连接（池子只有 6 条）。
        # 靠垃圾回收顺手关掉能work，但那是在赌 CPython 的引用计数时机。
        stream = iter_records(cfg, AuditFilter(include_started=True,
                                               thread_ids=(req.thread_id,)))
        with closing(stream):
            for rec in stream:
                owner = rec.get("user") or ""
                # 续跑必须回到**当初那个数据源**。审计里存了它（_audit_of 的
                # source 字段），所以不需要调用方再传一次 —— 传参既多一处
                # 契约，又给了"在 A 源发起、拿 B 源续跑"的可乘之机。
                origin_source = str(rec.get("source") or "")
                break
        if owner and owner != (_current_user(request) or ""):
            return not_found          # 与"不存在"同一响应，不暴露任务是否存在

        try:
            # 传 request：续跑同样要过环境校验。任务是历史，权限是现在 ——
            # 一个人今天被移出某个角色，昨天发起的任务不该还能接着跑。
            base = _cfg_for(origin_source, request)
        except HTTPException as e:
            # 源被删了 / 表被收回 / 实例没有默认源。这不是"任务不存在"，
            # 得说清楚是哪一步走不通，否则用户只看到一个 500。
            raise HTTPException(
                status_code=e.status_code,
                detail=f"这条任务当初跑在数据源「{origin_source or 'builtin'}」上，"
                       f"现在没法回到那里：{e.detail}",
            ) from e

        scoped = _scoped(request, base)
        r = run_resume(req.thread_id, scoped)
        if r is None:
            return not_found
        return JSONResponse(r.to_dict())

    # 登录失败限流。口令是离线可爆破的，接口侧必须先把速率压下去。
    #
    # 2026-09-09 修：原来调的是不带 key 的 _LOGIN_RL.allow()，也就是**全站
    # 共用一个 10 次/分钟的桶**。后果有两条，都不是"限得紧一点"那么轻：
    #   · 任何人每分钟发 10 个请求，就能让所有人登不上去 —— 一次零成本的拒绝服务；
    #   · 日活十万级下，正常登录量本身就会把这个桶顶满。
    # 改成按 _login_rl_key 分桶（只看真实来源地址，理由见 auth_login），
    # 每 IP 10 次/分钟对爆破依然足够紧，而一个人再怎么试也锁不住别人。
    _LOGIN_RL = _RateLimit(limit=10, window_s=60)

    def _current_user(request: Request) -> str | None:
        return _auth.read(request.cookies.get(_auth.COOKIE_NAME))

    def _scoped(request: Request, base: Config | None = None) -> Config:
        """本次调用生效的配置 —— 先定数据源，再按调用方角色收窄。

        **两条查询链路共用这一个入口**。角色的解析只有这一处，
        接入新的认证方式也只改这里；散开写就迟早漏掉一条，
        而漏掉的那条就是一条无声的提权路径。

        base 是本次要查的数据源派生出的配置（默认内置源）。顺序不能反：
        收窄只去表不加表，但换源那一步会把收窄结果整个替掉 ——
        先收窄再换源等于绕开权限。角色名单始终从内置配置读，
        它是这套部署的身份来源，不随数据源变。
        """
        base = cfg if base is None else base
        username = _current_user(request)
        if not username:
            return _identity.for_role(base, _identity.ANONYMOUS)
        return _identity.for_roles(base, _auth.roles_of(cfg, username), user=username)

    def _require_login(request: Request) -> None:
        if _auth.required(cfg) and not _current_user(request):
            raise HTTPException(status_code=401, detail="本实例需要登录后才能查询")

    def _roles(request: Request) -> list[str]:
        """调用方的角色码。未登录得到 [ANONYMOUS] —— 它是一个普通角色。

        没有"没有身份"这种第三态，判定代码里因此少一整类判空错误。
        这条与 _scoped 的口径必须一致，两处都从内置配置读角色名单。
        """
        username = _current_user(request)
        if not username:
            return [_identity.ANONYMOUS]
        return _auth.roles_of(cfg, username) or [_identity.ANONYMOUS]

    def _can(request: Request, cap: str) -> bool:
        return _identity.can(_roles(request), cap)

    def _apply_waiver(scoped: Config, request: Request, *, aid: str,
                      kind: str, text: str) -> Config:
        """校验审批单，通过就在这次调用的配置上打一个放行标记。

        走配置而不是层层传参，与 role / tables / max_rows 完全一条路 ——
        执行链路因此不需要知道"审批"这个概念存在，也就不会有人在
        某个分支上忘了判。

        校验不过一律 403 并把原因原话给出去：审批被拒、单子过期、
        内容对不上，这三种情况用户的下一步动作完全不同，含糊其辞
        会让他反复重试同一个不可能成功的操作。
        """
        if not aid:
            return scoped
        why = _approvals.waiver(cfg, aid, user=_current_user(request) or "",
                                kind=kind, text=text)
        if why:
            raise HTTPException(status_code=403, detail=why)
        return dataclasses.replace(
            scoped, raw={**scoped.raw, "_scan_waiver": True})

    def _open_approval(scoped: Config, request: Request, *, trace_id: str,
                       kind: str, question: str, sql: str, match_text: str,
                       est_rows: int | None) -> dict[str, Any]:
        """超阈值时登记一条待审批，并把单号回给发起人。

        R-11 此前直接打回并附一句"缩小时间范围"。对一次性的年度对账来说
        那是一句无解的话 —— 需求本身就要扫那么多行。于是人要么放弃，
        要么绕开 askdb 直接连库，而后者正是这套系统要消灭的行为。
        """
        rec = _approvals.request(
            cfg, trace_id=trace_id, user=_current_user(request) or "",
            roles=_roles(request), kind=kind, question=question, sql=sql,
            match_text=match_text, est_rows=est_rows,
            threshold=int(cfg.raw["guard"]["max_scan_rows"]),
            source=scoped.source_id or "builtin")
        return {"approval_id": rec["id"], "approval_status": rec["status"]}

    def _audit_owner_filter(request: Request) -> str | None:
        """审计的可见范围：None = 全量，字符串 = 只看这个人发起的。

        列表与统计**必须共用这一个函数**。两处各写一遍判断，就迟早出现
        "列表收敛了、统计没收敛"——而按天聚合的成本卡本身就是一次泄露。
        """
        if _can(request, _identity.AUDIT_ALL):
            return None
        return _current_user(request) or ""

    def _require_cap(request: Request, cap: str, what: str) -> None:
        """能力位判定。**接口入口处的那一层。**

        与 _require_scope 的分工：这里回答"进不进得了这个功能"，
        那里回答"进来之后看得到哪些数据"。两个问题混在一起判，
        就会出现"表白名单为空所以顺带把功能也关了"这种把因果说反的提示。

        措辞上给出角色名而不是角色码：看到这句话的人是业务方，
        「当前角色（产品）无权测试数据源连接」比「PRODUCT lacks sources.test」
        更能让他知道该找谁。
        """
        if _can(request, cap):
            return
        codes = _roles(request)
        names = "、".join(
            _identity.ROLE_BY_CODE[c].name for c in codes if c in _identity.ROLE_BY_CODE
        ) or "未登录"
        raise HTTPException(
            status_code=403,
            detail=f"当前角色（{names}）无权{what}。如需该权限，请联系系统管理员调整角色归属。",
        )

    def _require_scope(scoped: Config) -> None:
        """当前角色一张表都看不到时，给一句能懂的话。

        不这么做的话，用户会撞上 R-03「用到了没有开放的表」——
        那是给"表没开放"准备的措辞，用在"你没有数据角色"上会把人引向
        完全错误的排查方向。
        """
        if not scoped.tables:
            raise HTTPException(status_code=403, detail=_NO_DATA_ROLE)

    def _set_session(response: Response, username: str) -> None:
        # 票面有效期与 cookie max_age 必须同一个值：cookie 先过期会表现成
        # "无故掉线"，票先过期会表现成"带着 cookie 但一直 401"，两种都难查
        ttl = _auth.ttl_s(cfg)
        response.set_cookie(
            _auth.COOKIE_NAME, _auth.issue(username, ttl),
            max_age=ttl, httponly=True, samesite="lax",
            # HttpOnly 挡住 JS 读取；SameSite=Lax 挡住跨站携带。
            # secure 跟随部署：本地 http 调试也要能登进去，线上由入口强制 HTTPS。
            secure=bool(cfg.raw.get("auth", {}).get("cookie_secure", False)),
            path="/",
        )

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        """当前身份与**生效边界**。

        把 tables / max_rows 一并给出去，是为了让人看得见角色到底收窄了什么 ——
        权限体系最怕的是"配了但看不出有没有生效"。
        """
        username = _current_user(request)
        scoped = _scoped(request)
        ident = _auth.identity_of(cfg, username) if username else ([], "")
        return {
            "enabled": _auth.enabled(cfg),
            "required": _auth.required(cfg),
            "username": username,
            # 角色与姓名一次取完：这个接口每次页面加载都会调，分两次查等于
            # 把对身份库的往返翻倍，而它俩要的是同一份名单。
            # 姓名走并集口径（配置 ∪ 身份库）—— 只查配置的话，身份库里登记的人
            # 顶栏永远显示网关用户名（guandezhi），而那不是要给人看的东西
            "display_name": ident[1],
            "roles": ident[0],
            # 数据期限与 tables / max_rows 同一个理由：配了要看得见生效。
            # 给的是**生效值**而不是策略值 —— 运行时源上时间窗口落不了地
            # （sources.derive_config 显式关闭），那里报一个天数就是在宣称
            # 一条并未执行的策略，正是这套界面要消灭的东西。
            "scope": {"tables": sorted(scoped.tables), "max_rows": scoped.max_rows,
                      "max_age_days": scoped.window_days if scoped.window_enforceable else None},
        }

    @app.post("/api/auth/login")
    def auth_login(req: LoginRequest, response: Response,
                   request: Request) -> dict[str, Any]:
        if not _auth.enabled(cfg):
            raise HTTPException(status_code=404, detail="本实例未启用登录")
        # 分桶键这里**只按来源地址**：用 _rl_key 会先看当前会话的登录名，
        # 而登录接口的调用方按定义还没有会话，等于所有匿名请求并回一个桶，
        # 又变回全局限流。爆破也正是从未登录状态发起的。
        if not _LOGIN_RL.allow(_login_rl_key(request)):
            raise HTTPException(status_code=429, detail="尝试过于频繁，稍后再试")
        try:
            acc = _auth.authenticate(cfg, req.username, req.password)
        except _auth.AuthError as e:
            # 账号不存在与口令不对同一句话、同一状态码 —— 区分就是账号枚举
            raise HTTPException(status_code=401, detail=str(e)) from e
        _set_session(response, acc.username)
        return {"ok": True, "username": acc.username, "roles": list(acc.roles)}

    @app.post("/api/auth/logout")
    def auth_logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(_auth.COOKIE_NAME, path="/")
        return {"ok": True}

    #: 「一张表都看不到」的统一措辞。
    #:
    #: 2026-09-06 之后**内置默认不会再让人落到这里**：角色不再收窄可见面，
    #: 系统管理员那份空表集也撤了。剩下两条路还能到达：实例白名单本身是空的，
    #: 或者部署方在 role_policies 里手工收窄成了空集。措辞因此改为指向配置，
    #: 而不是指向角色 —— 原来那句"系统管理员只管理成员，要查数需另行加入
    #: 数据角色"现在是假话。
    _NO_DATA_ROLE = ("当前没有任何可查的表。请检查实例的表白名单配置，"
                     "或联系系统管理员。")

    def _cfg_for(source: str, request: Request | None = None) -> Config:
        """按数据源 id 取配置。空 / "builtin" 走启动配置。

        顺序上先选源、再按角色收窄 —— 收窄只会去表不会加表，
        所以任何数据源都逃不过角色策略。反过来先收窄再换源，
        换源那一步会把收窄结果整个替掉，等于绕开权限。

        **这里曾经还判一层"角色能不能连这个环境的库"，2026-09-06 撤掉**
        （见 identity.Policy 的说明）：共享平台上大家用的是同一批数据源。
        """
        sid = (source or "").strip()
        if not sid or sid == "builtin":
            if cfg.has_default_source:
                return cfg
            # 没有内置源时落到部署方指定的那个（datasources.default）。
            # 没指定就仍然拒绝，**不挑一个源顶上** —— "碰巧排在第一个的库"
            # 与"部署方要的库"是两件事，猜错时结果照样出得来，只是答的是
            # 另一个库的数，比报错难发现得多。
            src, why = _default_source()
            if src is None:
                raise HTTPException(
                    status_code=400,
                    detail=why or "本实例未配置默认数据源，查询必须指定数据源。"
                                  "到「数据源」页选一个已添加的源再发起。",
                )
            return _derived(src)
        src = _sources.get_source(cfg, sid)
        if src is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return _derived(src)

    def _derived(src: "_sources.Source") -> Config:
        """一条注册表记录 → 可直接用的配置。白名单为空是拒绝，不是空结果集。"""
        if not src.tables:
            raise HTTPException(
                status_code=400,
                detail="该数据源还没有开放任何表。到「数据源」页勾选后再查 —— "
                       "白名单同时是安全边界与准确率边界。",
            )
        return _sources.derive_config(cfg, src)

    def _default_source() -> tuple["_sources.Source | None", str]:
        """部署方指定的默认运行时数据源，连同"为什么没取到"。

        返回 (None, "") 表示压根没指定 —— 那是合法配置（每次调用都显式带源），
        不是错，调用方照旧报"必须指定数据源"那句话。

        指定了却取不到时**返回原因而不是回落**：名字写错、源被删掉、注册表这会儿
        连不上，三种情况处置完全不同；随便挑一个源顶上会让站点看起来正常，
        而它答的是另一个库的数。
        """
        ref = cfg.default_source_ref
        if not ref:
            return None, ""
        try:
            items = _sources.list_sources(cfg)
        except Exception as e:
            return None, f"数据源注册表暂时读不出来，默认数据源「{ref}」取不到：{e}"
        hit = [s for s in items if s.id == ref] or [s for s in items if s.name == ref]
        if not hit:
            return None, (f"配置指定的默认数据源「{ref}」在注册表里不存在 —— "
                          f"到「数据源」页核对名字，或在调用时显式指定数据源。")
        if len(hit) > 1:
            return None, (f"配置指定的默认数据源「{ref}」对应 {len(hit)} 个源，"
                          f"名字不唯一 —— 请改用数据源 id。")
        return hit[0], ""

    def _cfg_of_record(rec: dict[str, Any], request: Request) -> Config:
        """一条审计记录**当初跑在哪个源上**，就按那个源的配置判可见性。

        用内置配置去判，是 2026-09-07 查出来的一个静默失效：执行追踪与复放
        都写着"判据用 tables_hit 与本人此刻可见表取子集"，而"此刻可见表"取的是
        启动配置的白名单。多数据源之后，任何跑在运行时源上的记录都不可能是
        它的子集 —— careermate 源上的记录（users / resume_versions）在
        ragforge-prod 实例上一律 404，成功的、被拦的，全都点不开右半屏。
        表现是"列得出来、点进去空白"，最容易被读成"这条没有链路"。

        判据本身不放宽：它守的是"表被移出白名单后，旧记录跟着看不见"，
        按记录自己的源来判，这一条照样成立。角色自 2026-09-06 起不与数据源
        绑定，所以这里不存在"换个源就能越权"的口子。

        源被删了、记录没记源、或元数据库这会儿连不上，一律退回内置配置 ——
        与改动前同一行为（那条记录看不到），不引入新的放行路径。
        这里连 StoreUnavailable 一起吞：读一条历史链路不该因为数据源注册表
        临时不可用而变成 503，那是**写**数据源时才需要报的错。
        """
        try:
            return _cfg_for(str(rec.get("source") or ""), request)
        except Exception:
            return cfg

    def _serve_cached_ask(cached: dict[str, Any], scoped: Config,
                          question: str, org: int) -> dict[str, Any]:
        """把一条命中的缓存包装成本次响应：换新 trace_id、标 cached、补审计。

        命中不调模型、不扣配额、不执行 SQL。审计仍写一条（一调用一条留痕），
        但明确标 cached、成本 0，与真正跑过模型的记录区分开。

        **节点链只留 cache 这一条**，首跑那串节点不再复制过来（2026-09-09）。
        原来是整份 deepcopy 之后在头上插一行"命中缓存"，于是这次调用在执行
        追踪页上长着 Schema 召回 / SQL 生成 2.6s / 只读执行 …… 一整条它根本
        没跑的链路，页面无从区分，读起来就是"说没调模型，却又调了"。

        而且那串复制来的节点带着首跑的耗时与 token，被当成本次发生的事在算：
        audit 的「模型调用成功率」按 MODEL_STEPS 节点计数，每命中一次就虚增
        一次从未发生的 generate_sql；_nodes_of 把它的 ms 与 token 累进节点视图，
        与记录级 tok=0 / cost=0 自相矛盾；observe 见到 step 上有 token 就发一条
        generation，把幻影模型调用一路推给观测后端。执行追踪的语义是"这次调用
        发生了什么"，摆进别次的 span，护栏审计的可信度就没了。

        首跑链路并没有丢：cached_from 指着原 trace_id，追踪页从这一行跳过去。
        原 id 取不到（旧格式缓存里没有）时留空串，那一行只是不可点，
        不影响其余。原记录可能已过保留期或不在调用者可见范围内 —— 那时跳过去
        是既有的"这条链路当前不可见"，与任何一条查不到的 trace 同一个说法。
        """
        import copy
        import uuid as _uuid

        from .trace import now_iso as _ni, write_audit as _wa

        out = copy.deepcopy(cached)
        tid = _uuid.uuid4().hex[:12]
        origin = str(cached.get("trace_id") or "")
        steps = [{"step": "cache", "ms": 0, "status": "hit",
                  "note": "命中应答缓存，未调用模型"}]
        # 计量与"怎么跑出来的"全部按本次调用重置；结果本身（rows / sql_final /
        # tables_hit / masked_columns）照旧沿用缓存，那才是要还给调用方的东西。
        out.update({"trace_id": tid, "cached": True, "cached_from": origin,
                    "steps": steps, "step_count": 1, "attempts": 0,
                    "multi_step": False, "converged_early": "",
                    "elapsed_ms": 0, "tok_in": 0, "tok_out": 0, "cost_cny": 0.0})
        _wa(scoped, {
            "trace_id": tid, "ts": _ni(), "kind": "ask", "cached": True,
            "cached_from": origin,
            "model": "cache", "org_id": org, "role": scoped.role,
            "user": scoped.user, "question": question,
            "source": scoped.source_id or "builtin",
            "source_name": scoped.source_name or scoped.path,
            "tables_hit": out.get("tables_hit") or [], "metrics_hit": [],
            "sql_raw": "", "sql_final": out.get("sql_final") or "",
            "rules_fired": [], "rejected_by": None, "attempts": 0,
            "explain_rows": out.get("explain_rows"), "step_count": len(steps),
            "multi_step": False, "converged_early": "",
            "rows_returned": out.get("row_count") or 0,
            "masked_columns": out.get("masked_columns") or [],
            "mask_degraded": bool(out.get("mask_degraded")),
            "elapsed_ms": 0, "tok_in": 0, "tok_out": 0, "cost_cny": 0.0,
            "steps": steps,
        })
        return out

    @app.post("/api/ask")
    def ask(req: AskRequest, request: Request) -> JSONResponse:
        # 按角色收窄后再进链路。护栏、执行器、Schema 召回全部从配置取值，
        # 所以收窄一次即全链路生效 —— 模型连不可见的表都召回不到。
        _require_login(request)
        # 「创建任务」要登录，普通提问不要。两者是同一条链路，区别在于任务是
        # 一条**有归属、可续跑**的线程：匿名建出来的线程只落在匿名那一档，
        # 发起人自己都找不回来，等于点完就丢。与其让它成功，不如在这里说清楚。
        if req.as_task and not _current_user(request):
            raise HTTPException(
                status_code=401,
                detail="创建任务需要登录。任务是一条有归属、可续跑的线程，"
                       "匿名建出来无人认领，之后也无法续跑。"
                       "未登录可以直接在查询页提问，结果一样。",
            )
        # 顺序要紧：_require_scope 先跑。只有系统角色的人可见表为空，
        # 那时该给的是"你没有数据角色"，而不是"你无权用这个功能"——
        # 后者会让他去找系统管理员，而他自己就是。
        # 选源传 request：环境归属校验依赖"选中了哪个源"（Q-05）。
        scoped = _scoped(request, _cfg_for(req.source, request))
        _require_scope(scoped)
        _require_cap(request, _identity.QUERY, "发起查询")
        scoped = _apply_waiver(scoped, request, aid=req.approval_id,
                               kind="ask", text=req.question)

        # ---------- 应答缓存：命中即"零模型、零配额、零执行" ----------
        # 只对普通提问缓存；任务（有归属、可续跑）与审批豁免（一次性）都跳过。
        # 缓存是优化不是护栏：qcache 把一切 Redis 异常吞成未命中，这里不会因它抛错。
        q_text = req.question.strip()
        eff_org = scoped.default_org if req.org_id is None else req.org_id
        cache = build_answer_cache(scoped)
        ckey: str | None = None
        if cache.enabled and not req.as_task and not req.approval_id and not scoped.scan_waiver:
            ckey = _cache_key(question=q_text, source_id=scoped.source_id,
                              org_id=eff_org, role=scoped.role)
            hit = cache.get(ckey)
            if hit is not None:
                return JSONResponse(_serve_cached_ask(hit, scoped, q_text, eff_org))

        r = run_ask(q_text, scoped, org_id=req.org_id)
        out = r.to_dict()
        if r.rejected_by == "R-11" and not scoped.scan_waiver:
            # 与直查同一条口径：超阈值挂起，不是终结。
            # 绑定的是**问题原文**，因为再问一次生成的 SQL 未必逐字相同。
            out.update(_open_approval(scoped, request, trace_id=r.trace_id, kind="ask",
                                      question=q_text,
                                      sql=r.sql_final or r.sql_raw,
                                      match_text=q_text,
                                      est_rows=getattr(r, "explain_rows", None)))
        if scoped.scan_waiver and r.ok:
            _approvals.consume(cfg, req.approval_id)
        # 只缓存"干净的成功"：ok 且无任何拦截、无挂起审批。失败/被拦/挂起都是
        # 有状态或一次性的，缓存它们即错误（详见 qcache 模块头注）。
        if (ckey is not None and r.ok and r.rejected_by is None
                and not out.get("approval_id")):
            cache.put(ckey, out, cache.ttl)
        return JSONResponse(out)

    @app.post("/api/sql")
    def sql(req: SqlRequest, request: Request) -> JSONResponse:
        """直查模式：跳过模型，只跑 护栏 → 干跑 → 执行。未配密钥时也能用。

        直查同样一调用一条审计：拦截也留痕。此前这条路径不落流水，
        审计页上"被 R-02 挡掉的删表尝试"根本不存在 —— 而那恰恰是
        最需要留底的记录。
        """
        import uuid as _uuid

        from .trace import now_iso, write_audit

        # 直查同样按角色收窄：它绕过模型，但**不绕过权限**
        _require_login(request)
        # 产品角色没有这一位：直查绕开业务口径层，而口径归口正是它的职责所在，
        # 给它一条绕开口径的通道会让「指标以谁为准」失去落点。
        # 这是职责收敛，不是安全考虑 —— 直查同样过全部护栏。
        scoped = _scoped(request, _cfg_for(req.source, request))
        _require_scope(scoped)          # 顺序同 /api/ask，理由见那里
        _require_cap(request, _identity.QUERY_SQL, "使用直查 SQL")
        scoped = _apply_waiver(scoped, request, aid=req.approval_id,
                               kind="sql", text=req.sql)
        org = scoped.default_org if req.org_id is None else req.org_id
        trace_id = _uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        steps: list[dict[str, Any]] = []

        def _audit(*, rejected_by: str | None, sql_final: str = "",
                   rules_fired: list[str] | None = None,
                   explain_rows: int | None = None, rows_returned: int = 0,
                   masked_columns: list[str] | None = None,
                   mask_degraded: bool = False,
                   truncated: bool = False) -> None:
            write_audit(scoped, {
                "trace_id": trace_id, "ts": now_iso(), "kind": "sql",
                "model": None,
                "org_id": org, "role": scoped.role, "user": scoped.user,
                "question": "（直查模式）",
                "source": scoped.source_id or "builtin",
                "source_name": scoped.source_name or scoped.path,
                "tables_hit": [], "metrics_hit": [],
                "sql_raw": req.sql, "sql_final": sql_final,
                "rules_fired": rules_fired or [], "rejected_by": rejected_by,
                "attempts": 1, "explain_rows": explain_rows,
                "step_count": 1, "multi_step": False, "converged_early": "",
                "rows_returned": rows_returned,
                # 与 ask 链路同一套字段：脱了哪几列必须进审计，
                # 否则事后无从证明某一次结果到底脱没脱
                "masked_columns": masked_columns or [],
                "mask_degraded": mask_degraded,
                # 直查同样会被 R-13 截断。ask 链路记了这一条而直查不记的话，
                # 追踪页上同一枚可信度角标在两种模式下判的就不是同一件事。
                "truncated": truncated,
                # recall_blind / scope_narrowed 在直查上**不存在**（没有召回、
                # 超阈值走审批而不是自行收窄），所以这里不写 False 顶上 ——
                # 缺字段与"判过且通过"是两件事，可信度那边按不适用处理。
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "tok_in": 0, "tok_out": 0, "cost_cny": 0.0, "steps": steps,
            })

        g = guard.check(req.sql, scoped, org_id=org, dialect=scoped.dialect)
        if not g.ok:
            steps.append({"step": "guard", "ms": 0, "status": "blocked",
                          "note": f"{g.rejected_by} {g.reason}"})
            _audit(rejected_by=g.rejected_by)
            return JSONResponse({
                "ok": False, "question": "（直查模式）", "sql_raw": req.sql,
                "rejected_by": g.rejected_by, "error": g.reason,
                "hint": "改完 SQL 再试；这是纯代码的 AST 判定，不消耗 token。",
                "steps": steps, "org_id": org, "trace_id": trace_id,
            })
        steps.append({"step": "guard", "ms": 0, "status": "ok",
                      "note": "；".join(g.rewrites) or "无需改写"})

        with Executor(scoped) as ex:
            ep = ex.explain(g.sql)
            if not ep.ok and not scoped.scan_waiver:
                steps.append({"step": "dry_run", "ms": 0, "status": "blocked", "note": ep.reason})
                _audit(rejected_by="R-11", sql_final=g.sql, rules_fired=g.rules_fired)
                # 挂起而不是终结：登记一条待审批，把单号回给发起人（P07）
                pending = _open_approval(scoped, request, trace_id=trace_id, kind="sql",
                                         question="（直查模式）", sql=g.sql,
                                         # 指纹绑用户提交的原文，不是改写后的
                                         match_text=req.sql, est_rows=ep.est_rows)
                return JSONResponse({
                    "ok": False, "question": "（直查模式）", "sql_raw": req.sql,
                    "sql_final": g.sql, "rejected_by": "R-11", "error": ep.reason,
                    "hint": "缩小时间范围或增加筛选条件把扫描量降下来；"
                            "确有必要跑全量时，这条已登记为待审批，"
                            "由系统管理员放行后可原样重跑一次。",
                    "rewrites": g.rewrites, "steps": steps, "org_id": org,
                    "trace_id": trace_id, **pending,
                })
            if not ep.ok:
                # 已获批准。审计里必须看得出这条是走审批过来的，
                # 否则阈值形同虚设 —— 事后没人能分辨"没超"和"超了但批了"。
                steps.append({"step": "dry_run", "ms": 0, "status": "ok",
                              "note": f"{ep.reason}（已获审批放行）"})
            steps.append({"step": "dry_run", "ms": 0, "status": "ok",
                          "note": f"预估扫描 {ep.est_rows:,} 行" if ep.est_rows else "计划无基数估计"})
            try:
                ex.set_org(org)
                res = ex.run(g.sql, limit_capped="R-09" in g.rules_fired)
            except DataSourceError as e:
                steps.append({"step": "execute", "ms": 0, "status": "failed", "note": str(e)})
                _audit(rejected_by="EXEC", sql_final=g.sql, rules_fired=g.rules_fired,
                       explain_rows=ep.est_rows)
                return JSONResponse({
                    "ok": False, "question": "（直查模式）", "sql_final": g.sql,
                    "rejected_by": "EXEC", "error": str(e), "hint": e.hint,
                    "rewrites": g.rewrites, "steps": steps, "org_id": org,
                    "trace_id": trace_id,
                })

        note = f"返回 {res.row_count} 行"
        if res.masked_columns:
            note += f"；已脱敏 {len(res.masked_columns)} 列（{'、'.join(res.masked_columns[:5])}）"
        if res.mask_degraded:
            note += "；SQL 解析不出投影来源，本次按整行从严脱敏"
        steps.append({"step": "execute", "ms": res.elapsed_ms, "status": "ok",
                      "note": note})
        _audit(rejected_by=None, sql_final=g.sql, rules_fired=g.rules_fired,
               explain_rows=ep.est_rows, rows_returned=res.row_count,
               masked_columns=list(res.masked_columns),
               mask_degraded=res.mask_degraded, truncated=res.truncated)
        if scoped.scan_waiver:
            # **执行成功之后**才作废。执行失败就烧掉一次审批的话，
            # 用户得为一次数据源抖动重新走一遍人工流程。
            _approvals.consume(cfg, req.approval_id)
        return JSONResponse({
            "ok": True, "question": "（直查模式）", "sql_raw": req.sql, "sql_final": g.sql,
            "rules_fired": g.rules_fired, "rewrites": g.rewrites,
            "columns": [str(c) for c in res.columns],
            # 与 /api/ask 走同一套值渲染：str(Decimal) 对高标度 numeric 会变成
            # 0E-20 这种科学计数法，看的人认不出那是 0
            "rows": [[jsonable(v) for v in r] for r in res.rows],
            "row_count": res.row_count, "truncated": res.truncated,
            "as_of": res.as_of, "explain_rows": ep.est_rows,
            # 直查与 /api/ask 同一套契约：脱敏了哪几列、判定有没有退化，
            # 两条路都要给。少了它，直查模式下那条"星号是系统加的"的提示
            # 永远不出现 —— 而直查恰恰是最容易一次拉出整表的那条路。
            "masked_columns": list(res.masked_columns),
            "mask_degraded": res.mask_degraded,
            "elapsed_ms": res.elapsed_ms, "attempts": 1, "org_id": org,
            "tok_in": 0, "tok_out": 0, "cost_cny": 0.0, "steps": steps,
            "trace_id": trace_id,
        })

    return app
