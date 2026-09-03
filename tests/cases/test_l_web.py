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
PAGE = ROOT / "askdb" / "web_legacy" / "index.html"

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
    """对外开放实例的安全与成本边界，任何一条被改掉都不该悄悄上线。

    2026-09-03 前提变了：这个实例原来连合成样例库，"泄露不了真实数据"是
    整套安全论证的地基。按 @guandezhi 决定改为直连 ragforge 生产主库之后，
    那条地基没了，断言必须跟着换 —— **不是放宽，是换到新的边界上**。

    现在挡在公网与真实数据之间的只剩三样，这条测试逐条钉住：
      1. 必须登录（auth.required）—— 至少让调用方在审计里有名有姓
      2. 只读连接 + 租户隔离（应用层谓词 + 库侧 RLS 双层）
      3. 回放关闭 —— 它会返回 SQL 全文，等于把库结构透给任何登录用户
    加上原有的成本边界：每日配额 + 单价不高于开发配置。
    """
    from askdb.config import load

    c = load(ROOT / "config" / "public.yaml")
    dev = load(ROOT / "config" / "askdb.yaml")

    # ---- 数据边界 ----
    # 连的是真实库，所以每一层都必须在
    assert c.raw["auth"]["required"] is True, "连真实库的对外实例必须强制登录"
    assert c.tenant_enabled, "租户隔离不能关"
    assert c.raw["tenant"]["mode"] == "rls_and_predicate", \
        "对外实例要双层隔离：应用层被绕过时库侧 RLS 仍在"
    assert c.raw["tenant"]["on_unresolved"] == "reject", "定不出租户归属时必须拒绝，不能放行"
    assert c.raw["observability"]["replay_api"] is False, \
        "回放会返回 SQL 全文，连真实库时必须关"
    # 口令只走环境变量，任何形式的明文都不该出现在仓库里
    assert c.raw["datasource"].get("password_env"), "数据库口令必须走环境变量"
    assert "password=" not in c.raw["datasource"].get("dsn", ""), "连接串里不得写明文口令"

    # ---- 成本边界 ----
    assert 0 < c.daily_quota <= 500, f"每日配额必须设置且不得过宽：{c.daily_quota}"
    # 单价须不高于开发配置用的模型 —— 对外成本要按"被刷满"估，不是按正常使用估
    assert c.llm["price_input_per_1k"] <= dev.llm["price_input_per_1k"]
    assert c.llm["price_output_per_1k"] <= dev.llm["price_output_per_1k"]
    # 配额靠数当天审计条数实现，日志必须落在挂了持久卷的目录，
    # 否则 Pod 一重启计数归零，配额形同虚设
    assert "/var/" in str(c.audit_log).replace("\\", "/"), \
        f"审计日志必须写在 var/（k8s 在此挂持久卷）：{c.audit_log}"

    # ---- 护栏阈值须比本机开发更紧 ----
    assert c.max_rows <= dev.max_rows
    assert c.raw["guard"]["statement_timeout_ms"] <= dev.raw["guard"]["statement_timeout_ms"]
