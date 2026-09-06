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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import approvals as _approvals
from . import auth as _auth
from . import guard
from . import identity as _identity
from . import sources as _sources
from .config import Config, load
from .executor import DataSourceError, Executor
from .graph import ask as run_ask, jsonable, resume as run_resume
from .quota import build_quota
from .trace import now_iso as _now_iso, observability_status as _obs_status


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


def _same_source(a: str, b: str) -> bool:
    """两个数据源标识是否指同一个库。

    记录的是 `postgresql:ragforge@127.0.0.1:15432`，界面上是
    `postgresql:ragforge @ 127.0.0.1:15432` —— 只差空格，不能因此判为不同。
    """
    norm = lambda s: s.replace(" ", "").lower()
    return bool(a) and norm(a) == norm(b)


def _dsn_label(dsn: str, upstream: str = "") -> str:
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
    local = f"{parts.get('host', '?')}:{parts.get('port', '5432')}"
    if upstream and _same_endpoint(upstream, local, db):
        return f"{db} @ {upstream}"
    if upstream:
        return f"{db} @ {upstream}（经隧道 {local}）"
    return f"{db} @ {local}"


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


def create_app(config_path: str = "config/askdb.yaml") -> FastAPI:
    cfg: Config = load(config_path)
    app = FastAPI(title="askdb", docs_url="/api/docs", openapi_url="/api/openapi.json")

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
                # 两种状态要分开说。没配会话密钥时登录整体关闭，此时叫人"先登录"
                # 是让他去撞一扇根本打不开的门 —— 那种提示比不提示更浪费时间。
                # 这种实例仍有出路：管理员令牌不依赖会话密钥，运维照样进得来。
                if not _auth.session_available():
                    return JSONResponse(status_code=401, content={
                        "code": "login_unavailable",
                        "detail": "本实例未配置会话密钥（ASKDB_SESSION_SECRET），登录整体关闭，"
                                  "因此没有人能执行改动配置的操作。配置该环境变量后重启，"
                                  "或由运维携带管理员令牌调用。",
                    })
                return JSONResponse(status_code=401, content={
                    "code": "login_required",
                    "detail": "这是一个会改动配置的操作，需要登录后才能执行。"
                              "你当前未登录，只能只读查询。请先登录再试。",
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
        if not db_ok or not cfg.has_default_source:
            return {"ok": False, "reason": "数据源不可用，实证无从谈起",
                    "write_blocked": None, "default_tenant_has_rows": None}

        table = next(iter(sorted(cfg.tenant_tables())), None) or next(iter(cfg.tables), None)
        if table is None:
            return {"ok": False, "reason": "白名单里没有表",
                    "write_blocked": None, "default_tenant_has_rows": None}

        # 护栏：静态判定就够了，它拦的就是这一层（R-02 非只读语句）
        g = guard.check(f"DELETE FROM {table}", cfg,
                        org_id=cfg.default_org, dialect=cfg.dialect)
        write_blocked = (not g.ok) and g.rejected_by == "R-02"

        has_rows = None
        try:
            probe_sql = guard.check(f"SELECT 1 FROM {table}", cfg,
                                    org_id=cfg.default_org, dialect=cfg.dialect)
            if probe_sql.ok:
                with Executor(cfg) as ex:
                    # 走护栏改写后的那条：租户谓词与 LIMIT 都是它注入的，
                    # 绕过去验出来的"有数据"回答的是另一个问题
                    has_rows = bool(ex.run(probe_sql.sql).rows)
        except DataSourceError:
            has_rows = None

        return {
            "ok": bool(write_blocked and has_rows),
            "table": table,
            "org_id": cfg.default_org,
            "write_blocked": write_blocked,
            "default_tenant_has_rows": has_rows,
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
                          else _dsn_label(cfg.dsn, cfg.upstream))
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
            "observability": {
                "tracing": _obs_status(),
                "replay_api": bool(cfg.raw["observability"].get("replay_api", False)),
            },
        }
        if probe:
            if not _PROBE_RL.allow(_rl_key(request)):
                raise HTTPException(status_code=429, detail="实证接口限流，稍后再试")
            out["probe"] = _post_deploy_probe(db_ok)
        return out

    @app.get("/api/schema")
    def schema(request: Request) -> dict[str, Any]:
        """当前调用方**眼里的** schema。

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
        scoped = _scoped(request)
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
        return _quality(cfg.audit_log, days=days)

    @app.get("/api/metrics/check")
    def metrics_check(request: Request) -> dict[str, Any]:
        """逐条核对业务口径的**区分度**：按定义算 vs 凭直觉算，差多少。

        为什么这件事必须能自动跑：口径写错不报错、不越权，护栏 R-01～R-17
        一条都不会触发 —— 两种写法都语法正确、表在白名单里、租户谓词照样注入。
        它守的是护栏原理上守不到的那一层，那么它自己就必须有别的方式被检验。

        区分度为 0 的口径（两种写法结果相同）当前**检验不出模型有没有真的用它**，
        也不该拿来出评测题 —— 模型完全无视口径也能答对。这件事原来靠人工在
        配置注释里标注（"⚠ 当前退化：库中无 PENDING 文档"），现在按真实数据算。

        SQL 走**同一套护栏**再执行：口径的 SQL 自己都过不了护栏，本身就是要报的事。
        """
        _require_cap(request, _identity.GLOSSARY_READ, "校验业务口径")
        cfg = _scoped(request)
        out: list[dict[str, Any]] = []

        try:
            with Executor(cfg) as ex:
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
                    g = guard.check(sql, cfg, org_id=cfg.default_org, dialect=cfg.dialect)
                    if not g.ok:
                        row.update(status="blocked", detail=f"{g.rejected_by} {g.reason}")
                        out.append(row)
                        continue
                    try:
                        res = ex.run(g.sql)
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
    def selfcheck(request: Request) -> dict[str, Any]:
        _require_cap(request, _identity.SELFCHECK, "运行数据源自检")
        try:
            with Executor(cfg) as ex:
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
    def introspect(request: Request) -> dict[str, Any]:
        """列出数据源里全部的表，供接入向导第 2 步选表。

        白名单之外的表也要列出来 —— 用户得先看见，才谈得上决定开不开放。
        """
        _require_cap(request, _identity.INTROSPECT, "内省库结构")
        try:
            with Executor(cfg) as ex:
                found = ex.introspect()
        except DataSourceError as e:
            return {"ok": False, "error": str(e), "hint": e.hint, "tables": []}

        import re as _re

        tcol = cfg.tenant_column
        out = []
        for t in found:
            spec = cfg.tables.get(t["name"])
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
        """
        if request is None:
            return "-"
        user = _current_user(request)
        if user:
            return f"u:{user}"
        return f"ip:{request.client.host if request.client else '-'}"

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
            "host": _dsn_label(cfg.dsn, cfg.upstream) if cfg.db_type != "duckdb"
                    else cfg.db_path.name,
            "credential": cfg.raw["datasource"].get("password_env") or "",
            "created_at": "",
            "table_count": len(cfg.tables),
            "builtin": True,
            # 删得动才给按钮：删除会改配置文件，同样受 allow_runtime_add 约束；
            # 且必须先有别的源接手，否则删完这台实例查不了任何东西。
            "deletable": _sources.enabled(cfg) and bool(_sources.list_sources(cfg)),
        }

    @app.get("/api/sources")
    def sources_list(request: Request) -> dict[str, Any]:
        """列表恒可读；能不能新增由 can_add 告诉前端，而不是让它点了才知道。"""
        _require_cap(request, _identity.SOURCES_READ, "查看数据源")
        return {
            "can_add": _sources.enabled(cfg),
            "supported_types": list(_sources.SUPPORTED_TYPES),
            # 主密钥没配就只能用环境变量名那条路，前端据此决定表单里的默认项
            "can_store_password": bool(os.environ.get("ASKDB_SECRET_KEY", "").strip()),
            "items": _visible_sources(request),
        }

    def _visible_sources(request: Request) -> list[dict[str, Any]]:
        """列表按角色的环境档位过滤。

        列表本身就是信息：不该让测试角色知道生产只读镜像的存在与连接目标
        （设计文档脚注 7）。过滤放在这里而不是前端 —— 只灰按钮的话，
        curl 一下照样把主机名和库名全拿到。
        """
        builtin = _builtin_card()
        cards = ([builtin] if builtin else []) + [
            _sources.to_public(s) for s in _sources.list_sources(cfg)]
        out = []
        for card in cards:
            env = str(card.get("env") or "test")
            # 内置源那张卡的 env 是哨兵 "builtin"（前端据此显示 READ-ONLY
            # 而不是环境名），判定时要翻回它真正的档位，否则它会被过滤掉 ——
            # 那是"权限把功能测没了"的典型。
            if env == "builtin":
                env = _env_of(None)
            if not env or _env_visible(request, env):
                out.append(card)
        return out

    def _env_visible(request: Request, env: str) -> bool:
        """列表过滤用的软判定。与 _require_env 是同一个策略，只是不抛异常。

        两处必须同源：能列出来却连不上，或者连得上却列不出来，
        都会让人以为是 bug 而不是权限 —— 而排查权限问题最耗时的
        恰恰是"看起来像坏了"的那种表现。
        """
        policy = _identity.combine(
            [_identity.policy_for(cfg, c) for c in _roles(request)])
        return policy.envs is None or env in policy.envs

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
        src = _sources.get_source(cfg, sid)
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
            # 删的是配置文件里的那一段，不是 var/sources 下的记录 ——
            # 走同一个开关：能在页面上加源的实例，才谈得上在页面上删源。
            if not cfg.has_default_source:
                raise HTTPException(status_code=404, detail="本实例没有默认数据源")
            if not _sources.list_sources(cfg):
                raise HTTPException(
                    status_code=400,
                    detail="删除默认数据源前，至少要先添加一个可用的数据源 —— "
                           "一个源都不剩的实例查不了任何东西。",
                )
            _sources.drop_default_source(cfg)
            return JSONResponse({"ok": True})
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

        out: dict[str, Any] = {"available": True}

        # 成绩的出处，以及它是否就是当前连着的这个数据源。
        # 「这组数字算不算数」全看这两项，必须带到前端去。
        prov = _first_provenance(_read(blind_p)) or _first_provenance(_read(abl_p)) or {}
        here = (f"{cfg.db_type}:"
                + (cfg.db_path.name if cfg.db_type == "duckdb"
                   else _dsn_brief_id(cfg)))
        out["provenance"] = {
            **prov,
            "current_datasource": here,
            # 出处缺失时不敢断言"一致"——按不一致处理，宁可多提示一次
            "matches_current": bool(prov) and _same_source(prov.get("datasource", ""), here),
        }
        if (b := _read(blind_p)):
            out["blind"] = {k: b.get(k) for k in
                            ("n", "accuracy", "false_reject", "block_rate",
                             "multi_misuse", "p95_ms", "cost_cny", "failure_kinds")}
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
        bd = _read(blind_p) or {}
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
                        "question": c.get("question", ""),
                        "in_blind": bool(c.get("blind")),
                        # 期望：应拒用例看规则，其余看列与行数约束
                        "expect": (f"应被 {c['expect_rule']} 拦下" if c.get("expect_rule")
                                   else "、".join(c.get("expect_cols") or []) or c.get("note", "")),
                        # 本轮没跑到就是 null，不是"通过"
                        "passed": (None if o is None else bool(o.get("passed"))),
                        "reason": (o or {}).get("reason", ""),
                        "trace_id": (o or {}).get("trace_id", ""),
                    })
                out["cases"] = cases

        # 发布门禁评分。
        #
        # 四个维度**全部由真实结果算**，但**权重与目标值是项目策略、不是测量值** ——
        # 这一点必须在接口层就说清楚，页面照抄显示。发布门禁本来就是有人拍板
        # "多少分算过"，把它伪装成客观测量，才是这一页最容易骗人的地方。
        if b := _read(blind_p):
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
            out["score"] = {
                "overall": overall,
                "gate": _RELEASE_GATE,
                "pass": overall >= _RELEASE_GATE,
                "dimensions": dims,
                # 说清这组权重的性质，页面必须原样展示
                "policy_note": "权重与目标值是本项目设定的发布策略，不是测量结果。",
            }

        # 复现必须用同一份配置：检查点库跟着配置走
        out["replay_config"] = (bd.get("provenance") or {}).get("config", "")
        out["shipped"] = "E"     # 当前默认配置对应的组（多步已按消融结论关闭）
        return out

    @app.get("/api/audit")
    def audit_list(request: Request, page: int = 1, page_size: int = 10,
                   q: str = "", kind: str = "") -> dict[str, Any]:
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
        from .audit import list_audits

        return list_audits(cfg.audit_log, page=page, page_size=page_size,
                           q=q.strip(), kind=kind.strip(),
                           with_text=_can(request, _identity.AUDIT_CONTENT),
                           only_user=_audit_owner_filter(request))

    @app.get("/api/audit/stats")
    def audit_stats(request: Request, days: int = 30) -> dict[str, Any]:
        """时间窗统计：调用/拦截率/成本/按日序列。

        replay_api 开关状态一并带出 —— 前端据此决定"复放"入口
        显示还是置灰，而不是点了才发现 404。
        """
        _require_cap(request, _identity.AUDIT_READ, "查看审计统计")
        from .audit import stats as _stats

        days = min(max(int(days), 1), 365)
        return {
            **_stats(cfg.audit_log, days=days,
                     only_user=_audit_owner_filter(request)),
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

        rec = get_audit(cfg.audit_log, trace_id)
        if rec is None:
            return not_found

        scoped = _scoped(request)
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

        rec = get_audit(cfg.audit_log, trace_id)
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
        scoped = _scoped(request)
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
    def identity_members(request: Request, role: str = "") -> dict[str, Any]:
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
        try:
            return {"items": _identity.list_members(cfg, role.strip())}
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"身份库不可用：{e}") from e

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
        「提出与放行分属两人」失效。而系统管理员的 Policy 是空表集，
        永远不可能是发起人，所以自批在结构上不可能发生。
        """
        _require_login(request)
        _require_cap(request, _identity.APPROVE, "审批高成本查询")
        rec = _approvals.decide(cfg, approval_id,
                                approver=_current_user(request) or "",
                                approved=bool(req.approved), note=req.note)
        if rec is None:
            # 不存在与"已经批过"合并成同一句：重复决策不是错误，
            # 但也不该悄悄覆盖前一个人的结论。
            raise HTTPException(status_code=409,
                                detail="该申请不存在，或已经有过结论，不能重复决策。")
        return rec

    @app.get("/api/tasks")
    def tasks(request: Request) -> dict[str, Any]:
        """当前账号名下的**全部执行线程**，新的在前。

        列全部而不是只列中断的：中断只在异常逃出执行图时才发生（进程故障、
        递归超限、检查点库异常），是故障态不是常规流程 —— 只列中断等于这一页
        正常情况下永远是空的。可续跑的那些由 resumable 字段标出来，
        续跑入口只对它们开放。

        **按发起人收窄**，登录与匿名同一条规则：匿名看到的是匿名发起的线程，
        看不到任何登录用户的。收窄没有被放松 —— 放松的只是"匿名有没有资格
        看自己那一档"。归属口径与 /api/resume 完全一致（有主的只有主人能续跑），
        所以不会出现"列得出来、续不了"。
        """
        username = _current_user(request) or ""
        from .audit import tasks as _tasks
        from .graph import is_resumable

        items = _tasks(cfg.audit_log, username)
        # 审计只知道这条线程上次以 INTERRUPTED 收尾，不知道现场有没有真的
        # 落盘、也不知道后来是不是已被续跑跑完 —— 只按审计标 resumable，
        # 会出现"这里说能续、点下去 404"。以检查点为准再核一遍。
        for it in items:
            if it.get("resumable"):
                state = is_resumable(str(it.get("thread_id") or ""), cfg)
                if state is not None:
                    it["resumable"] = state
        return {"items": items, "user": username}   # 匿名时为空串，页面据此显示「匿名」

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
        from .audit import read_records

        owner = ""
        origin_source = ""
        for rec in read_records(cfg.audit_log):
            if (rec.get("thread_id") or rec.get("trace_id")) == req.thread_id:
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
            "scope": {"tables": sorted(scoped.tables), "max_rows": scoped.max_rows},
        }

    @app.post("/api/auth/login")
    def auth_login(req: LoginRequest, response: Response) -> dict[str, Any]:
        if not _auth.enabled(cfg):
            raise HTTPException(status_code=404, detail="本实例未启用登录")
        if not _LOGIN_RL.allow():
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

    #: 「完全没有数据权限」的统一措辞。环境判定与表白名单判定都可能先撞上
    #: 这个状态，两处各写一句就会出现"同一个原因两种说法" —— 而排查权限问题时
    #: 最耗时的恰恰是分不清撞的是哪道门。
    _NO_DATA_ROLE = ("当前角色没有数据访问权限。系统管理员只管理成员，"
                     "要查数需另行加入某个数据角色。")

    def _env_of(src: "_sources.Source | None") -> str:
        """数据源的环境档位。内置源未声明时返回空串 = **不参与环境判定**。

        运行时源在 sources.build() 里必然有 env（非法值回退 test），所以
        "未声明"只可能出现在配置文件里那个内置源上。

        为什么不给它猜一个默认值：猜 test 会把产品角色在所有现有部署上
        直接锁死（它只能连 prod_ro），猜 prod_ro 则会把开发与测试角色锁死。
        两种猜法都是拿一个我们并不知道的事实去拦人。

        更要紧的是诚实：对着一个没声明归属的库宣称"已按环境鉴权"，
        正是这次要消灭的那类"看起来在拦、其实没拦"。声明 datasource.env
        才是打开这一层的开关 —— 收窄是部署方的显式决定，与 role_policies 同理。
        """
        if src is not None:
            return src.env
        return str((cfg.raw.get("datasource") or {}).get("env") or "").strip()

    def _require_env(request: Request, src: "_sources.Source | None") -> None:
        """角色能不能连这个环境的库（设计文档 Q-05 / D-1）。

        **这一层必须在选源时判，不能交给护栏。** 护栏只看 SQL 文本，
        它没有"这条连接通向哪台机器"这个信息 —— 表白名单拦得住"查哪张表"，
        拦不住"查哪个库的同名表"。生产只读镜像与测试库的表结构往往一模一样，
        那正是这个洞最危险的地方：SQL 一字不差，数据完全不同。
        """
        policy = _identity.combine(
            [_identity.policy_for(cfg, c) for c in _roles(request)])
        if policy.envs is None:                      # 角色不额外收窄
            return
        env = _env_of(src)
        if not env:                                  # 数据源未声明归属，见 _env_of
            return
        if env in policy.envs:
            return
        # 一个档位都没有 = 压根没有数据权限，那是"你没有数据角色"而不是
        # "你够不着这个环境"。后者会让系统管理员去找系统管理员。
        if not policy.envs:
            raise HTTPException(status_code=403, detail=_NO_DATA_ROLE)
        names = "、".join(
            _identity.ROLE_BY_CODE[c].name for c in _roles(request)
            if c in _identity.ROLE_BY_CODE) or "未登录"
        raise HTTPException(
            status_code=403,
            detail=f"当前角色（{names}）不能访问 "
                   f"{_sources.ENV_LABEL.get(env, env)} 环境的数据源。",
        )

    def _cfg_for(source: str, request: Request | None = None) -> Config:
        """按数据源 id 取配置。空 / "builtin" 走启动配置。

        顺序上先选源、再按角色收窄 —— 收窄只会去表不会加表，
        所以任何数据源都逃不过角色策略。反过来先收窄再换源，
        换源那一步会把收窄结果整个替掉，等于绕开权限。

        环境归属的校验也在这里：它依赖"选中了哪个源"，所以只能在选完之后判，
        而且必须在返回之前判 —— 返回了就等于这条连接已经交出去了。
        """
        sid = (source or "").strip()
        if not sid or sid == "builtin":
            if not cfg.has_default_source:
                raise HTTPException(
                    status_code=400,
                    detail="本实例未配置默认数据源，查询必须指定数据源。"
                           "到「数据源」页选一个已添加的源再发起。",
                )
            if request is not None:
                _require_env(request, None)
            return cfg
        src = _sources.get_source(cfg, sid)
        if src is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        if request is not None:
            _require_env(request, src)
        if not src.tables:
            raise HTTPException(
                status_code=400,
                detail="该数据源还没有开放任何表。到「数据源」页勾选后再查 —— "
                       "白名单同时是安全边界与准确率边界。",
            )
        return _sources.derive_config(cfg, src)

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
        r = run_ask(req.question.strip(), scoped, org_id=req.org_id)
        out = r.to_dict()
        if r.rejected_by == "R-11" and not scoped.scan_waiver:
            # 与直查同一条口径：超阈值挂起，不是终结。
            # 绑定的是**问题原文**，因为再问一次生成的 SQL 未必逐字相同。
            out.update(_open_approval(scoped, request, trace_id=r.trace_id, kind="ask",
                                      question=req.question.strip(),
                                      sql=r.sql_final or r.sql_raw,
                                      match_text=req.question.strip(),
                                      est_rows=getattr(r, "explain_rows", None)))
        if scoped.scan_waiver and r.ok:
            _approvals.consume(cfg, req.approval_id)
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
                   explain_rows: int | None = None, rows_returned: int = 0) -> None:
            write_audit(scoped.audit_log, {
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
                res = ex.run(g.sql)
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

        steps.append({"step": "execute", "ms": res.elapsed_ms, "status": "ok",
                      "note": f"返回 {res.row_count} 行"})
        _audit(rejected_by=None, sql_final=g.sql, rules_fired=g.rules_fired,
               explain_rows=ep.est_rows, rows_returned=res.row_count)
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
            "elapsed_ms": res.elapsed_ms, "attempts": 1, "org_id": org,
            "tok_in": 0, "tok_out": 0, "cost_cny": 0.0, "steps": steps,
            "trace_id": trace_id,
        })

    return app
