"""L 域 · 前端渲染安全（补 L-10）。

其余 L-04～L-09 需要真正的 DOM 断言能力（jsdom 之类），本轮不落地；
L-10 是这批里唯一的 P0 安全项，而它不需要完整 DOM —— 直接对转义函数
喂攻击载荷即可，成本极低，不该因为"要搭 DOM 环境"而拖着不做。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PAGE = ROOT / "askdb" / "web" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="未安装 Node")

PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "<iframe src=javascript:alert(1)>",
    "'; DROP TABLE documents; --",
    "<svg/onload=alert(1)>",
]


def test_l10_result_values_are_escaped(tmp_path):
    """结果表格直接渲染数据库里的字符串。转义漏了就是存储型 XSS ——
    攻击载荷只要进过一次库（比如文件名），每个看结果的人都会中招。
    """
    src = "".join(m.group(1) for m in
                  re.finditer(r"<script>([\s\S]*?)</script>", PAGE.read_text(encoding="utf-8")))
    # 只取 esc 的定义，不牵扯页面其余部分
    m = re.search(r"const esc = [^\n]+", src)
    assert m, "页面里找不到 esc()，转义无从谈起"

    runner = tmp_path / "t.js"
    runner.write_text(
        m.group(0) + "\n"
        + "const out = " + json.dumps(PAYLOADS) + ".map(esc);\n"
        + "console.log(JSON.stringify(out));\n",
        encoding="utf-8")
    r = subprocess.run(["node", str(runner)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    escaped = json.loads(r.stdout)

    for raw, safe in zip(PAYLOADS, escaped):
        assert "<" not in safe and ">" not in safe, f"尖括号未转义：{raw} → {safe}"
        assert '"' not in safe, f"双引号未转义（属性注入面）：{raw} → {safe}"
        # 原文内容仍应可读，转义不等于丢信息
        assert "alert" in safe or "DROP" in safe


def test_l11_page_shows_which_config_is_loaded():
    """同一台机器上会同时跑多个实例（样例库 :8000 / 生产库 :8765），
    界面长得一模一样。不显示配置文件路径，就只能靠猜眼前这个连的是哪儿 ——
    实际已经因此误判过一次。
    """
    page = PAGE.read_text(encoding="utf-8")
    assert "HEALTH.config" in page, "页面必须把当前配置文件显示出来"
    assert "配置文件" in page


def test_public_instance_config_is_safe():
    """对外开放实例的两条安全边界，任何一条被改掉都不该悄悄上线。

    askdb 不设账号体系（§1.1：数据库连接本身即权限边界），
    所以开放实例只能靠"连的库里没有真实数据"和"不接模型"这两条自保。
    """
    from askdb.config import load

    c = load(ROOT / "config" / "public.yaml")
    assert c.db_type == "duckdb", "对外实例只能连合成样例库"
    assert c.db_path.name == "sample.duckdb"
    assert c.llm.get("disabled") is True, "对外实例必须显式声明不接模型"
    assert c.api_key() is None, "api_key_env 必须指向一个永不设置的变量名"
    # 阈值须比本机开发更紧
    dev = load(ROOT / "config" / "askdb.yaml")
    assert c.max_rows <= dev.max_rows
    assert c.raw["guard"]["statement_timeout_ms"] <= dev.raw["guard"]["statement_timeout_ms"]
