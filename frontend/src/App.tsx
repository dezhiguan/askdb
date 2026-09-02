import { useEffect, useState } from 'react'
import { AppShell, PageHeader } from './components/AppShell'
import { LoginPanel } from './components/LoginPanel'
import { MockNotice } from './components/MockNotice'
import { ModalLayer } from './components/Modals'
import { QueryWorkspace } from './components/QueryWorkspace'
import { DataSourcesPage } from './pages/DataSourcesPage'
import { AuditPage } from './pages/AuditPage'
import { GlossaryPage } from './pages/GlossaryPage'
import { PermissionsPage } from './pages/PermissionsPage'
import { TasksPage } from './pages/TasksPage'
import { TracesPage } from './pages/TracesPage'
import type { ModalName, View } from './types'
import { fetchMe, logout, type Me } from './api'
import { useHealth } from './useHealth'
import './styles/theme.css'
import './styles/shell.css'
import './styles/components.css'
import './styles/pages.css'
import './styles/traces.css'
import './styles/identity.css'
import './styles/auth.css'
import './styles/responsive.css'

function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [view, setView] = useState<View>('query')
  const [modal, setModal] = useState<ModalName>(null)
  const [toast, setToast] = useState('')
  const health = useHealth()

  // 身份与生效边界。登录/退出后必须重新拉一次 —— 可见表变了，
  // 页面上那些「当前能查什么」的显示不跟着变就是在说谎
  const reloadMe = () => { fetchMe().then(setMe).catch(() => setMe(null)) }
  useEffect(reloadMe, [])

  const notify = (message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(''), 1900)
  }

  const page = (() => {
    if (view === 'query') return <div className="page query-page"><PageHeader eyebrow="Phase 1 · Unified Query" title="查询工作台" description="无需写 SQL，直接描述你想查看的数据。每次查询均使用独立上下文。" action={<button className="secondary" onClick={() => setView('tasks')}>创建复杂任务</button>} /><QueryWorkspace health={health} onNavigate={setView} /></div>
    if (view === 'tasks') return <TasksPage onNavigate={setView} notify={notify} />
    if (view === 'sources') return <DataSourcesPage health={health} />
    if (view === 'permissions') return <PermissionsPage notify={notify} />
    if (view === 'glossary') return <GlossaryPage onNavigate={setView} />
    if (view === 'audit') return <AuditPage />
    if (view === 'traces') return <TracesPage />
    return <AuditPage />
  })()

  return (
    <>
      <AppShell
        activeView={view}
        health={health}
        onNavigate={setView}
        me={me}
        onOpenLogin={() => setLoginOpen(true)}
        onSignOut={() => { logout().then(() => { reloadMe(); notify('已退出，回到匿名可见范围') }) }}
        notice={<MockNotice view={view} />}
      >
        {page}
      </AppShell>
      {loginOpen && me && (
        <LoginPanel
          me={me}
          onClose={() => setLoginOpen(false)}
          onDone={() => { setLoginOpen(false); reloadMe() }}
          notify={notify}
        />
      )}
      <ModalLayer active={modal} onClose={() => setModal(null)} notify={notify} />
      <div className={`toast ${toast ? 'show' : ''}`}>{toast}</div>
    </>
  )
}

export default App
