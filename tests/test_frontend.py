"""前端工程与构建产物的约束。

换壳后页面主体在 React bundle 里，旧的「读 index.html 抓字符串」那套
断言不再适用。这里钉住的是换了形态之后仍然要成立的东西：

  · 产物必须跟着源码一起提交 —— Dockerfile 只 COPY askdb/，镜像里没有 node，
    忘了 build 就等于线上停留在上一版界面，而且没有任何报错
  · 后端访问只走 src/api.ts —— 组件里散落 fetch 会让接口契约无处可查
  · 不用 dangerouslySetInnerHTML —— React 默认转义是现在唯一的 XSS 防线，
    旧页面那条 esc() 的防线随单文件页一起退到了 /legacy
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from askdb import server

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "askdb" / "web"
FRONTEND_SRC = ROOT / "frontend" / "src"


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_build_output_is_committed():
    """产物缺失不会报错，只会让人看到上一版界面 —— 必须由测试挡住。"""
    page = WEB / "index.html"
    assert page.is_file(), "askdb/web/index.html 不存在：改完前端要 npm run build 并提交产物"

    html = page.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    assert refs, "构建产物没有引用任何 /assets 资源，index.html 可能不是 vite 产出的"

    for ref in refs:
        asset = WEB / ref.lstrip("/")
        assert asset.is_file(), f"页面引用了不存在的资源 {ref}（产物没提交全）"


def test_assets_are_actually_served(client):
    """只加路由不挂静态目录，页面会白屏而接口全绿 —— 这种故障最难查。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    ref = re.search(r'src="(/assets/[^"]+\.js)"', html)
    assert ref, "找不到入口 JS"

    r = client.get(ref.group(1))
    assert r.status_code == 200 and len(r.content) > 0


def test_legacy_page_stays_reachable(client):
    """旧界面是目前唯一接了真实数据的界面。新前端把能力接回来之前，
    它必须一直可达 —— 否则线上只剩一个查不了数的壳。
    """
    r = client.get("/legacy")
    assert r.status_code == 200
    assert "HEALTH.config" in r.text


def test_backend_access_goes_through_api_layer():
    src_files = [p for p in FRONTEND_SRC.rglob("*.ts*") if p.name != "api.ts"]
    offenders = [
        str(p.relative_to(ROOT))
        for p in src_files
        if re.search(r"\bfetch\s*\(", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"组件里不要直接 fetch，统一走 src/api.ts：{offenders}"


def test_no_dangerous_html_injection():
    offenders = [
        str(p.relative_to(ROOT))
        for p in FRONTEND_SRC.rglob("*.tsx")
        if "dangerouslySetInnerHTML" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"用了 dangerouslySetInnerHTML，绕过 React 转义：{offenders}"


AUDIT_PAGE = FRONTEND_SRC / "pages" / "AuditPage.tsx"


def test_audit_page_is_wired_to_real_endpoints():
    """审计页已接后端。接完必须同时撤掉样例数据声明 ——
    留着会让真实数据顶着一条「本页不是真实数据」的横幅，比漏加更误导。
    """
    src = AUDIT_PAGE.read_text(encoding="utf-8")
    for fn in ("fetchAudit", "fetchAuditStats", "fetchReplay"):
        assert fn in src, f"审计页没有调用 {fn}"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "audit:" not in notices, "审计页已接真实数据，MockNotice 里的条目要删掉"


def test_step_names_cover_every_traced_node():
    """后端加了新节点、前端没跟上，复放里就会显示 `assess` 这种原始 id。

    这类漂移没有任何报错，只是看着眼生 —— 实际已经发生过：旧页面漏了
    assess / plan / quota / interrupted 四个。
    """
    traced = set()
    for path in (ROOT / "askdb").glob("*.py"):
        traced |= set(re.findall(r'tracer\.add\("([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert traced, "没扫到任何 tracer.add，正则或代码结构变了"

    src = AUDIT_PAGE.read_text(encoding="utf-8")
    block = re.search(r"const STEP_NAMES[^{]*\{(.*?)\n\}", src, re.S)
    assert block, "AuditPage 里找不到 STEP_NAMES"
    known = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))

    missing = sorted(traced - known)
    assert not missing, f"这些节点在复放里会显示成原始 id：{missing}"
