"""页面脚本的冒烟测试 —— 在 Node 里把它跑一遍。

Python 测试碰不到浏览器里的 ReferenceError，而前端已经因此溜过两个 bug：
「执行步数」渲染成 [object Object]，以及 renderEval 里变量用出作用域导致
整个函数抛出、消融卡空白。后者尤其隐蔽：页面不报错、不告警，只是少一块内容。

没装 Node 就跳过 —— 它是可选的开发期检查，不是运行时依赖。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from askdb import server

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "js" / "run_page.js"
PAGE = ROOT / "askdb" / "web" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="未安装 Node")


@pytest.fixture
def client(cfg, monkeypatch):
    # client 夹具定义在 test_server.py 里，跨文件用不了，这里自建一个
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def _fixtures(client: TestClient) -> dict:
    """接口数据取自真实应用，避免桩数据与接口形状脱节。"""
    out = {}
    for path in ("/api/health", "/api/schema", "/api/eval"):
        r = client.get(path)
        if r.status_code == 200:
            out[path] = r.json()
    # introspect 要连库，测试环境不一定有 —— 给一个形状正确的空壳
    out.setdefault("/api/introspect", {"ok": True, "tables": [],
                                       "allowed_count": 0, "total": 0})
    return out


def _run(tmp_path: Path, fixtures: dict) -> subprocess.CompletedProcess:
    fp = tmp_path / "fixtures.json"
    fp.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    return subprocess.run(["node", str(RUNNER), str(PAGE), str(fp)],
                          capture_output=True, text=True, timeout=60)


def test_page_script_runs_without_throwing(tmp_path, client):
    r = _run(tmp_path, _fixtures(client))
    assert r.returncode == 0, f"页面脚本抛异常：{r.stderr.strip()}"


def test_eval_page_survives_missing_results(tmp_path, client):
    """评测没跑过时页面必须显示"尚未运行"，而不是抛异常导致整块空白。"""
    f = _fixtures(client)
    f["/api/eval"] = {"available": False}
    r = _run(tmp_path, f)
    assert r.returncode == 0, f"无评测结果时页面脚本抛异常：{r.stderr.strip()}"
