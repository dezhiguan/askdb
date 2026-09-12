"""命令行入口。

`sql` 子命令刻意保留：它跳过模型，直接跑 护栏 → 干跑 → 执行，
让人在**没有模型密钥**的情况下也能完整验证护栏行为。
这既是排障入口，也是评测时对照组的执行方式。
"""

from __future__ import annotations

import sys
from typing import Optional

# serve 的第一声必须在**模块级 import 之前**发出。下面这几行会把 langchain /
# fastapi 整条依赖链拉起来，pydevd 追踪下要几十秒 —— 在此之前控制台一个字都
# 没有，看起来就像进程没起来。只在 serve 时打，别的子命令输出可能被管道消费。
if sys.argv[1:2] == ["serve"]:
    print("askdb 启动中 · 正在加载依赖…", flush=True)

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table as RichTable

from . import guard
from .config import load
from .executor import DataSourceError, Executor
from .agent import run_agent as run_ask
from .graph import AskResult, jsonable

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
    """启动 Web 界面。

    启动过程分段打印，因为**"进程在"不等于"能连"**：在 PyCharm 调试器下
    pydevd 要给整个进程装追踪钩子，从进程起来到端口 LISTEN 有十几秒，
    这段时间里前端打接口是连接被拒 —— 看起来和"服务崩了"一模一样。
    所以最后那行就绪横幅**在端口真正绑定之后**才打（见 _ReadyServer）：
    看到它才算能连，没看到就是还在起。
    """
    import time as _time

    t0 = _time.perf_counter()
    # 第一行必须在**任何重量级 import 之前**打。fastapi / langchain 那一坨在
    # pydevd 追踪下要装很久，早先把 import 写在前面，控制台会先空白几十秒 ——
    # 而那正是最需要一句"它在起，没崩"的时候。
    con.print(f"[bold]askdb[/] 启动中 · 配置 [cyan]{config}[/]")

    import uvicorn

    from .server import create_app

    cfg = _load(config)  # 提前暴露配置错误，别等到浏览器打开才报
    source = (f"{cfg.db_type}:{cfg.db_path.name}" if cfg.db_type == "duckdb"
              else cfg.db_type) if cfg.has_default_source else "无默认数据源（查询须指定运行时数据源）"
    con.print(f"  [dim]配置就绪[/] · 数据源 {source} · 审计 {cfg.audit_log.name}")

    con.print("  [dim]装配 Web 应用…[/]")
    application = create_app(config)

    class _ReadyServer(uvicorn.Server):
        """端口绑定成功之后再报就绪。

        uvicorn 自己那行 "Uvicorn running on …" 在 log_level=warning 下不打，
        而它正是唯一可靠的就绪信号。这里覆写 startup()：super() 里做完
        lifespan 与 create_server 才返回，所以这一行落在**已经能连**之后。
        端口被占用时 super() 直接退出，这行不会打 —— 正是想要的语义。
        """

        async def startup(self, sockets: list | None = None) -> None:
            await super().startup(sockets=sockets)
            con.print(
                f"\n[green]✓ 服务已就绪[/] → [cyan]http://{host}:{port}[/]"
                f"   [dim]（总耗时 {_time.perf_counter() - t0:.1f}s，Ctrl+C 停止）[/]\n")

    _ReadyServer(uvicorn.Config(
        application, host=host, port=port, log_level="warning",
    )).run()


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
    from .agentgraph import replay as do_replay

    cfg = _load(config)
    snaps = do_replay(trace_id, cfg)
    if not snaps:
        from . import auditstore, pgstore

        where = (f"PostgreSQL（{pgstore.raw_dsn() or '未配置连接串'}）"
                 if auditstore.enabled(cfg) else str(cfg.checkpoint_db))
        _fail(f"检查点里没有 {trace_id}",
              f"确认配置是否对得上：这份配置的检查点落在 {where}。"
              "本机与线上落点不同，别拿本机的配置去查线上的 trace。")

    # 2026-09-12：快照字段随 agent 迁到 LangGraph 一并换了。管道那版每步
    # 存的是"这一版 SQL 长什么样"（sql_raw / sql_final / attempt）；agent 每步
    # 存的是"第几步、挑了哪个工具、有没有结论"—— 失败复现要看的正是它在哪
    # 一步拐错了弯。照着旧字段读会整片打不出来（KeyError 或全空）。
    con.print(f"[bold]复现[/] {trace_id}   共 {len(snaps)} 个检查点\n")
    first_bad = None
    for i, s in enumerate(snaps):
        nxt = "、".join(s.get("next") or ()) or "END"
        bad = bool(s.get("error") or s.get("rejected_by"))
        if bad and first_bad is None:
            first_bad = i
        mark = "[red]✗[/]" if bad else "[green]✓[/]"
        step = f"  第 {s['step']} 步" if s.get("step") else ""
        con.print(f"{mark} [{i}] 下一步 [cyan]{nxt}[/]{step}")
        if s.get("tool"):
            con.print(f"      调用工具 [cyan]{s['tool']}[/]")
        if s.get("answer"):
            con.print(f"      结论 [dim]{' '.join(str(s['answer']).split())[:110]}[/]")
        if s.get("tok_used"):
            con.print(f"      [dim]累计 token {s['tok_used']}[/]")
        if s.get("rejected_by"):
            con.print(f"      [red]拦截[/] {s['rejected_by']}")
        if s.get("error"):
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


@app.command("migrate-store")
def cmd_migrate_store(
    config: str = CONFIG,
    dry_run: bool = typer.Option(False, "--dry-run", help="只报要迁多少条，不写库"),
) -> None:
    """把审计 / 审批 / 复核三份 JSONL 灌进 PostgreSQL。

    **可以反复跑。** 判重按自然键（审计=trace_id+ts+phase，审批与复核=id+ts+status），
    已经在库里的跳过 —— 迁移当天写入还在继续，一次跑不干净是常态。

    顺序建议：先跑一次（把历史灌进去），再改配置 observability.store: postgres
    并发版，发版后再跑一次（把切换前那几分钟的尾巴收掉）。
    反过来做会丢中间那段。
    """
    from . import auditstore
    from .audit import read_records
    from .approvals import _read as _read_approvals, store as approvals_store
    from .reviews import _read as _read_reviews, store as reviews_store

    cfg = _load(config)
    streams = [
        (auditstore.AUDIT, "审计", read_records(cfg.audit_log, include_started=True)),
        (auditstore.APPROVALS, "审批", _read_approvals(approvals_store(cfg))),
        (auditstore.REVIEWS, "复核", _read_reviews(reviews_store(cfg))),
    ]
    if dry_run:
        for _, label, recs in streams:
            con.print(f"{label}：文件里 {len(recs)} 条")
        return

    try:
        before = auditstore.counts()
        for stream, label, recs in streams:
            r = auditstore.import_records(stream, recs)
            con.print(f"{label}：文件 {r['total']} 条，导入 {r['imported']}，"
                      f"已存在跳过 {r['skipped']}")
        after = auditstore.counts()
    except Exception as e:                              # 连不上/没权限：说清楚
        _fail(f"凭据库不可用：{e}",
              "设置 ASKDB_STORE_DSN（或复用 ASKDB_SOURCES_DSN），口令走 "
              "ASKDB_STORE_PASSWORD / ASKDB_SOURCES_PASSWORD。")
    con.print(f"[dim]库中总数：{before} → {after}[/]")


def _load(path: str):
    try:
        return load(path)
    except FileNotFoundError:
        _fail(f"配置文件不存在：{path}", "确认在项目根目录下运行，或用 --config 指定路径。")
    except ValueError as e:
        _fail(str(e), "修正 config/ 下的 YAML 后重试。")


if __name__ == "__main__":
    app()
