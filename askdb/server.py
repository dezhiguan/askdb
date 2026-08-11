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
from .graph import ask as run_ask

WEB = Path(__file__).resolve().parent / "web"


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
        return FileResponse(WEB / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """页面启动时调一次，决定要不要显示配置横幅。"""
        db_ok, db_msg, db_hint = True, "", ""
        try:
            with Executor(cfg) as ex:
                ex.connect()
            db_msg = str(cfg.db_path.name)
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
            "tenant": {"column": cfg.tenant_column, "org_id": cfg.default_org},
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

    @app.post("/api/ask")
    def ask(req: AskRequest) -> JSONResponse:
        r = run_ask(req.question.strip(), cfg, org_id=req.org_id)
        return JSONResponse(r.to_dict())

    @app.post("/api/sql")
    def sql(req: SqlRequest) -> JSONResponse:
        """直查模式：跳过模型，只跑 护栏 → 干跑 → 执行。未配密钥时也能用。"""
        org = cfg.default_org if req.org_id is None else req.org_id
        steps: list[dict[str, Any]] = []
        g = guard.check(req.sql, cfg, org_id=org)
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
            "rows": [[None if v is None else str(v) for v in r] for r in res.rows],
            "row_count": res.row_count, "truncated": res.truncated,
            "elapsed_ms": res.elapsed_ms, "attempts": 1, "org_id": org,
            "tok_in": 0, "tok_out": 0, "cost_cny": 0.0, "steps": steps,
        })

    return app
