"""FastAPI 接口与静态页面。

接口设计上有意让**失败也是结构化的**：护栏拦截不是 HTTP 500，
而是 200 + ok:false + rejected_by/hint，前端才能给出针对性的提示。
只有服务本身坏了（配置错误、数据源不可用）才用非 2xx。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import guard
from . import sources as _sources
from .config import Config, load
from .executor import DataSourceError, Executor
from .graph import ask as run_ask, jsonable, resume as run_resume
from .quota import build_quota
from .trace import observability_status as _obs_status


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

WEB = Path(__file__).resolve().parent / "web"
# 换壳前的单文件页面。它仍然是唯一一处接了真实数据的界面 ——
# 新前端把后端能力接回来之前，不能只剩一个查不了数的壳，所以留在 /legacy。
WEB_LEGACY = Path(__file__).resolve().parent / "web_legacy"

# 回放 id 严格校验：12 位十六进制，命中与未命中同为 404
_TRACE_ID_RE = __import__("re").compile(r"[0-9a-f]{12}")


class _RateLimit:
    """回放接口的进程内固定窗口限流。

    单独限流而不是复用全局配额：回放不花 token，但每次都要开 SQLite
    遍历检查点历史 —— 防的是把它当查询接口刷（设计说明 §5.1）。
    """

    def __init__(self, limit: int = 30, window_s: int = 60) -> None:
        self.limit, self.window_s = limit, window_s
        self._hits: list[float] = []

    def allow(self) -> bool:
        import time as _t

        now = _t.monotonic()
        self._hits = [t for t in self._hits if now - t < self.window_s]
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True


_REPLAY_RL = _RateLimit()
# 新增/测试数据源会让服务端主动向外建连。开关之外再加一道限流 ——
# 开关决定「能不能」，限流决定「能多快」，被拿去当端口扫描器的正是后者。
_SOURCE_RL = _RateLimit(limit=10, window_s=60)


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
    """
    parts = dict(
        kv.split("=", 1) for kv in dsn.split() if "=" in kv and not kv.startswith("password=")
    )
    db = parts.get("dbname", "?")
    local = f"{parts.get('host', '?')}:{parts.get('port', '5432')}"
    if upstream:
        return f"{db} @ {upstream}（经隧道 {local}）"
    return f"{db} @ {local}"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    org_id: int | None = None


class SqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    org_id: int | None = None


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

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """页面启动时调一次，决定要不要显示配置横幅。"""
        db_ok, db_msg, db_hint = True, "", ""
        try:
            with Executor(cfg) as ex:
                ex.connect()
            db_msg = (cfg.db_path.name if cfg.db_type == "duckdb"
                      else _dsn_label(cfg.dsn, cfg.upstream))
        except DataSourceError as e:
            db_ok, db_msg, db_hint = False, str(e), e.hint
        return {
            # 有意不接模型的实例（对外开放配置），没配密钥是**预期状态**，
            # 不能算不健康 —— 否则 health 顶层恒报 false，看的人以为服务坏了。
            "ok": db_ok and (bool(cfg.api_key()) or bool(cfg.llm.get("disabled"))),
            # 复现命令要带 -c：检查点库跟着配置走，配置不对就找不到 trace
            "config": cfg.path,
            "datasource": {
                "ok": db_ok, "type": cfg.db_type, "detail": db_msg, "hint": db_hint,
                # 口令来自哪个环境变量 —— 只给变量名，不给值。
                # 界面要在数据源卡上交代凭证来源；写死一个 "VAULT" 是假的，
                # 而 askdb 的真实答案就是"环境变量"或"这个库不需要口令"。
                "credential": cfg.raw["datasource"].get("password_env") or "",
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

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
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
                for t in cfg.tables.values()
            ],
            "metrics": [
                {"name": m.name, "aliases": m.aliases, "scope": m.scope,
                 "definition": m.expr or m.predicate or "", "note": m.note}
                for m in cfg.metrics
            ],
        }

    @app.get("/api/selfcheck")
    def selfcheck() -> dict[str, Any]:
        with Executor(cfg) as ex:
            checks = ex.self_check()
        latency = next((c["ms"] for c in checks if "ms" in c), None)
        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "latency_ms": latency}

    @app.get("/api/introspect")
    def introspect() -> dict[str, Any]:
        """列出数据源里全部的表，供接入向导第 2 步选表。

        白名单之外的表也要列出来 —— 用户得先看见，才谈得上决定开不开放。
        """
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

    def _sources_gate() -> None:
        """写操作的准入。开关关闭时给 403 并说清原因 —— 这不是秘密，
        界面需要照实解释为什么按钮是灰的（与 /api/replay 的 404 语义不同：
        那里要防的是「记录是否存在」这一位信息泄露，这里没有这个问题）。"""
        if not _sources.enabled(cfg):
            raise HTTPException(
                status_code=403,
                detail="本实例未开启运行时添加数据源（datasources.allow_runtime_add）。"
                       "服务端会按填入的地址主动建连，而 askdb 不设账号体系，"
                       "所以对外实例一律关闭。",
            )
        if not _SOURCE_RL.allow():
            raise HTTPException(status_code=429, detail="操作过于频繁，稍后再试")

    def _builtin_card() -> dict[str, Any]:
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
        }

    @app.get("/api/sources")
    def sources_list() -> dict[str, Any]:
        """列表恒可读；能不能新增由 can_add 告诉前端，而不是让它点了才知道。"""
        return {
            "can_add": _sources.enabled(cfg),
            "supported_types": list(_sources.SUPPORTED_TYPES),
            # 主密钥没配就只能用环境变量名那条路，前端据此决定表单里的默认项
            "can_store_password": bool(os.environ.get("ASKDB_SECRET_KEY", "").strip()),
            "items": [_builtin_card()] + [_sources.to_public(s) for s in _sources.list_sources(cfg)],
        }

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
            "tables": tables,
        }

    @app.post("/api/sources/test")
    def sources_test(req: SourceRequest) -> JSONResponse:
        """只连不存。表单上的「测试连接」。"""
        _sources_gate()
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
                                 "checks": [], "latency_ms": None, "tables": []})

    @app.post("/api/sources")
    def sources_create(req: SourceRequest) -> JSONResponse:
        """保存并扫描元数据。

        **扫描出来的表一张都不开放。** 扫描只解决「看得见」，开放与否是单独
        一步（PUT /tables）—— 白名单同时是安全边界与准确率边界，默认全开
        等于把两条边界一起取消。
        """
        _sources_gate()
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
        return JSONResponse({"source": _sources.to_public(src), **probe}, status_code=201)

    @app.get("/api/sources/{sid}/scan")
    def sources_scan(sid: str) -> JSONResponse:
        """重新扫描：列出全部表，并标出哪些已在白名单里。"""
        _sources_gate()
        src = _sources.get_source(cfg, sid)
        if src is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        allowed = {t["name"] for t in src.tables}
        try:
            probe = _probe(src)
        except DataSourceError as e:
            raise HTTPException(status_code=400, detail=f"{e}｜{e.hint}") from e
        for t in probe["tables"]:
            t["allowed"] = t["name"] in allowed
        return JSONResponse(probe)

    @app.put("/api/sources/{sid}/tables")
    def sources_set_tables(sid: str, req: SourceTablesRequest) -> JSONResponse:
        """设置白名单。字段名与类型在这里落库 —— R-04 与 R-05 靠它判定。"""
        _sources_gate()
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
    def sources_delete(sid: str) -> JSONResponse:
        _sources_gate()
        try:
            ok = _sources.delete_source(cfg, sid)
        except _sources.SourceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not ok:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return JSONResponse({"ok": True})

    @app.get("/api/eval")
    def evaluation() -> dict[str, Any]:
        """已跑完的评测结果。

        没有结果文件时如实返回 available:false —— 页面据此显示"尚未运行"，
        而不是编一组数字出来。
        """
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
        # 复现必须用同一份配置：检查点库跟着配置走
        out["replay_config"] = (bd.get("provenance") or {}).get("config", "")
        out["shipped"] = "E"     # 当前默认配置对应的组（多步已按消融结论关闭）
        return out

    @app.get("/api/audit")
    def audit_list(page: int = 1, page_size: int = 10,
                   q: str = "", kind: str = "") -> dict[str, Any]:
        """审计流水（摘要分页）。列表有意不含 SQL 文本与结果行 ——
        细节只经 /api/replay 的白名单+开关出去。"""
        from .audit import list_audits

        return list_audits(cfg.audit_log, page=page, page_size=page_size,
                           q=q.strip(), kind=kind.strip())

    @app.get("/api/audit/stats")
    def audit_stats(days: int = 30) -> dict[str, Any]:
        """时间窗统计：调用/拦截率/成本/按日序列。

        replay_api 开关状态一并带出 —— 前端据此决定"复放"入口
        显示还是置灰，而不是点了才发现 404。
        """
        from .audit import stats as _stats

        days = min(max(int(days), 1), 365)
        return {
            **_stats(cfg.audit_log, days=days),
            "replay_api": bool(cfg.raw["observability"].get("replay_api", False)),
            "tracing": _obs_status(),
        }

    @app.get("/api/replay")
    def replay_trace(trace_id: str = "") -> JSONResponse:
        """判定链路回放（设计说明 V1.1）。

        三条硬规则，都是为了"接口本身在任何实例上都不泄露数据"：
        - 字段白名单（audit.REPLAY_FIELDS）：rows / schema_prompt 永不出接口；
        - 开关关闭、id 非法、id 不存在 **同为 404**，不区分"不存在"与
          "存在但无权"——区分本身就是信息泄露；
        - 独立限流：每次回放都要开 SQLite 遍历历史，不能被当查询接口刷。
        """
        not_found = JSONResponse({"error": "not found"}, status_code=404)
        if not cfg.raw["observability"].get("replay_api", False):
            return not_found
        if not _REPLAY_RL.allow():
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if not _TRACE_ID_RE.fullmatch(trace_id or ""):
            return not_found

        from .audit import REPLAY_FIELDS, get_audit

        rec = get_audit(cfg.audit_log, trace_id)
        if rec is None:
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

    @app.post("/api/resume")
    def resume_task(req: ResumeRequest) -> JSONResponse:
        """从断点续跑一次中断的提问（中断恢复设计 V1.1）。

        只接受调用方自己持有的 thread_id；格式非法、不存在、已跑完
        一律 404 且响应一致 —— 不提供未完成任务的枚举入口（§4.2）。
        入口层限流应与 /api/ask 同档（见 deploy/nginx-askdb.conf）。
        """
        not_found = JSONResponse({"error": "not found"}, status_code=404)
        if not _TRACE_ID_RE.fullmatch(req.thread_id or ""):
            return not_found
        r = run_resume(req.thread_id, cfg)
        if r is None:
            return not_found
        return JSONResponse(r.to_dict())

    @app.post("/api/ask")
    def ask(req: AskRequest) -> JSONResponse:
        r = run_ask(req.question.strip(), cfg, org_id=req.org_id)
        return JSONResponse(r.to_dict())

    @app.post("/api/sql")
    def sql(req: SqlRequest) -> JSONResponse:
        """直查模式：跳过模型，只跑 护栏 → 干跑 → 执行。未配密钥时也能用。

        直查同样一调用一条审计：拦截也留痕。此前这条路径不落流水，
        审计页上"被 R-02 挡掉的删表尝试"根本不存在 —— 而那恰恰是
        最需要留底的记录。
        """
        import uuid as _uuid

        from .trace import now_iso, write_audit

        org = cfg.default_org if req.org_id is None else req.org_id
        trace_id = _uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        steps: list[dict[str, Any]] = []

        def _audit(*, rejected_by: str | None, sql_final: str = "",
                   rules_fired: list[str] | None = None,
                   explain_rows: int | None = None, rows_returned: int = 0) -> None:
            write_audit(cfg.audit_log, {
                "trace_id": trace_id, "ts": now_iso(), "kind": "sql",
                "model": None,
                "org_id": org, "question": "（直查模式）",
                "tables_hit": [], "metrics_hit": [],
                "sql_raw": req.sql, "sql_final": sql_final,
                "rules_fired": rules_fired or [], "rejected_by": rejected_by,
                "attempts": 1, "explain_rows": explain_rows,
                "step_count": 1, "multi_step": False, "converged_early": "",
                "rows_returned": rows_returned,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "tok_in": 0, "tok_out": 0, "cost_cny": 0.0, "steps": steps,
            })

        g = guard.check(req.sql, cfg, org_id=org, dialect=cfg.dialect)
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

        with Executor(cfg) as ex:
            ep = ex.explain(g.sql)
            if not ep.ok:
                steps.append({"step": "dry_run", "ms": 0, "status": "blocked", "note": ep.reason})
                _audit(rejected_by="R-11", sql_final=g.sql, rules_fired=g.rules_fired)
                return JSONResponse({
                    "ok": False, "question": "（直查模式）", "sql_raw": req.sql,
                    "sql_final": g.sql, "rejected_by": "R-11", "error": ep.reason,
                    "hint": "缩小时间范围或增加筛选条件，把扫描量降下来。",
                    "rewrites": g.rewrites, "steps": steps, "org_id": org,
                    "trace_id": trace_id,
                })
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
        return JSONResponse({
            "ok": True, "question": "（直查模式）", "sql_raw": req.sql, "sql_final": g.sql,
            "rules_fired": g.rules_fired, "rewrites": g.rewrites,
            "columns": [str(c) for c in res.columns],
            # 与 /api/ask 走同一套值渲染：str(Decimal) 对高标度 numeric 会变成
            # 0E-20 这种科学计数法，看的人认不出那是 0
            "rows": [[jsonable(v) for v in r] for r in res.rows],
            "row_count": res.row_count, "truncated": res.truncated,
            "as_of": res.as_of,
            "elapsed_ms": res.elapsed_ms, "attempts": 1, "org_id": org,
            "tok_in": 0, "tok_out": 0, "cost_cny": 0.0, "steps": steps,
            "trace_id": trace_id,
        })

    return app
