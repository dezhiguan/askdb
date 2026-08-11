"""命令行测试 —— 重点是失败路径必须给出可执行的下一步。"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from askdb import cli
from askdb.llm import LlmUsage, SqlDraft

runner = CliRunner()


def _cfg_file(tmp_path, cfg):
    """把夹具配置落成一个临时 YAML，让 CLI 能通过 --config 读到。"""
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    raw = dict(cfg.raw)
    raw["tables_file"] = str(cfg.root / "config" / "tables.yaml")
    raw["metrics_file"] = str(cfg.root / "config" / "metrics.yaml")
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
    class Fake:
        def generate_sql(self, *a, **k):
            return SqlDraft(sql="SELECT file_name AS 文件名 FROM documents", reasoning="r"), LlmUsage(9, 4)

        def structured(self, schema, system, human):
            from askdb.planner import Assessment, Plan
            if schema is Plan:
                return Plan(multi_step=False, reason="替身"), LlmUsage(1, 1)
            return Assessment(enough=True, reason="替身"), LlmUsage(1, 1)


    from askdb import graph
    monkeypatch.setattr(cli, "run_ask",
                        lambda q, c, org_id=None: graph.ask(q, c, org_id=org_id, llm=Fake()))
    r = runner.invoke(cli.app, ["ask", "有哪些文档", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 0
    assert "执行链路" in r.stdout and "强制改写" in r.stdout


def test_ask_command_exits_nonzero_on_block(tmp_path, cfg, monkeypatch):
    class Fake:
        def generate_sql(self, *a, **k):
            return SqlDraft(sql="DELETE FROM documents", reasoning="r"), LlmUsage(1, 1)

        def structured(self, schema, system, human):
            from askdb.planner import Assessment, Plan
            if schema is Plan:
                return Plan(multi_step=False, reason="替身"), LlmUsage(1, 1)
            return Assessment(enough=True, reason="替身"), LlmUsage(1, 1)


    from askdb import graph
    monkeypatch.setattr(cli, "run_ask",
                        lambda q, c, org_id=None: graph.ask(q, c, org_id=org_id, llm=Fake()))
    r = runner.invoke(cli.app, ["ask", "删掉文档", "-c", _cfg_file(tmp_path, cfg)])
    assert r.exit_code == 1 and "R-02" in r.stdout
