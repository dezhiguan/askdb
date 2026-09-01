import { useState } from 'react'
import { AppShell, Brand, PageHeader } from './components/AppShell'
import { MockNotice } from './components/MockNotice'
import { ModalLayer } from './components/Modals'
import { QueryWorkspace } from './components/QueryWorkspace'
import { sources } from './data/mockData'
import { DataSourcesPage } from './pages/DataSourcesPage'
import { AuditPage } from './pages/AuditPage'
import { GlossaryPage, PermissionsPage } from './pages/GovernancePages'
import { ConnectorsPage, DeveloperPage, RoadmapPage } from './pages/ScalePages'
import { TasksPage } from './pages/TasksPage'
import { TracesPage } from './pages/TracesPage'
import type { DataSource, ModalName, View } from './types'
import { useHealth } from './useHealth'
import './styles/theme.css'
import './styles/shell.css'
import './styles/components.css'
import './styles/pages.css'
import './styles/responsive.css'

function App() {
  const [entered, setEntered] = useState(false)
  const [view, setView] = useState<View>('query')
  const [source, setSource] = useState<DataSource>(sources[0])
  const [modal, setModal] = useState<ModalName>(null)
  const [toast, setToast] = useState('')
  const health = useHealth()

  const notify = (message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(''), 1900)
  }

  const page = (() => {
    if (view === 'query') return <div className="page query-page"><PageHeader eyebrow="Phase 1 · Unified Query" title="查询工作台" description="无需写 SQL，直接描述你想查看的数据。每次查询均使用独立上下文。" action={<button className="secondary" onClick={() => setView('tasks')}>创建复杂任务</button>} /><QueryWorkspace source={source} setSource={setSource} onNavigate={setView} onClarify={() => setModal('clarification')} notify={notify} /></div>
    if (view === 'tasks') return <TasksPage onClarify={() => setModal('clarification')} notify={notify} />
    if (view === 'sources') return <DataSourcesPage health={health} />
    if (view === 'permissions') return <PermissionsPage notify={notify} />
    if (view === 'glossary') return <GlossaryPage notify={notify} />
    if (view === 'audit') return <AuditPage />
    if (view === 'traces') return <TracesPage openModal={setModal} notify={notify} />
    if (view === 'connectors') return <ConnectorsPage openModal={setModal} />
    if (view === 'developer') return <DeveloperPage notify={notify} />
    return <RoadmapPage notify={notify} />
  })()

  return (
    <>
      {!entered && <EntryScreen onEnter={() => setEntered(true)} notify={notify} />}
      <AppShell
        activeView={view}
        health={health}
        onNavigate={setView}
        onSignOut={() => setEntered(false)}
        notice={<MockNotice view={view} />}
      >
        {page}
      </AppShell>
      <ModalLayer active={modal} onClose={() => setModal(null)} notify={notify} />
      <div className={`toast ${toast ? 'show' : ''}`}>{toast}</div>
    </>
  )
}

/** 入口页。
 *
 * 保留了原型的门面，但**没有把它做成一道假门禁**：askdb 目前不设账号体系，
 * 权限边界是数据库连接本身。装出「已登录 / 已鉴权」的样子，会让人以为
 * 界面背后有访问控制 —— 那比没有访问控制更危险，对外实例尤其如此。
 * 所以企业 SSO 与短时令牌都是明确的禁用态，并写明归属阶段。
 */
function EntryScreen({ onEnter, notify }: { onEnter: () => void; notify: (message: string) => void }) {
  return (
    <div className="login-screen">
      <div className="login-layout">
        <section className="login-story">
          <Brand />
          <h1>企业数据，<br />从可信提问开始。</h1>
          <p>统一的自然语言查数入口。数据留在企业内网，只读护栏、租户隔离与全链路审计贯穿每一次查询。</p>
          <div><span>服务端部署</span><span>数据库凭证不出服务端</span><span>全链路审计</span></div>
        </section>

        <section className="login-form">
          <div className="eyebrow">Secure Workspace</div>
          <h2>进入数据工作台</h2>
          <p>askdb 当前不设账号体系，本页不构成访问控制边界。身份能力归阶段 D。</p>

          <button className="sso-button" disabled title="阶段 D 接入企业 SSO">
            ◉ 使用企业 SSO 登录
            <em>阶段 D</em>
          </button>
          <span className="login-divider">当前可用</span>
          <button className="enter-button" onClick={onEnter}>直接进入工作台</button>
          <button disabled title="依赖阶段 D 的令牌体系">
            使用短时访问令牌
            <em>阶段 G</em>
          </button>

          <small>
            每次查询都会写入本地审计日志（一次调用一条记录，被护栏拦截的同样留痕）。
            数据库口令只存在于服务端，既不下发浏览器，也不进入提示词。
            <a href="/legacy" onClick={() => notify('旧界面接的是真实数据源')}>旧界面（接真实数据）↗</a>
          </small>
        </section>
      </div>
    </div>
  )
}

export default App
