"""FastAPI 接口与静态页面。

接口设计上有意让**失败也是结构化的**：护栏拦截不是 HTTP 500，
而是 200 + ok:false + rejected_by/hint，前端才能给出针对性的提示。
只有服务本身坏了（配置错误、数据源不可用）才用非 2xx。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import guard
from .config import Config, load
from .executor import DataSourceError, Executor
from .graph import ask as run_ask, jsonable

WEB = Path(__file__).resolve().parent / "web"


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


def create_app(config_path: str = "config/askdb.yaml") -> FastAPI:
    cfg: Config = load(config_path)
    app = FastAPI(title="askdb", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # 不缓存：页面会随配置与数据源变化，缓存住旧版本会让人误判为 bug
        return FileResponse(WEB / "index.html", headers={
            "Cache-Control": "no-store, must-revalidate",
            "Pragma": "no-cache",
        })

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
            "ok": db_ok and bool(cfg.api_key()),
            "datasource": {"ok": db_ok, "type": cfg.db_type, "detail": db_msg, "hint": db_hint},
            "llm": {
                "ok": bool(cfg.api_key()),
                "model": cfg.llm["model"],
                "env": cfg.llm["api_key_env"],
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
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

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

        tcol = cfg.tenant_column
        out = []
        for t in found:
            spec = cfg.tables.get(t["name"])
            described = sum(1 for c in spec.columns.values() if c.desc) if spec else 0
            total = len(spec.columns) if spec else t["cols"]
            out.append({
                **t,
                "allowed": spec is not None,
                "tenant_column": (spec.tenant_column if spec else (tcol if t["tenant"] else None)),
                "coverage": round(described / total * 100) if total else 0,
                "desc": spec.desc if spec else "",
            })
        return {"ok": True, "tables": out,
                "allowed_count": sum(1 for t in out if t["allowed"]), "total": len(out)}

    @app.get("/api/eval")
    def evaluation() -> dict[str, Any]:
        """已跑完的评测结果。

        没有结果文件时如实返回 available:false —— 页面据此显示"尚未运行"，
        而不是编一组数字出来。
        """
        root = cfg.root / "evals" / "results"
        # 优先用跑在**当前数据源**上的那套结果。此前这里只读样例库那套，
        # 于是把一组跑在合成库上的成绩摆在了连着生产库的界面旁边。
        prod = (root / "ragforge-blind.json", root / "ragforge-ablation.json", None)
        sample = (root / "blind.json", root / "ablation2.json", root / "ablation_F.json")
        blind_p, abl_p, fix_p = prod if prod[0].exists() or prod[1].exists() else sample
        if not blind_p.exists() and not abl_p.exists():
            return {"available": False}

        import json as _json

        def _read(p):
            if p is None:
                return None
            try:
                return _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None

        out: dict[str, Any] = {"available": True}

        # 成绩的出处，以及它是否就是当前连着的这个数据源。
        # 「这组数字算不算数」全看这两项，必须带到前端去。
        prov = ((_read(blind_p) or {}).get("provenance")
                or (next(iter((_read(abl_p) or {}).values()), {}) or {}).get("provenance")
                or {})
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
        out["shipped"] = "E"     # 当前默认配置对应的组（多步已按消融结论关闭）
        return out

    @app.post("/api/ask")
    def ask(req: AskRequest) -> JSONResponse:
        r = run_ask(req.question.strip(), cfg, org_id=req.org_id)
        return JSONResponse(r.to_dict())

    @app.post("/api/sql")
    def sql(req: SqlRequest) -> JSONResponse:
        """直查模式：跳过模型，只跑 护栏 → 干跑 → 执行。未配密钥时也能用。"""
        org = cfg.default_org if req.org_id is None else req.org_id
        steps: list[dict[str, Any]] = []
        g = guard.check(req.sql, cfg, org_id=org, dialect=cfg.dialect)
        if not g.ok:
            steps.append({"step": "guard", "ms": 0, "status": "blocked",
                          "note": f"{g.rejected_by} {g.reason}"})
            return JSONResponse({
                "ok": False, "question": "（直查模式）", "sql_raw": req.sql,
                "rejected_by": g.rejected_by, "error": g.reason,
                "hint": "改完 SQL 再试；这是纯代码的 AST 判定，不消耗 token。",
                "steps": steps, "org_id": org,
            })
        steps.append({"step": "guard", "ms": 0, "status": "ok",
                      "note": "；".join(g.rewrites) or "无需改写"})

        with Executor(cfg) as ex:
            ep = ex.explain(g.sql)
            if not ep.ok:
                steps.append({"step": "dry_run", "ms": 0, "status": "blocked", "note": ep.reason})
                return JSONResponse({
                    "ok": False, "question": "（直查模式）", "sql_raw": req.sql,
                    "sql_final": g.sql, "rejected_by": "R-11", "error": ep.reason,
                    "hint": "缩小时间范围或增加筛选条件，把扫描量降下来。",
                    "rewrites": g.rewrites, "steps": steps, "org_id": org,
                })
            steps.append({"step": "dry_run", "ms": 0, "status": "ok",
                          "note": f"预估扫描 {ep.est_rows:,} 行" if ep.est_rows else "计划无基数估计"})
            try:
                ex.set_org(org)
                res = ex.run(g.sql)
            except DataSourceError as e:
                steps.append({"step": "execute", "ms": 0, "status": "failed", "note": str(e)})
                return JSONResponse({
                    "ok": False, "question": "（直查模式）", "sql_final": g.sql,
                    "rejected_by": "EXEC", "error": str(e), "hint": e.hint,
                    "rewrites": g.rewrites, "steps": steps, "org_id": org,
                })

        steps.append({"step": "execute", "ms": res.elapsed_ms, "status": "ok",
                      "note": f"返回 {res.row_count} 行"})
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
        })

    return app
