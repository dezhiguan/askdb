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

    src = (FRONTEND_SRC / "traceSteps.ts").read_text(encoding="utf-8")
    block = re.search(r"export const STEP_NAMES[^{]*\{(.*?)\n\}", src, re.S)
    assert block, "traceSteps.ts 里找不到 STEP_NAMES"
    known = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))

    missing = sorted(traced - known)
    assert not missing, f"这些节点在复放里会显示成原始 id：{missing}"


SOURCES_PAGE = FRONTEND_SRC / "pages" / "DataSourcesPage.tsx"


def test_sources_page_is_wired_to_real_endpoints():
    src = SOURCES_PAGE.read_text(encoding="utf-8")
    for fn in ("fetchSchema", "fetchIntrospect", "fetchSelfCheck"):
        assert fn in src, f"数据源页没有调用 {fn}"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "sources:" not in notices, "数据源页已接真实数据，MockNotice 里的条目要删掉"


def test_plaintext_password_mode_is_gated_by_the_server():
    """表单现在确实收数据库口令（运行时添加数据源）—— 那条路必须由服务端
    有没有配主密钥来决定开不开。

    主密钥缺失时若还让人填明文，口令要么以明文落盘、要么保存时才报错，
    两种都不能接受。所以「直接填密码」这个选项由 can_store_password 控制，
    且推荐项始终是「环境变量名」（口令一个字不落盘）。
    """
    src = (FRONTEND_SRC / "components" / "AddSourceModal.tsx").read_text(encoding="utf-8")
    assert "can_store_password" in src, "明文口令入口没有跟服务端主密钥状态挂钩"
    assert "disabled={!meta.can_store_password}" in src, "主密钥缺失时明文口令项必须禁用"
    assert "useState<'env' | 'plain'>('env')" in src, "默认必须是环境变量名那条路"


def test_credentials_never_touch_browser_storage():
    """口令只在提交那一刻存在于内存。任何持久化到浏览器的动作都会让它
    留在别人的机器上 —— 而这是数据库口令，不是登录态。
    """
    offenders = []
    for path in FRONTEND_SRC.rglob("*.tsx"):
        text = path.read_text(encoding="utf-8")
        if "localStorage" in text or "sessionStorage" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"前端往浏览器存储写了东西，需人工确认不含凭证：{offenders}"


QUERY_WORKSPACE = FRONTEND_SRC / "components" / "QueryWorkspace.tsx"
RESULT_TABS = FRONTEND_SRC / "components" / "ResultTabs.tsx"
TRUST_SIDEBAR = FRONTEND_SRC / "components" / "TrustSidebar.tsx"


def test_query_workspace_is_wired_to_real_endpoints():
    src = QUERY_WORKSPACE.read_text(encoding="utf-8")
    for fn in ("askQuestion", "runSql"):
        assert fn in src, f"查询工作台没有调用 {fn}"
    assert "resumeTask" in RESULT_TABS.read_text(encoding="utf-8"), "断点续跑没有接上"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "query:" not in notices, "查询工作台已接真实数据，MockNotice 里的条目要删掉"


def test_rejection_codes_all_have_plain_language():
    """只把规则号（R-03）甩给用户，等于把排查成本原样丢过去 —— 他不知道那是什么。

    后端新增一种拦截而前端没跟上，页面会显示成"这次查询没能完成"加一串代码，
    没有任何处置建议。这类漂移不报错，所以由测试盯住。
    """
    codes = set()
    for path in (ROOT / "askdb").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        codes |= set(re.findall(r'rejected_by=?\s*=?\s*"([A-Z0-9_-]+)"', text))
        codes |= set(re.findall(r'"rejected_by":\s*"([A-Z0-9_-]+)"', text))
    assert codes, "没扫到任何拦截码，正则或代码结构变了"

    src = RESULT_TABS.read_text(encoding="utf-8")
    block = re.search(r"const RULES[^{]*\{(.*?)\n\}", src, re.S)
    assert block, "ResultTabs 里找不到 RULES"
    known = set(re.findall(r"^\s*'?([A-Z0-9_-]+)'?:", block.group(1), re.M))

    missing = sorted(codes - known)
    assert not missing, f"这些拦截码在结果页没有人话解释：{missing}"


def test_no_fabricated_assurance_claims():
    """原型右栏那套「可信度 96 / SSO · PRODUCT / PROD-RO / MASK · AUDIT / 90 DAYS」
    在 askdb 里一条都不成立：没有账号体系、没有列级脱敏、没有数据期限策略，
    更没有对答案可靠性的评分。

    askdb 保证的是**过程可信**（危险操作可拦、结果附 SQL 可自验、判定可追溯），
    不是**结果可信**。给一个分数等于替用户下了「这个答案有多可靠」的判断。
    """
    src = TRUST_SIDEBAR.read_text(encoding="utf-8")
    for claim in ("PROD-RO", "SSO · PRODUCT", "MASK · AUDIT", "90 DAYS"):
        assert claim not in src, f"右栏还在展示不成立的承诺：{claim}"
