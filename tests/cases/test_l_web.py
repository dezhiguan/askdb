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

    2026-09-07 当天变两次，最终落在"读也要登录"：

      · 先按 @guandezhi 决定把 auth.required 改回 false（未登录可读）。
      · 同日又撤掉内置数据源，ragforge 与 careermate 两个库改走运行时注册表。
        **这两条合起来才是真正的变化**：运行时源上 derive_config 写死关租户，
        行级边界整个消失，于是"匿名可读"的实际含义变成任何访客可查 ragforge
        全部组织 + careermate 生产库全部数据（含手机号、简历、口令哈希）。
        因此 required 改回 true —— 用登录换回租户隔离让出的那一层。

    这条测试的清单跟着换，不是把某一条删掉了事。现在挡在公网与真实数据之间的
    是这几样，逐条钉住：
      1. **读要登录**（auth.required）—— 撤掉租户隔离之后，它是唯一挡住
         "全表 + 无租户"这个组合的东西
      2. 写门 —— 未登录不能改配置。落在配置上就是 auth.enabled 必须为真：
         关掉它连登录都没有，写操作只剩一把 ASKDB_ADMIN_TOKEN 挡着
      3. 回放关闭 —— 它会返回 SQL 全文，等于把库结构透给任何访客
    加上原有的成本边界：每日配额 + 单价不高于开发配置。
    """
    from askdb.config import load

    c = load(ROOT / "config" / "public.yaml")
    dev = load(ROOT / "config" / "askdb.yaml")

    # ---- 数据边界 ----
    assert c.raw["auth"]["enabled"] is True, \
        "登录不能关：它是写操作唯一的身份来源，关掉就只剩管理员令牌挡着"
    # 这一条是这份清单里现在最要紧的。它和"撤掉内置源"是一对改动：
    # 谁把 required 改回 false 而没先把行级边界补回去，公网就读得到两个生产库。
    assert c.raw["auth"]["required"] is True, \
        "运行时数据源上没有租户隔离，读必须要求登录 —— 要改回 false，先把行级边界补回去"
    # 内置源已撤。它若哪天配回来，仍然必须是只读账号 + 口令走环境变量：
    # 断言写成条件式而不是删掉，是为了让"配回来但配错"照样红。
    ds = c.raw.get("datasource")
    assert ds is None or "user=askdb_ro" in ds.get("dsn", ""), \
        "内置源若配回来，必须连只读库账号 —— 护栏是应用层的，库账号是最后一道"
    if ds is not None:
        assert ds.get("password_env"), "数据库口令必须走环境变量"
        assert "password=" not in ds.get("dsn", ""), "连接串里不得写明文口令"
    assert c.raw["observability"]["replay_api"] is False, \
        "回放会返回 SQL 全文，连真实库时必须关"

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
