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
    """口令只在提交那一刻存在于内存。任何持久化到浏览器的动作都会让它
    留在别人的机器上 —— 而这是数据库口令，不是登录态。
    """
    offenders = []
    for path in FRONTEND_SRC.rglob("*.tsx"):
        text = _code_only(path)
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
def test_trace_detail_does_not_depend_on_replay_for_the_basics():
    """执行追踪的详情区：标题与事实网格必须只用审计流水里的字段。

    回放（observability.replay_api）在连真实数据源的实例上**默认关闭** ——
    那才是常态。一旦把整个详情区做成"取不到回放就只显示一句话"，
    右半屏就是一片空白，看起来像页面坏了；而耗时、角色、轮次、成本
    这些字段流水里本来就有，不该跟着一起消失。
    """
    src = _code_only(FRONTEND_SRC / "pages" / "TracesPage.tsx")

    head = src[src.index("function TraceDetail("):src.index("function TraceNodes(")]
    for field in ("item.elapsed_ms", "item.attempts", "item.cost_cny", "item.trace_id"):
        assert field in head, f"详情区标题/事实网格没有用 {field}，可能又挂到回放上了"

    # 钉的是「不能因为取不到回放就提前 return」，而不是「不许出现 replayOn」——
    # 它作为参数往下传给链路条那一段是正常的
    for early in ("if (!replayOn)", "if (!replay)", "if (!replay "):
        assert early not in head, f"标题/事实网格前有基于回放的提前返回（{early}），整块会被一起吞掉"
    assert "if (!replayOn)" in src[src.index("function TraceNodes("):], \
        "链路条那一段没有处理回放未开启的情况"


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

    css = "\n".join(p.read_text(encoding="utf-8") for p in (FRONTEND_SRC / "styles").glob("*.css"))
    for cls in (".connector-card", ".tool-card", ".roadmap", ".phase-detail", ".code-line"):
        assert cls not in css, f"{cls} 只服务已删页面，样式该一并清掉"


def test_tasks_page_is_wired_and_promises_no_approval_flow():
    """任务中心已接 /api/tasks。

    这页原本有一张说明卡写明「没有任务队列」「审批流还没有」，2026-09-03 按
    @guandezhi 决定移除。断言跟着改，但**守的东西不变**：原型讲的是
    「缺少时间范围 → 任务暂停 → 补充输入后继续」，那是产品化的澄清流程；
    askdb 的中断是故障恢复（进程挂了、递归超限、检查点异常）。混为一谈会让人
    以为能靠它做人工介入与审批。

    说明卡没了之后，唯一还会暗示审批的地方是左导航副标题 —— 守在那里。
    """
    src = _code_only(FRONTEND_SRC / "pages" / "TasksPage.tsx")
    assert "fetchTasks" in src and "resumeTask" in src

    nav = _code_only(FRONTEND_SRC / "data" / "mockData.ts")
    assert "审批" not in nav, "askdb 没有审批流，导航不该这么承诺"

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
    for invented in ("已认证", "VERIFIED", "v3.2", "更新时间", "当前版本"):
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
