import { Fragment, useEffect, useRef, useState } from 'react'
import { writeGuard } from './writeGuard'
import { AppShell, PageHeader } from './components/AppShell'
import { LoginScreen } from './components/LoginScreen'
import { MockNotice } from './components/MockNotice'
import { ModalLayer } from './components/Modals'
import { DialogProvider } from './components/ConfirmDialog'
import { QueryWorkspace } from './components/QueryWorkspace'
import { DataSourcesPage } from './pages/DataSourcesPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { AuditPage } from './pages/AuditPage'
import { EvaluationPage } from './pages/EvaluationPage'
import { GlossaryPage } from './pages/GlossaryPage'
import { PermissionsPage } from './pages/PermissionsPage'
import { TasksPage } from './pages/TasksPage'
import { TracesPage } from './pages/TracesPage'
import type { ModalName, View } from './types'
import { fetchMe, logout, setLoginRequiredHandler, type Me } from './api'
import { useHealth } from './useHealth'
import { useSources } from './useSources'
import './styles/theme.css'
import './styles/shell.css'
import './styles/components.css'
import './styles/dialog.css'
import './styles/pages.css'
import './styles/result-detail.css'
import './styles/traces.css'
import './styles/evaluation.css'
import './styles/identity.css'
import './styles/auth.css'
import './styles/responsive.css'
/* 各页对齐原型的样式，必须排在通用样式之后 —— 同权重时后者生效 */
import './styles/proto-query.css'
import './styles/proto-tasks.css'
import './styles/proto-sources.css'
import './styles/proto-permissions.css'
import './styles/proto-glossary.css'
import './styles/proto-audit.css'
import './styles/filterbar.css'

/** 「本次浏览已跳过登录」。session 级 —— 关掉标签页就忘掉。 */
const SKIP_LOGIN_KEY = 'askdb.skipLogin.v1'

function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [view, setView] = useState<View>('query')
  /** 跳「执行追踪」时要定位的那条 trace。本站没有 URL 路由（view 是状态），
   *  参数就走这里 —— 侧栏「Agent 执行链路」按的是刚跑完的那一条，
   *  落到追踪页最近一条上等于点了个碰运气的按钮。 */
  const [focusTrace, setFocusTrace] = useState<string | null>(null)
  const [modal, setModal] = useState<ModalName>(null)
  const [toast, setToast] = useState('')
  // 侧栏「发起快捷查询」重开一次工作台
  const [queryEpoch, setQueryEpoch] = useState(0)
  const health = useHealth()
  const sources = useSources(health.status === 'ready' ? health.health.datasource.configured : null)

  // 身份与生效边界。登录/退出后必须重新拉一次 —— 可见表变了，
  // 页面上那些「当前能查什么」的显示不跟着变就是在说谎
  const reloadMe = () => { fetchMe().then(setMe).catch(() => setMe(null)) }
  useEffect(reloadMe, [])

  // 登录成功后用它把当前页整块重挂。会话失效时页面上留着的是一批读失败的
  // 空态与红条，登录只换回了身份，不会让那些已经发过的请求自己再来一次 ——
  // 不重挂就要人再手动刷一次页面，那正是这次要消灭的那步。
  const [sessionEpoch, setSessionEpoch] = useState(0)

  // 会话在页面开着的时候失效（票过期、换了签名密钥、实例把 auth.required
  // 打开了）：接口层统一抛 LoginRequired 并叫到这里。做两件事 —— 重取身份，
  // 摆出登录页。此前没有这条线，后果是每个页面各自把 401 画成一条红色
  // 「读取失败」，看的人会去查一个不存在的故障（2026-09-07 线上就是这样）。
  //
  // 只响应第一次：页面上有几处在轮询（追踪、离线回归），会话一失效它们会
  // 一直撞同一堵墙，每撞一次就重取一次身份、再弹一次登录页纯属噪音。
  // 登录页关掉或登录成功即复位，下一次失效照样能叫醒。
  const sessionLost = useRef(false)
  useEffect(() => {
    setLoginRequiredHandler(() => {
      if (sessionLost.current) return
      sessionLost.current = true
      reloadMe()
      setLoginOpen(true)
    })
    return () => setLoginRequiredHandler(null)
  }, [])

  // 登录页是**落地页**：未登录时一进来就显示它。
  //
  // required 的实例上它是硬门（关不掉，后端也会 401）；不 required 的实例上
  // 它可以被「一键体验」跳过 —— 跳过之后就是未登录身份本身。
  //
  // 跳过记在 sessionStorage 而不是内存：否则每刷新一次就被拦一次，
  // 而这个实例本来就允许未登录查数，拦第二次纯属骚扰。用 session 级而非
  // localStorage，是因为"这次来访不想登录"不该变成一个永久决定。
  const [skipped, setSkipped] = useState(
    () => sessionStorage.getItem(SKIP_LOGIN_KEY) === '1',
  )
  const needsIdentity = !!me && me.enabled && !me.username
  const gated = needsIdentity && (me.required || !skipped)

  /** 统一的换页入口。第二个参数是**带去目标页的一小段内容**，含义由目标页定：
   *    · 执行追踪 —— 要定位的 trace_id
   *    · 查询页   —— 预填进输入框的问题原文（任务中心「换个问法」带过来的）
   *  不带就清掉 —— 否则从导航栏点进去还会停在上一次那条上。
   *
   *  查询页要**重挂**才吃得到预填（question 是它的初始状态），所以这里顺带
   *  推一下 queryEpoch。不推的话第二次带不同的问题过来，输入框纹丝不动。 */
  const navigate = (next: View, focus?: string) => {
    setFocusTrace(focus ?? null)
    if (next === 'query' && focus) setQueryEpoch(n => n + 1)
    setView(next)
  }

  const notify = (message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(''), 1900)
  }

  const page = (() => {
    if (view === 'query') return (
      <div className="page query-page">
        <PageHeader
          title="查询 Agent"
          description="无需写 SQL，直接描述你想查看的数据。每次查询均使用独立上下文。"
          /* 这只是跳转，但它通向的「创建任务」要登录 —— 入口和目的地状态不一致，
             会让人点进去才发现做不了 */
          action={<button className="secondary" {...writeGuard(me, '创建任务').props}
            onClick={() => navigate('tasks')}>创建复杂任务</button>}
        />
        {/* key 变化即整块重挂 —— 侧栏「发起快捷查询」照原型要回到空态 */}
        <QueryWorkspace key={queryEpoch} health={health} sources={sources} onNavigate={navigate}
                        notify={notify} me={me} prefill={focusTrace ?? ''} />
      </div>
    )
    if (view === 'tasks') return <TasksPage onNavigate={navigate} notify={notify} me={me} />
    if (view === 'sources') return <DataSourcesPage health={health} me={me} />
    if (view === 'permissions') return <PermissionsPage notify={notify} me={me} />
    if (view === 'glossary') return <GlossaryPage onNavigate={navigate} notify={notify} me={me} sources={sources} />
    if (view === 'approvals') return <ApprovalsPage notify={notify} />
    if (view === 'audit') return <AuditPage me={me} onNavigate={navigate} />
    if (view === 'evaluation') return <EvaluationPage onNavigate={navigate} me={me} onOpenLogin={() => setLoginOpen(true)} />
    // key 带上 focusTrace：已经停在这一页时再定位另一条，靠重挂让左栏
    // 的搜索框与选中项一起复位（它们是页内初始状态）
    if (view === 'traces') return <TracesPage key={focusTrace ?? ''} focusTrace={focusTrace}
                                              onNavigate={navigate} onOpenModal={setModal} me={me} />
    return <AuditPage me={me} onNavigate={navigate} />
  })()

  return (
    /* 全局确认弹窗挂在最外层：站内任何一处要问"确定吗"都走 useDialog()，
       不再有 window.confirm —— 那个框顶着域名当标题，也排不出后果清单。 */
    <DialogProvider>
      <AppShell
        activeView={view}
        health={health}
        source={sources.current}
        onNavigate={navigate}
        onQuickNew={() => { setQueryEpoch(n => n + 1); navigate('query') }}
        me={me}
        onOpenLogin={() => setLoginOpen(true)}
        onSignOut={() => { logout().then(() => { reloadMe(); notify('已退出，回到匿名可见范围') }) }}
        notice={<MockNotice view={view} />}
      >
        {/* key 变了就整页重挂 —— 重新登录之后各页自己去把数据取回来。
            Fragment 上挂 key 是为了不额外插一层 DOM 把栅格挤变形 */}
        <Fragment key={sessionEpoch}>{page}</Fragment>
      </AppShell>
      {/* me 还没拿到时**不显示** —— 拿不准是不是要登录就先别糊一扇门上去，
          那会在每次刷新时闪一下。 */}
      {(loginOpen || gated) && me && (
        <LoginScreen
          me={me}
          dismissible={!gated}
          onClose={() => { sessionLost.current = false; setLoginOpen(false) }}
          onDone={() => {
            sessionLost.current = false
            setLoginOpen(false)
            // 登录成功就不再是"跳过"状态了，清掉标记：下次退出登录时
            // 应当重新落在登录页，而不是被上一次的跳过决定顺延
            sessionStorage.removeItem(SKIP_LOGIN_KEY)
            setSkipped(false)
            reloadMe()
            setSessionEpoch(n => n + 1)
          }}
          onSkip={() => {
            sessionStorage.setItem(SKIP_LOGIN_KEY, '1')
            setSkipped(true)
            setLoginOpen(false)
          }}
          notify={notify}
        />
      )}
      <ModalLayer active={modal} onClose={() => setModal(null)} notify={notify} />
      <div className={`toast ${toast ? 'show' : ''}`}>{toast}</div>
    </DialogProvider>
  )
}

export default App
