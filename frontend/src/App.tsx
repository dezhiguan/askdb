import { useEffect, useState } from 'react'
import { writeGuard } from './writeGuard'
import { AppShell, PageHeader } from './components/AppShell'
import { LoginScreen } from './components/LoginScreen'
import { MockNotice } from './components/MockNotice'
import { ModalLayer } from './components/Modals'
import { QueryWorkspace } from './components/QueryWorkspace'
import { DataSourcesPage } from './pages/DataSourcesPage'
import { AuditPage } from './pages/AuditPage'
import { EvaluationPage } from './pages/EvaluationPage'
import { GlossaryPage } from './pages/GlossaryPage'
import { PermissionsPage } from './pages/PermissionsPage'
import { TasksPage } from './pages/TasksPage'
import { TracesPage } from './pages/TracesPage'
import type { ModalName, View } from './types'
import { fetchMe, logout, type Me } from './api'
import { useHealth } from './useHealth'
import { useSources } from './useSources'
import './styles/theme.css'
import './styles/shell.css'
import './styles/components.css'
import './styles/pages.css'
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

/** 「本次浏览已跳过登录」。session 级 —— 关掉标签页就忘掉。 */
const SKIP_LOGIN_KEY = 'askdb.skipLogin.v1'

function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [view, setView] = useState<View>('query')
  const [modal, setModal] = useState<ModalName>(null)
  const [toast, setToast] = useState('')
  // 侧栏「发起快捷查询」重开一次工作台
  const [queryEpoch, setQueryEpoch] = useState(0)
  const health = useHealth()
  const sources = useSources()

  // 身份与生效边界。登录/退出后必须重新拉一次 —— 可见表变了，
  // 页面上那些「当前能查什么」的显示不跟着变就是在说谎
  const reloadMe = () => { fetchMe().then(setMe).catch(() => setMe(null)) }
  useEffect(reloadMe, [])

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
            onClick={() => setView('tasks')}>创建复杂任务</button>}
        />
        {/* key 变化即整块重挂 —— 侧栏「发起快捷查询」照原型要回到空态 */}
        <QueryWorkspace key={queryEpoch} health={health} sources={sources} onNavigate={setView} notify={notify} me={me} />
      </div>
    )
    if (view === 'tasks') return <TasksPage onNavigate={setView} notify={notify} me={me} />
    if (view === 'sources') return <DataSourcesPage health={health} me={me} />
    if (view === 'permissions') return <PermissionsPage notify={notify} me={me} />
    if (view === 'glossary') return <GlossaryPage onNavigate={setView} notify={notify} me={me} />
    if (view === 'audit') return <AuditPage />
    if (view === 'evaluation') return <EvaluationPage />
    if (view === 'traces') return <TracesPage onNavigate={setView} onOpenModal={setModal} me={me} />
    return <AuditPage />
  })()

  return (
    <>
      <AppShell
        activeView={view}
        health={health}
        source={sources.current}
        onNavigate={setView}
        onQuickNew={() => { setQueryEpoch(n => n + 1); setView('query') }}
        me={me}
        onOpenLogin={() => setLoginOpen(true)}
        onSignOut={() => { logout().then(() => { reloadMe(); notify('已退出，回到匿名可见范围') }) }}
        notice={<MockNotice view={view} />}
      >
        {page}
      </AppShell>
      {/* me 还没拿到时**不显示** —— 拿不准是不是要登录就先别糊一扇门上去，
          那会在每次刷新时闪一下。 */}
      {(loginOpen || gated) && me && (
        <LoginScreen
          me={me}
          dismissible={!gated}
          onClose={() => setLoginOpen(false)}
          onDone={() => {
            setLoginOpen(false)
            // 登录成功就不再是"跳过"状态了，清掉标记：下次退出登录时
            // 应当重新落在登录页，而不是被上一次的跳过决定顺延
            sessionStorage.removeItem(SKIP_LOGIN_KEY)
            setSkipped(false)
            reloadMe()
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
    </>
  )
}

export default App
