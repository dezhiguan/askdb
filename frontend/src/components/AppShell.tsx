import { navGroups } from '../data/mockData'
import type { Me } from '../api'
import type { HealthState } from '../useHealth'
import type { View } from '../types'

interface AppShellProps {
  activeView: View
  health: HealthState
  onNavigate: (view: View) => void
  me: Me | null
  onOpenLogin: () => void
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
/** 顶栏中段：当前空间。
 *
 *  版式照原型 —— 「当前空间 /」+ 三枚 chip。三枚各自填的是**askdb 真有的东西**：
 *    1. 连的哪个库
 *    2. 库在哪（本机文件 / 本机端口 / 远端主机）
 *    3. 这条连接的只读级别
 *
 *  原型第二枚写「企业内网」、第三枚写「PROD-RO」。前者是部署事实，askdb 无从得知；
 *  后者只有运行时数据源声明了环境才成立，内置源没有这个信息 —— 一律写 PROD-RO
 *  等于对着测试库宣称"这是生产只读镜像"。
 *
 *  配置文件路径挪进第一枚 chip 的 title：同一台机器上会同时跑多个实例、界面长得
 *  一模一样，这条信息不能没有，但它不该占顶栏的视觉位置。
 */
function WorkspaceContext({ health }: { health: HealthState }) {
  if (health.status === 'loading') {
    return (
      <div className="workspace-context">
        <span>当前空间 /</span>
        <div className="context-chip">连接中…</div>
      </div>
    )
  }
  if (health.status === 'error') {
    return (
      <div className="workspace-context">
        <span>当前空间 /</span>
        <div className="context-chip danger-chip" title={health.message}>连不上 askdb 服务</div>
      </div>
    )
  }

  const { datasource, config } = health.health
  return (
    <div className="workspace-context">
      <span>当前空间 /</span>
      <div
        className={`context-chip ${datasource.ok ? '' : 'danger-chip'}`}
        title={datasource.ok ? `配置文件 ${config}` : datasource.hint || undefined}
      >
        {datasource.ok && <i className="online-dot" />}
        {datasource.ok ? datasource.detail : '数据源不可用'}
      </div>
      <div className="context-chip hide-mobile">{placeOf(datasource)}</div>
      <div className="context-chip hide-mobile">READ-ONLY</div>
    </div>
  )
}

/** 库在哪。duckdb 是本机文件；postgres 看 detail 里的主机是不是回环。 */
function placeOf(ds: { type: string; detail: string }): string {
  if (ds.type === 'duckdb') return '本机文件'
  return /(^|@|\s)(127\.0\.0\.1|localhost)\b/.test(ds.detail) ? '本机' : '远端'
}

export function AppShell({ activeView, health, onNavigate, me, onOpenLogin, onSignOut, notice, children }: AppShellProps) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Brand />
        <WorkspaceContext health={health} />
        <div className="top-actions">
          <Identity me={me} onOpenLogin={onOpenLogin} onSignOut={onSignOut} />
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

/** 顶栏身份位。
 *
 * 除了「是谁」，还要把**生效边界**摆出来（可见表数 / 行上限）——
 * 权限体系最怕的是"配了但看不出有没有生效"，而这一格是访客唯一会看的地方。
 */
function Identity({ me, onOpenLogin, onSignOut }: {
  me: Me | null
  onOpenLogin: () => void
  onSignOut: () => void
}) {
  if (!me) return <span className="context-chip">…</span>
  if (!me.enabled) {
    return <span className="context-chip" title="未配置会话密钥，登录整体关闭">登录未启用</span>
  }

  const scope = `${me.scope.tables.length} 表 · ${me.scope.max_rows} 行`

  if (!me.username) {
    return (
      <>
        <span className="context-chip" title={`可见表：${me.scope.tables.join('、')}`}>
          匿名 · {scope}
        </span>
        <button className="primary" onClick={onOpenLogin}>登录 / 一键体验</button>
      </>
    )
  }
  return (
    <>
      <span className="context-chip identity-chip" title={`可见表：${me.scope.tables.join('、')}`}>
        <b>{me.display_name || me.username}</b>
        <em>{me.roles.join('+') || '无角色'}</em>
        <span className="dim">{scope}</span>
      </span>
      <button className="ghost" onClick={onSignOut}>退出</button>
    </>
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
