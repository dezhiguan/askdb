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
