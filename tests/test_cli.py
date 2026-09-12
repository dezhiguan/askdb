"""命令行测试 —— 重点是失败路径必须给出可执行的下一步。"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

def _blocked_result(q):
    """被护栏拦下的那一种。CLI 据 ok=False 退非零码 —— 这条验的就是退出码，
    所以必须给一个**失败**的结果，不能复用成功的那个替身。"""
    from askdb.graph import AskResult

    return AskResult(ok=False, question=q, trace_id="b" * 12, org_id=0,
                     thread_id="b" * 12, rejected_by="R-02",
                     error="不允许的写操作", hint="只读查询才可执行")


def _fake_result(q, cfg):
    """CLI 只验**输出格式**，不验查询本身 —— 给一个字段齐全的 AskResult 即可。

    2026-09-12 从打桩 graph.ask 改过来：那条链路当天随固定管道一起删了。
    steps / rewrites 必须给：CLI 的「执行链路」「强制改写」两块直接读它们，
    少一个那一块就整段不渲染，而这条用例验的正是它们在不在。
    """
    from askdb.graph import AskResult

    return AskResult(
        ok=True, question=q, trace_id="a" * 12, org_id=0, thread_id="a" * 12,
        sql_raw="SELECT file_name FROM documents",
        sql_final="SELECT file_name AS 文件名 FROM documents LIMIT 200",
        columns=["文件名"], rows=[["x"]], row_count=1, reasoning="替身",
        rewrites=["注入 LIMIT 200"],
        steps=[{"step": "schema_recall", "status": "ok", "ms": 3, "note": "命中 1 张表"},
               {"step": "tool_call", "status": "ok", "ms": 9, "note": "返回 1 行",
                "tool": "execute_sql"}])


from askdb import cli
from askdb.llm import LlmUsage, SqlDraft

runner = CliRunner()


def _cfg_file(tmp_path, cfg):
    """把夹具配置落成一个临时 YAML，让 CLI 能通过 --config 读到。"""
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    raw = dict(cfg.raw)
    raw["tables_file"] = str(cfg.root / "config" / "schemas" / "sample-tables.yaml")
    raw["metrics_file"] = str(cfg.root / "config" / "schemas" / "sample-metrics.yaml")
    p = d / "askdb.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return str(p)


def test_check_passes(tmp_path, cfg):
    r = runner.invoke(cli.app, ["check", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 0
    assert "自检全部通过" in r.stdout
    assert "写操作实探" in r.stdout


def test_check_reports_missing_key_but_still_passes(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv(cfg.llm["api_key_env"], raising=False)
    r = runner.invoke(cli.app, ["check", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 0
    assert "askdb sql" in r.stdout          # 指出无密钥仍可验证护栏


def test_check_fails_when_datasource_missing(tmp_path, cfg):
    cfg.raw["datasource"]["path"] = str(tmp_path / "gone.duckdb")
    r = runner.invoke(cli.app, ["check", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 1
    assert "未通过" in r.stdout


def test_missing_config_file_is_explained():
    r = runner.invoke(cli.app, ["check", "-c", "nope/askdb.yaml"])
    assert r.exit_code == 1
    assert "配置文件不存在" in r.stdout and "--config" in r.stdout


def test_invalid_config_is_explained(tmp_path, cfg):
    path = _cfg_file(tmp_path, cfg)
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    raw["tenant"]["column"] = "nope_col"
    open(path, "w", encoding="utf-8").write(yaml.safe_dump(raw, allow_unicode=True))
    r = runner.invoke(cli.app, ["check", "-c", path])
    assert r.exit_code == 1
    assert "YAML" in r.stdout


def test_sql_command_runs_and_shows_rewrites(tmp_path, cfg):
    r = runner.invoke(cli.app, [
        "sql", "SELECT file_name AS 文件名 FROM documents WHERE status='PROCESSING'",
        "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 0
    assert "护栏通过" in r.stdout and "org_id" in r.stdout
    assert "干跑通过" in r.stdout


def test_sql_command_reports_block(tmp_path, cfg):
    r = runner.invoke(cli.app, ["sql", "DELETE FROM documents", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 1 and "R-02" in r.stdout


def test_sql_command_reports_scan_threshold(tmp_path, cfg):
    cfg.raw["guard"]["max_scan_rows"] = 1
    r = runner.invoke(cli.app, ["sql", "SELECT id FROM documents", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 1 and "R-11" in r.stdout


def test_sql_command_org_override(tmp_path, cfg):
    r = runner.invoke(cli.app, ["sql", "SELECT id FROM documents",
                                "-c", _cfg_file(tmp_path, cfg), "-o", "66"])
    assert r.exit_code == 0 and "org_id = 66" in r.stdout.replace("\n", " ")


def test_ask_command_renders_pipeline(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(cli, "run_ask",
                        lambda q, c, org_id=None, **kw: _fake_result(q, c))
    r = runner.invoke(cli.app, ["ask", "有哪些文档", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 0
    assert "执行链路" in r.stdout and "强制改写" in r.stdout


def test_ask_command_exits_nonzero_on_block(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(cli, "run_ask",
                        lambda q, c, org_id=None, **kw: _blocked_result(q))
    r = runner.invoke(cli.app, ["ask", "删掉文档", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 1 and "R-02" in r.stdout


def test_replay_command_exists_and_reports_missing_trace(tmp_path, monkeypatch):
    """评测报告与界面都在提示 `askdb replay <trace_id>`，此前该命令并不存在。

    §5 的检查点、§10.1 的"失败报告按步拆解、标注首个偏离步"都靠它落地。
    """
    from typer.testing import CliRunner
    from askdb.cli import app

    r = CliRunner().invoke(app, ["replay", "--help"])
    assert r.exit_code == 0

    r = CliRunner().invoke(app, ["replay", "no-such-trace", "-c", "config/askdb.yaml"])
    assert r.exit_code != 0
    # 报错必须指出检查点库路径 —— 最常见的原因就是配置和跑评测时对不上
    assert "检查点" in r.output


# ---------------------------------------------------------------- 复现

def test_replay_renders_the_decision_trail(tmp_path, cfg, monkeypatch):
    """`askdb replay` 打的是**决策轨迹**：第几步、调了哪个工具、在哪拐错弯。

    2026-09-12 这里修过一个 bug：agent 迁到 LangGraph 之后快照字段整套换了
    （管道存 sql_raw/sql_final/attempt，agent 存 step/tool/answer），而这段
    渲染没跟着改 —— 照旧字段读会整片打不出来，而命令本身照样退 0。
    """
    from askdb import agentgraph

    monkeypatch.setattr(agentgraph, "replay", lambda t, c: [
        {"next": ["decide"], "step": 1, "tool": "", "answer": "",
         "error": "", "rejected_by": None, "tok_used": 120},
        {"next": ["act"], "step": 2, "tool": "get_table_schema", "answer": "",
         "error": "", "rejected_by": None, "tok_used": 900},
        {"next": [], "step": 3, "tool": "execute_sql", "answer": "",
         "error": "列不存在", "rejected_by": "EXEC", "tok_used": 1500},
    ])
    r = runner.invoke(cli.app, ["replay", "a" * 12, "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 0, r.stdout
    assert "共 3 个检查点" in r.stdout
    assert "get_table_schema" in r.stdout and "execute_sql" in r.stdout
    # 首个偏离步要指出来 —— 这条命令存在的理由就是这一句
    assert "首个偏离步" in r.stdout and "第 2 个检查点" in r.stdout


def test_replay_says_where_it_looked_when_nothing_found(tmp_path, cfg, monkeypatch):
    """查不到时要说**去哪儿找的**。

    本机与线上检查点落点不同，不说清楚的话，人会拿本机配置反复查线上的
    trace，而命令只回一句"没有"。
    """
    from askdb import agentgraph

    monkeypatch.setattr(agentgraph, "replay", lambda t, c: [])
    r = runner.invoke(cli.app, ["replay", "b" * 12, "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code != 0
    # rich 会按终端宽度折行，断言必须先把换行抹掉 —— 否则一句提示语被折过
    # 就断言失败，而提示语本身完全正确。
    flat = r.stdout.replace("\n", "")
    assert "检查点里没有" in flat
    assert "别拿本机的配置去查线上的 trace" in flat
    assert "checkpoints.sqlite" in flat, "没说清去哪儿找的"


# ---------------------------------------------------------------- 凭据

def test_hash_password_is_verifiable_and_salted():
    """输出必须能被 verify_password 认，且**每次不同**（带盐）。

    裸 print 而不是 rich：rich 会按终端宽度折行，而这串要被原样复制进配置，
    折过一次粘进去就是截断的哈希，登录直接 500。
    """
    from askdb.auth import verify_password

    out = [runner.invoke(cli.app, ["hash-password", "pw"]).stdout.splitlines()[0]
           for _ in range(2)]
    assert out[0] != out[1], "同一口令两次哈希相同 —— 没加盐"
    for h in out:
        assert verify_password("pw", h)
        assert not verify_password("pw2", h)


def test_session_secret_is_long_enough_and_random():
    """会话密钥要够长、且每次不同。短了或固定，签名就等于没签。"""
    a = runner.invoke(cli.app, ["session-secret"])
    b = runner.invoke(cli.app, ["session-secret"])
    assert a.exit_code == 0
    first = a.stdout.splitlines()[0].strip()
    assert len(first) >= 40, f"密钥太短：{len(first)}"
    assert first != b.stdout.splitlines()[0].strip()
