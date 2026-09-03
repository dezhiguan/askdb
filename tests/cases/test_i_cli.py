"""I 域 · 命令行（7 条）。"""
from __future__ import annotations
import pytest
from pathlib import Path

from typer.testing import CliRunner
from askdb.cli import app

R = CliRunner()


def test_i01_ask_help():
    assert R.invoke(app, ["ask", "--help"]).exit_code == 0


def test_i02_ask_failure_exit_code(monkeypatch):
    r = R.invoke(app, ["sql", "DELETE FROM documents", "-c", "config/askdb.yaml"])
    assert r.exit_code != 0


def test_i03_sql_without_api_key(monkeypatch, tmp_path, sample_db):
    """直查不该依赖模型密钥。

    用自建配置而不是 config/askdb.yaml：开发配置可以没有默认数据源
    （运行时源专用实例就是这样），那时 CLI 直查本来就该报错 ——
    拿它当基线，这条用例考的就不再是"要不要模型密钥"了。
    """
    import yaml

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    root = Path(__file__).resolve().parent.parent.parent
    raw = yaml.safe_load((root / "config" / "askdb.yaml").read_text(encoding="utf-8"))
    raw["datasource"] = {"type": "duckdb", "path": str(sample_db), "read_only": True}
    raw["tenant"] = {**raw["tenant"], "column": "org_id",
                     "default_ctx": 65, "mode": "predicate"}
    raw["tables_file"] = str(root / "config" / "tables.yaml")
    raw["metrics_file"] = str(root / "config" / "metrics.yaml")
    raw["observability"] = {**raw["observability"],
                            "audit_log": str(tmp_path / "a.jsonl"),
                            "checkpoint_db": str(tmp_path / "c.sqlite")}
    raw.pop("identity", None)
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    cfg_path = d / "askdb.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    r = R.invoke(app, ["sql", "SELECT 1 AS x", "-c", str(cfg_path)])
    assert r.exit_code == 0, "直查不该依赖模型密钥"


def test_i03b_sql_without_default_source(tmp_path):
    """没有默认数据源时，直查要给一句能看懂的话，而不是抛栈。"""
    import yaml

    root = Path(__file__).resolve().parent.parent.parent
    raw = yaml.safe_load((root / "config" / "askdb.yaml").read_text(encoding="utf-8"))
    raw.pop("datasource", None)
    raw["tables_file"] = str(root / "config" / "tables.yaml")
    raw["metrics_file"] = str(root / "config" / "metrics.yaml")
    raw["observability"] = {**raw["observability"],
                            "audit_log": str(tmp_path / "a.jsonl"),
                            "checkpoint_db": str(tmp_path / "c.sqlite")}
    raw["tenant"] = {**raw["tenant"], "mode": "predicate"}
    raw.pop("identity", None)
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    cfg_path = d / "askdb.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    r = R.invoke(app, ["sql", "SELECT 1 AS x", "-c", str(cfg_path)])
    assert r.exit_code != 0
    assert "未配置默认数据源" in (r.output + str(r.exception))


def test_i04_check_command():
    r = R.invoke(app, ["check", "-c", "config/askdb.yaml"])
    assert r.exit_code in (0, 1)


def test_i05_missing_config_friendly():
    r = R.invoke(app, ["check", "-c", "config/nope.yaml"])
    assert r.exit_code != 0 and "Traceback" not in r.output


def test_i06_replay_exists():
    assert R.invoke(app, ["replay", "--help"]).exit_code == 0


def test_i07_replay_unknown_trace():
    r = R.invoke(app, ["replay", "zzz", "-c", "config/askdb.yaml"])
    assert r.exit_code != 0 and "检查点" in r.output
