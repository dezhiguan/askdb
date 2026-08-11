"""命令行入口。

`sql` 子命令刻意保留：它跳过模型，直接跑 护栏 → 干跑 → 执行，
让人在**没有模型密钥**的情况下也能完整验证护栏行为。
这既是排障入口，也是评测时对照组的执行方式。
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table as RichTable

from . import guard
from .config import load
from .executor import DataSourceError, Executor
from .graph import AskResult, ask as run_ask

app = typer.Typer(add_completion=False, help="askdb —— 可信数据问答 Agent")
con = Console()

CONFIG = typer.Option("config/askdb.yaml", "--config", "-c", help="配置文件路径")
ORG = typer.Option(None, "--org", "-o", help="租户 ID，覆盖配置中的默认值")


def _fail(msg: str, hint: str = "") -> None:
    con.print(f"[bold red]✗[/] {msg}")
    if hint:
        con.print(f"  [dim]{hint}[/]")
    raise typer.Exit(1)


def _print_sql(sql: str, title: str = "最终 SQL") -> None:
    con.print(Panel(Syntax(sql, "sql", theme="ansi_dark", word_wrap=True),
                    title=title, border_style="cyan", title_align="left"))


def _print_rows(columns: list[str], rows: list[list], truncated: bool) -> None:
    if not columns:
        return
    t = RichTable(show_lines=False, header_style="bold")
    for c in columns:
        t.add_column(str(c))
    for r in rows[:50]:
        t.add_row(*["" if v is None else str(v) for v in r])
    con.print(t)
    if len(rows) > 50:
        con.print(f"[dim]（仅显示前 50 行，共 {len(rows)} 行）[/]")
    if truncated:
        con.print("[yellow]⚠ 结果已按行数上限截断（R-13）[/]")


def _print_result(r: AskResult) -> None:
    if r.tables_hit:
        line = f"[dim]命中表[/] {'、'.join(r.tables_hit)}"
        if r.metrics_hit:
            line += f"   [dim]口径[/] {'、'.join(r.metrics_hit)}"
        con.print(line)

    con.print("\n[bold]执行链路[/]")
    icon = {"ok": "[green]✓[/]", "blocked": "[red]✕[/]", "failed": "[red]![/]", "skipped": "[dim]-[/]"}
    for s in r.steps:
        con.print(f"  {icon.get(s['status'], '·')} {s['step']:<15} {s['ms']:>5} ms  [dim]{s['note']}[/]")

    if r.rewrites:
        con.print(f"\n[cyan]强制改写[/] {'；'.join(r.rewrites)}  [dim](模型改不掉)[/]")

    if not r.ok:
        con.print()
        tag = f"[{r.rejected_by}] " if r.rejected_by else ""
        con.print(f"[bold red]✗ {tag}{r.error}[/]")
        if r.hint:
            con.print(f"  [dim]{r.hint}[/]")
        if r.sql_raw:
            _print_sql(r.sql_raw, "模型产出（未执行）")
        return

    con.print()
    _print_sql(r.sql_final)
    _print_rows(r.columns, r.rows, r.truncated)
    con.print(
        f"\n[dim]{r.row_count} 行 · {r.elapsed_ms} ms · "
        f"{r.attempts} 轮 · {r.tok_in}+{r.tok_out} tok · ¥{r.cost_cny} · trace {r.trace_id}[/]"
    )


@app.command("ask")
def cmd_ask(
    question: str = typer.Argument(..., help="用一句话描述你要查什么"),
    config: str = CONFIG,
    org: Optional[int] = ORG,
) -> None:
    """自然语言提问（需要模型密钥）。"""
    cfg = _load(config)
    con.print(f"[bold]问题[/] {question}\n")
    r = run_ask(question, cfg, org_id=org)
    _print_result(r)
    if not r.ok:
        raise typer.Exit(1)


@app.command("sql")
def cmd_sql(
    statement: str = typer.Argument(..., help="直接给一条 SQL"),
    config: str = CONFIG,
    org: Optional[int] = ORG,
) -> None:
    """跳过模型，直接验证 护栏 → 干跑 → 执行（无需密钥）。"""
    cfg = _load(config)
    org_id = cfg.default_org if org is None else org

    g = guard.check(statement, cfg, org_id=org_id)
    if not g.ok:
        con.print(f"[bold red]✗ [{g.rejected_by}][/] {g.reason}")
        raise typer.Exit(1)
    con.print(f"[green]✓[/] 护栏通过   [cyan]{'；'.join(g.rewrites) or '无需改写'}[/]")
    _print_sql(g.sql)

    with Executor(cfg) as ex:
        ep = ex.explain(g.sql)
        if not ep.ok:
            _fail(f"[R-11] {ep.reason}", "缩小时间范围或加筛选条件。")
        con.print(f"[green]✓[/] 干跑通过   预估扫描 {ep.est_rows:,} 行" if ep.est_rows
                  else "[green]✓[/] 干跑通过")
        try:
            res = ex.run(g.sql)
        except DataSourceError as e:
            _fail(str(e), e.hint)
        _print_rows(res.columns, res.rows, res.truncated)
        con.print(f"\n[dim]{res.row_count} 行 · {res.elapsed_ms} ms[/]")


@app.command("check")
def cmd_check(config: str = CONFIG) -> None:
    """配置与数据源自检。上线前先跑这个。"""
    cfg = _load(config)
    con.print(f"[green]✓[/] 配置校验通过   白名单 {len(cfg.tables)} 张表 · 口径 {len(cfg.metrics)} 条")
    con.print(f"[dim]  租户列 {cfg.tenant_column} · 涉及 {len(cfg.tenant_tables())} 张表[/]\n")

    ex = Executor(cfg)
    bad = 0
    for c in ex.self_check():
        mark = "[green]✓[/]" if c["ok"] else "[red]✗[/]"
        con.print(f"  {mark} {c['name']:<12} [dim]{c['detail']}[/]")
        bad += 0 if c["ok"] else 1
    ex.close()

    key = "[green]已配置[/]" if cfg.api_key() else f"[yellow]未配置[/]（{cfg.llm['api_key_env']}）"
    con.print(f"\n  模型密钥 {key}")
    if not cfg.api_key():
        con.print("  [dim]未配置时 `askdb ask` 不可用，`askdb sql` 仍可验证护栏。[/]")
    if bad:
        _fail(f"{bad} 项自检未通过，拒绝进入可用状态。")
    con.print("\n[bold green]自检全部通过。[/]")


@app.command("seed")
def cmd_seed() -> None:
    """生成本机样例库。"""
    from data.seed import build

    build()


@app.command("serve")
def cmd_serve(
    config: str = CONFIG,
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
) -> None:
    """启动 Web 界面。"""
    import uvicorn

    from .server import create_app

    _load(config)  # 提前暴露配置错误，别等到浏览器打开才报
    con.print(f"[bold]askdb[/] → [cyan]http://{host}:{port}[/]\n")
    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")


def _load(path: str):
    try:
        return load(path)
    except FileNotFoundError:
        _fail(f"配置文件不存在：{path}", "确认在项目根目录下运行，或用 --config 指定路径。")
    except ValueError as e:
        _fail(str(e), "修正 config/ 下的 YAML 后重试。")


if __name__ == "__main__":
    app()
