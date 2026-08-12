"""I 域 · 命令行（7 条）。"""
from __future__ import annotations
import pytest
from typer.testing import CliRunner
from askdb.cli import app

R = CliRunner()


def test_i01_ask_help():
    assert R.invoke(app, ["ask", "--help"]).exit_code == 0


def test_i02_ask_failure_exit_code(monkeypatch):
    r = R.invoke(app, ["sql", "DELETE FROM documents", "-c", "config/askdb.yaml"])
    assert r.exit_code != 0


def test_i03_sql_without_api_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = R.invoke(app, ["sql", "SELECT 1 AS x", "-c", "config/askdb.yaml"])
    assert r.exit_code == 0, "直查不该依赖模型密钥"


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
