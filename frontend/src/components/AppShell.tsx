import { navGroups } from '../data/mockData'
import type { HealthState } from '../useHealth'
import type { View } from '../types'

interface AppShellProps {
  activeView: View
  health: HealthState
  onNavigate: (view: View) => void
  onSignOut: () => void
  notice?: React.ReactNode
  children: React.ReactNode
}

/** 顶栏中段是「当前连的是哪个库」。
 *
 * 这些值一律取自 /api/health，不落任何写死的库名或环境名 ——
 * 同一台机器上会同时跑多个实例（样例库、生产库），界面长得一模一样，
 * 顶栏说错一次就会有人拿着另一个库的结论下判断。
 */
function WorkspaceContext({ health }: { health: HealthState }) {
  if (health.status === 'loading') {
    return <div className="workspace-context"><span>当前数据源 /</span><div className="context-chip">连接中…</div></div>
  }
  if (health.status === 'error') {
    return (
      <div className="workspace-context">
        <span>当前数据源 /</span>
        <div className="context-chip danger-chip" title={health.message}>连不上 askdb 服务</div>
      </div>
    )
  }

  const { datasource, llm, tenant } = health.health
  return (
    <div className="workspace-context">
      <span>当前数据源 /</span>
      <div className={`context-chip ${datasource.ok ? '' : 'danger-chip'}`} title={datasource.hint || undefined}>
        {datasource.ok ? <i className="online-dot" /> : null}
        {datasource.ok ? `${datasource.type} · ${datasource.detail}` : '数据源不可用'}
      </div>
      <div className="context-chip hide-mobile">
        {llm.ok ? llm.model : llm.disabled ? '仅直查 SQL' : '未配置模型密钥'}
      </div>
      <div className="context-chip hide-mobile">
        {tenant.enabled ? `${tenant.column} = ${tenant.org_id}` : '单租户 · 不做行级隔离'}
      </div>
    </div>
  )
}

export function AppShell({ activeView, health, onNavigate, onSignOut, notice, children }: AppShellProps) {
  const configPath = health.status === 'ready' ? health.health.config : '—'

  return (
    <div className="app-shell">
      <header className="topbar">
        <Brand />
        <WorkspaceContext health={health} />
        <div className="top-actions">
          {/* 这句是架构事实：数据库口令只在服务端，既不下发浏览器也不进提示词 */}
          <span className="secure-label"><i className="online-dot" /> 凭证不出服务端</span>
          <span className="context-chip config-chip hide-mobile" title="当前实例加载的配置文件">
            配置文件 {configPath}
          </span>
          <button className="ghost" onClick={onSignOut}>退出</button>
        </div>
      </header>

      <aside className="sidebar">
        <button className="quick-new" onClick={() => onNavigate('query')}><span>发起查询</span><span>↗</span></button>
        {navGroups.map(group => (
          <div className="nav-group" key={group.label}>
            <div className="nav-label">{group.label}</div>
            {group.items.map(item => (
              <button
                className={`nav-item ${activeView === item.view ? 'active' : ''}`}
                key={item.view}
                title={item.title}
                onClick={() => onNavigate(item.view)}
              >
                <span className="nav-icon">{item.icon}</span>
                <span className="nav-copy"><strong>{item.title}</strong><small>{item.subtitle}</small></span>
              </button>
            ))}
          </div>
        ))}
        <div className="phase-card">
          <span>当前阶段</span><strong>A · 外壳统一</strong>
          <div className="progress"><i style={{ width: '14%' }} /></div>
          <p>界面形态已换到工作台外壳。后端能力正在按页接回，未接入的页面均已标注。</p>
        </div>
      </aside>
      <main className="main-content">{notice}{children}</main>
    </div>
  )
}

export function Brand({ subtitle = 'ASKDB' }: { subtitle?: string }) {
  return (
    <div className="brand">
      <div className="brand-mark" />
      <div><strong>可信数据问答平台</strong><small>{subtitle}</small></div>
    </div>
  )
}

export function PageHeader({ eyebrow, title, description, action }: {
  eyebrow: string
  title: string
  description: string
  action?: React.ReactNode
}) {
  return (
    <div className="page-head">
      <div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>
      {action}
    </div>
  )
}
