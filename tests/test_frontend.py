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


def _code_only(path: Path) -> str:
    """去掉注释后的源码。

    扫描类断言只该看**代码**。注释里为了解释「为什么不这么做」而写下
    localStorage、REDACTED 这类词，会把自己的说明当成违规命中 ——
    已经被绊倒两次了。剥注释比反复改措辞可靠。

    `//` 只在不是 `://` 的情况下才当行注释 —— 否则 https:// 的后半截会被吃掉。
    这只挡住协议头这一种情形：字符串里出现 `a//b` 仍会被当成注释起点，
    该行后半段随之丢失。对这些扫描断言够用（要找的标识符本身出现在代码里，
    不会藏在这种串里），但别把它当成通用的注释解析器。
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)          # 块注释，含 JSX 里的 {/* */}
    text = re.sub(r"(?<!:)//[^\n]*", "", text)            # 行注释，放过 ://
    return text

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
    # 走 _code_only：注释里为了写清"刻意不用 dangerouslySetInnerHTML"
    # 而提到这个词，不该被当成违规命中 —— 这已经是第三次被自己的说明绊倒
    offenders = [
        str(p.relative_to(ROOT))
        for p in FRONTEND_SRC.rglob("*.tsx")
        if "dangerouslySetInnerHTML" in _code_only(p)
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
    """口令只在提交那一刻存在于内存 —— 它是数据库口令，不是登录态，
    落到浏览器就等于留在别人的机器上。

    原来这条禁止**任何**组件碰浏览器存储，当时前端确实一处都没用。
    「最近查询」上线后那是正当用途（存的是自己问过的问题，不是凭证），
    规则因此收窄到真正要守的那条线：**经手凭证的组件不许碰存储**，
    其余组件可以用，但不得存下凭证字段名。
    """
    credential_marks = ("password", "password_env", "dsn")
    offenders = []
    for path in FRONTEND_SRC.rglob("*.tsx"):
        text = _code_only(path)
        if "localStorage" not in text and "sessionStorage" not in text:
            continue
        # 同一个文件里既碰存储又经手凭证 —— 不管它实际存了什么，都要人来看一眼
        if any(m in text for m in credential_marks):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"经手凭证的组件碰了浏览器存储：{offenders}"


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

    # 词表已抽到 src/rules.ts —— 结果页与任务中心显示的是同一个 rejected_by，
    # 各存一份必然漂
    src = (FRONTEND_SRC / "rules.ts").read_text(encoding="utf-8")
    block = re.search(r"const RULES[^{]*\{(.*?)\n\}", src, re.S)
    assert block, "src/rules.ts 里找不到 RULES"
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
    src = _code_only(TRUST_SIDEBAR)
    # SSO · PRODUCT / MASK · AUDIT / 90 DAYS 在 askdb 里没有任何真实来源，
    # 一律不许出现。PROD-RO 例外：右栏随数据源联动后，它取自运行时数据源
    # **自己声明的环境**（env=prod_ro），是真数据不是承诺 ——
    # 所以改判"它有没有跟着 env 走"，而不是"这几个字出现没出现"。
    for claim in ("SSO · PRODUCT", "MASK · AUDIT", "90 DAYS"):
        assert claim not in src, f"右栏还在展示不成立的承诺：{claim}"
    if "PROD-RO" in src:
        assert "prod_ro" in src, "PROD-RO 必须由数据源声明的 env 推出，不能写死"

def test_observability_deep_link_is_gated_by_reachability():
    """观测后端的深链必须先过可达性判定，不能照着配置直接渲染成外链。

    自托管的 Langfuse 只在内网活着，对外实例上 tracing.url 就是
    `http://localhost:3000` —— 那是给部署方挂了 SSH 隧道之后用的。直接当外链
    给出去，访客点下去打的是**他自己机器的 3000 端口**；而 3000 是 Next.js /
    Grafana 这类的默认端口，运气不好会打开他本机碰巧在跑的东西。
    比"点了没反应"更糟，而且不报任何错。
    """
    src = _code_only(FRONTEND_SRC / "api.ts")
    assert "export function tracingReachable" in src, "缺少可达性判定"

    block = re.search(r"export function tracingLink[\s\S]*?\n\}", src)
    assert block, "api.ts 里找不到 tracingLink"
    assert "tracingReachable" in block.group(0), (
        "tracingLink 没有过可达性判定 —— 内网地址会被当成外链给访客"
    )

PERMISSIONS_PAGE = FRONTEND_SRC / "pages" / "PermissionsPage.tsx"


def test_permissions_page_is_wired_to_real_endpoints():
    src = PERMISSIONS_PAGE.read_text(encoding="utf-8")
    for fn in ("fetchRoles", "fetchMembers", "addMember", "removeMember"):
        assert fn in src, f"身份与权限页没有调用 {fn}"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "permissions:" not in notices, "身份与权限页已接真实数据，MockNotice 里的条目要删掉"


def test_permissions_page_does_not_claim_roles_are_inert():
    """角色已真正参与执行判定：server 的 _scoped 走 _auth.roles_of →
    _identity.for_roles，按角色收窄本次查询可见的表与行上限。

    页面早先有一条"角色目前不参与执行判定 / 尚未接入登录"的横幅，那是登录
    与收窄落地之前的状态。功能上线后它就说反了 —— 会让人以为把某人加进角色
    没有任何效果。这条测试钉住"过期声明不得回潮"，防止有人日后又抄回来。
    """
    src = PERMISSIONS_PAGE.read_text(encoding="utf-8")
    for stale in ("不参与执行判定", "尚未接入登录", "对所有调用一视同仁"):
        assert stale not in src, f"页面还留着过期声明：{stale}"


def test_admin_token_never_persisted_in_browser_storage():
    """管理员令牌是部署方持有的共享口令，写进 localStorage 等于把它
    长期留在浏览器里。只准留在内存，刷新即失效。
    """
    offenders = []
    for path in list(FRONTEND_SRC.rglob("*.tsx")) + list(FRONTEND_SRC.rglob("*.ts")):
        text = _code_only(path)
        if "Admin-Token" in text or "adminWrite" in text:
            if "localStorage" in text or "sessionStorage" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"管理员令牌被存进了浏览器存储：{offenders}"


def test_no_undefined_css_variables():
    """用了主题里没定义的变量，整条声明会静默失效。

    实际踩过：勾选框写成 `border: 1px solid var(--axis)`，而 --axis 是旧单文件
    页面的变量、React 主题里没有 —— 颜色回退成 currentColor，同一条规则里
    又是 color: white，于是白框画在近白底上，字面意义的隐形。
    页面不报错、样式不报错，只是那个框看不见了。
    """
    styles = FRONTEND_SRC / "styles"
    defined = set()
    for path in styles.glob("*.css"):
        defined |= set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", path.read_text(encoding="utf-8"), re.M))
    assert defined, "没扫到任何变量定义，目录结构变了"

    # 行内 style 里传进去的，且用处都带了兜底值
    inline = {"--ratio"}

    # 只看代码：这条测试的说明文字里就写着 var(--axis)，
    # 不剥注释的话它会把自己的病历当成病灶
    used = set()
    for path in [*styles.glob("*.css"), *FRONTEND_SRC.rglob("*.tsx")]:
        used |= set(re.findall(r"var\((--[a-z0-9-]+)", _code_only(path)))

    missing = sorted(used - defined - inline)
    assert not missing, f"用到了主题里没有的 CSS 变量，这些声明会静默失效：{missing}"


def test_answer_card_states_only_what_the_data_says():
    """原型的结论卡写的是一句自然语言结论（"今天共有 18 笔支付失败订单，
    相比昨日同期下降 14.3%"）。askdb **不产出结论散文** —— 它返回行，
    附带 SQL 让人自验；那句同比更是设计稿的虚构，后端没有任何同比口径。

    所以结论行只能由结果本身推出来：单值念值、多行报行数。
    这条测试挡的是"照着原型把那句话抄进来"。
    """
    src = _code_only(RESULT_TABS)
    for invented in ("相比昨日", "同比", "较昨日", "环比"):
        assert invented not in src, f"结论卡出现了后端算不出来的口径：{invented}"
    assert "result.row_count === 1" in src, "单值结果应当直接把值念出来"


def test_evidence_strip_fields_come_from_the_response():
    """四格事实条的每一格都要有真实来源，不能留装饰位。"""
    src = _code_only(RESULT_TABS)
    for field in ("result.trace_id", "result.as_of", "result.explain_rows", "useSqlDigest"):
        assert field in src, f"事实条缺少真实来源：{field}"


def test_sql_is_highlighted_without_raw_html():
    """SQL 里带着数据库对象名。拼进 innerHTML 就是把转义责任交给自己，
    而 React 默认转义本来就是对的 —— 高亮走分词渲染成元素，
    多几行代码换掉一整类注入面。
    """
    src = _code_only(RESULT_TABS)
    assert "tokenizeSql" in src, "SQL 高亮没有走分词"
    assert "dangerouslySetInnerHTML" not in src


def test_sql_toolbar_does_not_claim_the_sql_is_unmodified():
    """原型那行写死「未经格式改写」。askdb 的最终 SQL 恰恰是被护栏改写过的
    （注入租户谓词、补 LIMIT、展开 SELECT *）—— 照抄就是撒谎，
    而这条 SQL 是让人拿去自验的，说错了整套自验就失效。
    """
    src = _code_only(RESULT_TABS)
    assert "已按护栏改写" in src, "改写过的 SQL 必须如实标注"
    assert "result.rewrites" in src, "改写标注要由真实的 rewrites 决定"
def test_trace_page_does_not_depend_on_replay():
    """执行追踪页整页都不许挂在 /api/replay 上。

    回放要登录、要 observability.replay_api 开关，而连真实数据源的实例默认
    关着 —— 挂上去的结果是这一页在它**最常见**的形态下右半屏全是占位符、
    Span 表一行没有，而这一页存在的全部意义就是把链路显出来。节点链改走
    /api/trace（双白名单，SQL 只以哈希出现），六格与 Span 表因此在未登录、
    回放关闭时照样是满的。

    2026-09-06 起事实网格严格照原型的六格（总耗时/模型/Token/工具调用/
    SQL Hash/数据源），角色、轮次、成本等格子已撤 —— 那些是原型没有的字段。
    """
    src = _code_only(FRONTEND_SRC / "pages" / "TracesPage.tsx")

    assert "fetchTraceChain" in src, "执行追踪页没有走 /api/trace"
    assert "fetchReplay" not in src, "执行追踪页又挂回 /api/replay 了（要登录+开关，默认取不到）"

    head = src[src.index("function TraceDetail("):src.index("function TraceNodes(")]
    for field in ("item.elapsed_ms", "item.trace_id", "item.kind", "chain?.model", "chain?.sql_hash"):
        assert field in head, f"详情区事实网格没有用 {field}"

    # 钉的是「取不到就整块消失」这一类退化，不管它退化的判据是什么
    for early in ("if (!chain)", "if (!replay)", "if (!replayOn)"):
        assert early not in head, f"标题/事实网格前有提前返回（{early}），整块会被一起吞掉"

    # 链路条与 Span 表同理：没有步骤时按原型版式留空表，
    # 而不是把这两段换成一段说明文字 —— 页面形态要和原型一致。
    nodes = src[src.index("function TraceNodes("):src.index("function hex16(")]
    assert "Span 明细" in nodes, "没有渲染 Span 表版式"
    assert "steps.length === 0" not in nodes, "空态被换成了另一种展示，不再是原型的空表"


def test_model_step_classification_matches_the_frontend():
    """「模型调用成功率」按模型**节点**算，而节点归类前后端各存一份。

    后端 audit.MODEL_STEPS 拿来算数，前端 traceSteps.STEP_TYPE 拿来在 Span 表上
    标 MODEL —— 两份漂了不会报错，只会让那格百分比和页面上标 MODEL 的行对不上，
    而这正是"数字经不经得起对账"要防的事。
    """
    from askdb.audit import MODEL_STEPS

    src = (FRONTEND_SRC / "traceSteps.ts").read_text(encoding="utf-8")
    block = re.search(r"export const STEP_TYPE[^{]*\{(.*?)\n\}", src, re.S)
    assert block, "traceSteps.ts 里找不到 STEP_TYPE"
    front = {name for name, kind in
             re.findall(r"^\s*([a-z_]+):\s*'([A-Z]+)'", block.group(1), re.M)
             if kind == "MODEL"}
    assert front == set(MODEL_STEPS), (
        f"模型节点归类前后端不一致：前端 {sorted(front)} / 后端 {sorted(MODEL_STEPS)}")


def test_removed_pages_leave_no_dangling_references():
    """Connector 节点 / 开发者工具 / 产品落地路线 已移除（2026-09-02）。

    删页面最容易留下的是**引用残渣**：导航里还有条目但组件没了（点了白屏）、
    View 类型里还留着值（写错也不报错）、CSS 里一堆没人用的规则（下次改样式
    的人以为还在用）。这条把三类残渣一起挡住。
    """
    names = ("ConnectorsPage", "DeveloperPage", "RoadmapPage")
    values = ("'connectors'", "'developer'", "'roadmap'")

    offenders = []
    for path in list(FRONTEND_SRC.rglob("*.tsx")) + list(FRONTEND_SRC.rglob("*.ts")):
        text = _code_only(path)
        hit = [n for n in names + values if n in text]
        if hit:
            offenders.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not offenders, f"已删页面仍被引用：{offenders}"

    # 孤儿样式按**实际引用**判，不钉死类名清单 ——
    # .code-line 原本在这张清单里，但审计页的 `askdb replay` 提示条仍在用它，
    # 于是这条断言变成了假阳性。改成"没人引用才算孤儿"，
    # 既保住本意，也不会在类被复用时误伤。
    css = "\n".join(p.read_text(encoding="utf-8") for p in (FRONTEND_SRC / "styles").glob("*.css"))
    tsx = "\n".join(_code_only(p) for p in FRONTEND_SRC.rglob("*.tsx"))
    orphans = [
        cls for cls in (".connector-card", ".tool-card", ".roadmap", ".phase-detail", ".code-line")
        if cls in css and cls.lstrip(".") not in tsx
    ]
    assert not orphans, f"这些样式已经没人引用，随页面一起删掉：{orphans}"


def test_tasks_page_is_wired_and_promises_no_approval_flow():
    """任务中心已接 /api/tasks。

    这页原本有一张说明卡写明「没有任务队列」「审批流还没有」，2026-09-03 按
    @guandezhi 决定移除。断言跟着改，但**守的东西不变**：原型讲的是
    「缺少时间范围 → 任务暂停 → 补充输入后继续」，那是产品化的澄清流程；
    askdb 的中断是故障恢复（进程挂了、递归超限、检查点异常）。混为一谈会让人
    以为能靠它做人工介入与审批。

    说明卡没了之后，唯一还会暗示审批的地方是左导航副标题 —— 守在那里。

    2026-09-06 起 askdb **确实有了**高成本查询审批（P07：预估扫描超阈值 →
    挂起 → 系统管理员放行 → 发起人重跑一次）。所以断言不再笼统地禁"审批"
    二字，而是收紧到它真正要守的那一点：**任务中心那一项**不能这么承诺。
    任务中断是故障恢复，与审批是两件事，混起来会让人以为中断的任务可以靠
    人工补充输入接着跑 —— 那个能力至今不存在。
    """
    src = _code_only(FRONTEND_SRC / "pages" / "TasksPage.tsx")
    assert "fetchTasks" in src and "resumeTask" in src

    nav = _code_only(FRONTEND_SRC / "data" / "mockData.ts")
    tasks_entry = next(line for line in nav.splitlines() if "view: 'tasks'" in line)
    assert "审批" not in tasks_entry, "任务中断是故障恢复，不是审批流，导航不该这么承诺"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "tasks:" not in notices, "任务中心已接真实数据，MockNotice 里的条目要删掉"


GLOSSARY_PAGE = FRONTEND_SRC / "pages" / "GlossaryPage.tsx"


def test_glossary_page_is_wired_to_real_endpoints():
    src = GLOSSARY_PAGE.read_text(encoding="utf-8")
    assert "fetchSchema" in src, "业务口径页没有调用 fetchSchema"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "glossary:" not in notices, "业务口径页已接真实数据，MockNotice 里的条目要删掉"


def test_glossary_invents_no_governance_metadata():
    """原型的口径详情写着「VERIFIED METRIC · FINANCE · v3.2 · 已认证 · 更新时间」。

    askdb 的口径模型里只有 name / aliases / scope / expr|predicate / note / owner ——
    域、版本、认证状态、更新时间四样都不存在。写上去会让人以为背后有一套
    评审流程，而据此判断"这条口径可不可信"正是这页存在的意义。
    """
    src = _code_only(GLOSSARY_PAGE)
    # 挡的是**编出来的值**，不是标了名的空位。按原型排版留一格显示「—」
    # 是诚实的（读的人看得出这里没有数据）；写上 "v3.2 已认证" 才是撒谎。
    for invented in ("已认证", "VERIFIED", "v3.2"):
        assert invented not in src, f"业务口径页出现了后端没有的治理元数据：{invented}"


def test_glossary_has_no_fake_metric_editor():
    """页面上「提交后仅加入本地列表」的新建表单必须不存在。

    它不调后端、刷新即消失、对真实查询零影响，却和真实口径进同一个列表、
    同一个详情页。用户填完 SQL 定义与同义词，得到一句"已加入"，很容易以为
    配好了 —— 而下一次查询仍然按模型自己的理解算，正是这个功能要防的那件事。
    制造"我已经定义了口径"的错觉，比没有这个按钮危险。
    """
    src = _code_only(GLOSSARY_PAGE)
    assert "localMetrics" not in src, "口径页仍在往本地数组塞假指标"
    assert not (FRONTEND_SRC / "components" / "AddMetricModal.tsx").exists(), \
        "假的新建指标表单还在"


def test_glossary_surfaces_discrimination():
    """区分度是这页唯一无法靠翻配置文件替代的东西 —— 口径写错不报错、
    不越权，护栏一条都不触发，它自己必须有别的方式被检验。"""
    src = _code_only(GLOSSARY_PAGE)
    assert "checkMetrics" in src, "没有接区分度核对接口"
    assert "metric.grain" in src, "粒度没有在详情里展示"


EVALUATION_PAGE = FRONTEND_SRC / "pages" / "EvaluationPage.tsx"


def test_quality_center_is_wired_to_real_endpoints():
    src = _code_only(EVALUATION_PAGE)
    for fn in ("fetchLiveQuality", "fetchOfflineQuality"):
        assert fn in src, f"质量中心没有调用 {fn}"

    notices = (FRONTEND_SRC / "components" / "MockNotice.tsx").read_text(encoding="utf-8")
    assert "evaluation:" not in notices, "质量中心已接真实数据，MockNotice 里的条目要删掉"


def test_quality_center_invents_no_composite_score():
    """设计稿有「离线质量分 92.9/100」「综合健康分 97.6/100」与「允许发布」门禁。

    那几个数在设计稿里是**写死的**，没有任何来源，页面上的同名指标必须由
    真实数据算出来。

    版本号是**有意的例外**（2026-09-06 产品决定）：askdb 没有对外的 Agent 版本
    概念，但这一格要按设计稿显示。例外的代价是它随时可能与实际跑的东西对不上，
    所以约束改成"只准有一处、且集中声明" —— 版本号必须是 AGENT_VERSION 这个
    单一常量，不许散落成字面量。接上真正的版本来源时只需要换掉那一个常量。

    「允许发布」不在禁用之列，但有条件：它现在由 /api/eval 的 score.pass
    算出来（overall 与门禁比大小），页面只是显示。所以这里不禁字面量，
    而是钉住那条绑定 —— 一旦有人把它改回常量，判语又变成凭空下的结论。
    """
    src = _code_only(EVALUATION_PAGE)
    for invented in ("92.9", "97.6", "4,286", "126 CASES"):
        assert invented not in src, f"质量中心出现了没有来源的数字或判语：{invented}"

    assert src.count("Agent v") <= 1, "版本号散落成多处字面量，改来源时必然漏改"
    if "Agent v" in src:
        assert "const AGENT_VERSION" in src, (
            "写死的版本号必须收进 AGENT_VERSION 常量，接上真实来源时只改这一处"
        )

    if "允许发布" in src:
        assert "sc.pass ?" in src or ".pass ?" in src, (
            "「允许发布」必须由 score.pass 算出来，不能写成固定文案"
        )


def test_quality_center_shows_where_the_score_came_from():
    """同一份代码会部署成多个实例。拿别的库跑出来的成绩当本实例的，
    比没有成绩更糟 —— /api/eval 一直返回 provenance.matches_current，
    这一页必须把它显示出来。
    """
    src = _code_only(EVALUATION_PAGE)
    assert "matches_current" in src, "没有显示成绩出处是否与当前数据源一致"


def test_quality_center_does_not_treat_unrun_cases_as_passed():
    """盲测只跑黄金集的一部分。把没跑到的算成"没失败所以通过"，
    会让覆盖面与通过率一起虚高。"""
    src = _code_only(EVALUATION_PAGE)
    assert "c.passed === null" in src, "未跑用例必须与通过区分开"
