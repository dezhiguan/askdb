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
from .graph import AskResult, ask as run_ask, jsonable

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
        t.add_row(*["" if v is None else str(jsonable(v)) for v in r])
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

    g = guard.check(statement, cfg, org_id=org_id, dialect=cfg.dialect)
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
            ex.set_org(org_id)
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


@app.command("replay")
def cmd_replay(
    trace_id: str = typer.Argument(..., help="失败样本的 trace_id"),
    config: str = CONFIG,
) -> None:
    """从检查点原样复现某次调用，按节点拆解判定链路。

    设计文档 §5「检查点持久化至本地 SQLite，作用是失败样本可原样复现」
    与 §10.1「失败报告按步拆解，标注首个偏离步」落地于此。
    评测报告和界面上都在提示用这个命令，但它此前并不存在。

    检查点库跟着配置走 —— 用哪份配置跑出来的失败，就用哪份配置复现。
    """
    from .graph import replay as do_replay

    cfg = _load(config)
    snaps = do_replay(trace_id, cfg)
    if not snaps:
        _fail(f"检查点里没有 {trace_id}",
              f"确认配置是否对得上：这份用的检查点库是 {cfg.checkpoint_db}。"
              "不同数据源的检查点分开存放。")

    con.print(f"[bold]复现[/] {trace_id}   共 {len(snaps)} 个检查点\n")
    first_bad = None
    for i, s in enumerate(snaps):
        nxt = "、".join(s["next"]) or "END"
        bad = bool(s["error"] or s["rejected_by"])
        if bad and first_bad is None:
            first_bad = i
        mark = "[red]✗[/]" if bad else "[green]✓[/]"
        att = f"  重试 {s['attempt']}" if s.get("attempt") else ""
        con.print(f"{mark} [{i}] 下一步 [cyan]{nxt}[/]{att}")
        if s["sql_raw"] and s["sql_raw"] != s["sql_final"]:
            con.print(f"      模型产出 [dim]{' '.join(s['sql_raw'].split())[:110]}[/]")
        if s["sql_final"]:
            con.print(f"      改写之后 {' '.join(s['sql_final'].split())[:110]}")
        if s["rejected_by"]:
            con.print(f"      [red]拦截[/] {s['rejected_by']}")
        if s["error"]:
            con.print(f"      [red]报错[/] {str(s['error'])[:150]}")

    if first_bad is None:
        con.print("\n[dim]全链路无拦截、无报错 —— 若这题仍判失败，"
                  "问题在结果与标准答案不一致，不在链路。[/]")
    else:
        con.print(f"\n[bold]首个偏离步[/] 第 {first_bad} 个检查点")


@app.command("hash-password")
def cmd_hash_password(
    password: str = typer.Argument(..., help="要哈希的口令"),
) -> None:
    """生成一条口令哈希，粘进配置的 auth.accounts[].password_hash。

    口令**不进配置文件、不进版本库**，配置里只放哈希。
    scrypt n=2^14，本机约 60~100ms —— 登录慢一点无所谓，离线爆破要足够贵。
    """
    from .auth import hash_password

    # 裸 print：rich 会按终端宽度折行，而这串要被原样复制进配置。
    # 折行过一次，粘进去的哈希是截断的，登录直接 500。
    print(hash_password(password))
    con.print("[dim]粘到 config 的 auth.accounts[].password_hash；口令本身不要入库。[/]")


@app.command("session-secret")
def cmd_session_secret() -> None:
    """生成一把会话签名密钥，写进 .env 的 ASKDB_SESSION_SECRET。

    多副本必须共用同一把 —— 各签各的会表现为「刷新几次就掉线」，
    而这种故障比登不上更难查。
    """
    import secrets as _secrets

    print(f"ASKDB_SESSION_SECRET={_secrets.token_urlsafe(32)}")


def _load(path: str):
    try:
        return load(path)
    except FileNotFoundError:
        _fail(f"配置文件不存在：{path}", "确认在项目根目录下运行，或用 --config 指定路径。")
    except ValueError as e:
        _fail(str(e), "修正 config/ 下的 YAML 后重试。")


if __name__ == "__main__":
    app()
