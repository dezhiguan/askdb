"""MCP 服务端 —— 把 askdb 暴露成任何 Agent 都能调用的工具。

按 2026-07-28 规范实现：**无状态请求/响应**。每次调用自描述，
不依赖会话粘性，因此可以直接挂在普通轮询负载均衡后面。

暴露三个工具，边界与 HTTP 接口完全一致：
  ask     自然语言问数据（走完整链路，含护栏与租户注入）
  run_sql 直查模式：跳过模型，只验证护栏与执行
  schema  查看开放的表、字段语义与业务口径

**护栏不因调用方是 Agent 而放松。** 恰恰相反：Agent 自动发起的查询
没有人盯着，租户谓词、行数上限、扫描阈值、每日配额全部照常生效。

用法（stdio，供 Claude Code / Cursor 等接入）：
  python -m askdb.mcp_server --config config/askdb.yaml
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import guard, identity
from .config import Config, load
from .executor import DataSourceError, Executor
from .graph import ask as run_ask, jsonable
from .quota import build_quota

TOOLS_DESC = {
    "ask": (
        "用自然语言查询数据库。返回结果集、实际执行的 SQL、以及每一步的判定链路。\n"
        "输出必须与 SQL 一并呈现给用户 —— 这是唯一的正确性兜底。"
    ),
    "run_sql": (
        "直接执行一条 SELECT，跳过模型生成，只走护栏与执行。\n"
        "适合你已经知道要查什么的时候；不消耗模型 token。"
    ),
    "schema": "列出当前开放的表、字段语义与业务口径定义。先看这个再决定怎么问。",
}


def _result_payload(r: Any) -> dict[str, Any]:
    """裁剪成适合 Agent 消费的形状：别把整个结果集和全部追踪都塞回去。"""
    return {
        "ok": r.ok,
        "sql": r.sql_final or r.sql_raw,
        "rewrites": r.rewrites,          # 系统强制注入了什么，调用方有权知道
        "columns": r.columns,
        # 走 jsonable 而不是让 json.dumps 的 default=str 兜底：Decimal 直接 str() 会变成
        # "0E-20" 这类科学计数法，Agent 拿到后同样会读错数。
        "rows": [[jsonable(v) for v in row] for row in r.rows[:200]],
        "as_of": r.as_of,
        "row_count": r.row_count,
        "truncated": r.truncated or r.row_count > 200,
        "tables_hit": r.tables_hit,
        "metrics_hit": r.metrics_hit,
        "steps": r.step_count,
        "rejected_by": r.rejected_by,
        "error": r.error,
        "hint": r.hint,
        "converged_early": r.converged_early,
        "trace_id": r.trace_id,
        "cost_cny": r.cost_cny,
        "caveat": "askdb 是辅助工具：SQL 由模型生成，结果需人工核对，不应作为决策唯一依据。",
    }


def build_server(cfg: Config, role: str = identity.ANONYMOUS):
    """把 askdb 暴露成 MCP 工具，**按角色收窄之后**。

    这里此前直接吃启动配置，于是三个工具都以实例白名单全量运行 ——
    一个接进来的 Agent 因此拥有超过任何内置角色的可见范围，而审计里
    user 还是空的。那是这套权限体系里唯一一条完全绕开角色的通道。

    收窄走 identity.for_roles，与 HTTP 那条链路是同一个入口，所以
    表白名单、行上限、Schema 召回、口径过滤全部自动跟着生效 ——
    不需要在这里重复实现任何判定，也就不会与 HTTP 那侧走偏。

    默认 ANONYMOUS：它是一个**普通角色**，语义与匿名 HTTP 调用完全一致。
    在配了 role_policies 的实例上它会被真实收窄；没配的实例上它等于
    实例白名单 —— 与今天的行为相同，所以接入这层不会悄悄改变任何现有部署，
    但从此有一个可以调紧的旋钮，而不是一条没有旋钮的旁路。

    user 记 "mcp" 而不是留空：调用方确实不是某个人，留空会让审计里
    那一栏看起来像"记录丢了"。如实写明它来自哪条通道。
    """
    # 2026 版 SDK 的类名是 MCPServer（旧版叫 FastMCP，已不存在）
    from mcp.server.mcpserver import MCPServer

    if role != identity.ANONYMOUS and role not in identity.ROLE_BY_CODE:
        # 拼错角色码就退回全量，正是这次要消灭的那类静默失效 —— 宁可起不来
        raise SystemExit(
            f"未知角色：{role}。可选：{', '.join([*identity.ROLE_BY_CODE, identity.ANONYMOUS])}")
    cfg = identity.for_roles(cfg, [role], user="mcp")

    mcp = MCPServer("askdb")

    @mcp.tool(description=TOOLS_DESC["ask"])
    def ask(question: str, org_id: int | None = None) -> str:
        r = run_ask(question.strip(), cfg, org_id=org_id)
        return json.dumps(_result_payload(r), ensure_ascii=False, default=str)

    @mcp.tool(description=TOOLS_DESC["run_sql"])
    def run_sql(sql: str, org_id: int | None = None) -> str:
        org = cfg.default_org if org_id is None else org_id
        g = guard.check(sql, cfg, org_id=org, dialect=cfg.dialect)
        if not g.ok:
            return json.dumps({
                "ok": False, "rejected_by": g.rejected_by, "error": g.reason,
                "hint": "这是纯代码的 AST 判定，不消耗 token；改完 SQL 再试。",
            }, ensure_ascii=False)
        try:
            with Executor(cfg) as ex:
                ep = ex.explain(g.sql)
                if not ep.ok:
                    return json.dumps({
                        "ok": False, "rejected_by": "R-11", "error": ep.reason,
                        "sql": g.sql, "hint": "缩小时间范围或增加筛选条件。",
                    }, ensure_ascii=False)
                ex.set_org(org)
                res = ex.run(g.sql)
        except DataSourceError as e:
            return json.dumps({"ok": False, "rejected_by": "EXEC",
                               "error": str(e), "hint": e.hint}, ensure_ascii=False)
        return json.dumps({
            "ok": True, "sql": g.sql, "rewrites": g.rewrites,
            "columns": res.columns,
            "rows": [[jsonable(v) for v in row] for row in res.rows[:200]],
            "as_of": res.as_of,
            "row_count": res.row_count, "truncated": res.truncated,
        }, ensure_ascii=False, default=str)

    @mcp.tool(description=TOOLS_DESC["schema"])
    def schema() -> str:
        return json.dumps({
            "tenant": {
                "enabled": cfg.tenant_enabled,
                "column": cfg.tenant_column,
                "default_org": cfg.default_org,
                "note": ("租户谓词由系统强制注入，不要自己写进 WHERE"
                         if cfg.tenant_enabled else "单租户库，不做行级隔离"),
            },
            "limits": {
                "max_rows": cfg.max_rows,
                "max_scan_rows": cfg.raw["guard"]["max_scan_rows"],
                # 配额按**模型调用**计，不是按提问计：一次提问可能触发多次调用。
                # 调用方据此判断还能问几轮，口径写清楚才不会误判。
                "daily_quota_model_calls": cfg.daily_quota,
                "daily_quota_used": build_quota(cfg).peek(),
            },
            "tables": [
                {"name": t.name, "desc": t.desc, "aliases": t.aliases,
                 "columns": [{"name": c.name, "type": c.type, "desc": c.desc,
                              "enum": c.enum, "tenant": c.tenant}
                             for c in t.columns.values()]}
                for t in cfg.tables.values()
            ],
            "metrics": [
                {"name": m.name, "aliases": m.aliases, "scope": m.scope,
                 "definition": m.expr or m.predicate or "", "note": m.note}
                for m in cfg.metrics
            ],
        }, ensure_ascii=False)

    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(description="askdb MCP 服务端（stdio）")
    ap.add_argument("-c", "--config", default="config/askdb.yaml")
    # 这条通道没有登录，角色只能由启动方声明。
    #
    # 默认 ANONYMOUS 曾经意味着"最保守的那一档"；2026-09-06 角色之间不再有
    # 可见面差别之后，**换哪个角色都不会让 MCP 看到更多或更少的表** ——
    # 想收紧只有两条路：改实例白名单，或在 role_policies 里显式配一档。
    # 参数保留：它仍决定审计里记的是哪个角色，且收窄机制随时可以配起来。
    ap.add_argument("--role", default=identity.ANONYMOUS,
                    help="以哪个角色的可见范围运行（默认 ANONYMOUS）")
    a = ap.parse_args()
    build_server(load(a.config), role=a.role).run(transport="stdio")


if __name__ == "__main__":
    main()
