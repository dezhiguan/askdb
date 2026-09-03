import { navGroups } from '../data/mockData'
import type { Me } from '../api'
import type { SourceCard } from '../api'
import type { HealthState } from '../useHealth'
import type { View } from '../types'

interface AppShellProps {
  activeView: View
  health: HealthState
  /** 工作台当前选中的运行时数据源；内置源为 null */
  source: SourceCard | null
  onNavigate: (view: View) => void
  /** 侧栏「发起快捷查询」：回到工作台并清空上一次结果 */
  onQuickNew: () => void
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
 *    1. 正在查哪个库（跟随工作台的数据源切换，不是永远显示内置源）
 *    2. 库在哪（本机文件 / 本机 / 远端）
 *    3. 这条连接的性质：运行时源报它自己声明的环境，内置源报 READ-ONLY
 *
 *  原型第二枚写「企业内网」、第三枚写「PROD-RO」。前者是部署事实，askdb 无从得知；
 *  后者只有数据源自己声明了环境才成立 —— 一律写 PROD-RO 等于对着测试库宣称
 *  "这是生产只读镜像"。
 *
 *  配置文件路径挪进第一枚 chip 的 title：同一台机器上会同时跑多个实例、界面长得
 *  一模一样，这条信息不能没有，但它不该占顶栏的视觉位置。
 */
function WorkspaceContext({ health, source }: { health: HealthState; source: SourceCard | null }) {
  // 选了运行时数据源就以它为准 —— /api/health 描述的永远是内置源，
  // 拿它当"当前空间"会在切换后说谎
  if (source) {
    return (
      <div className="workspace-context">
        <span>当前空间 /</span>
        <div className="context-chip" title={`运行时数据源 ${source.id}`}>
          <i className={source.table_count ? 'online-dot' : 'idle-dot'} />
          {source.name}
        </div>
        <div className="context-chip hide-mobile">{placeOfHost(source.type, source.host)}</div>
        <div className="context-chip hide-mobile">{ENV_LABEL[source.env] ?? 'READ-ONLY'}</div>
      </div>
    )
  }

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

  // 没配默认数据源：既不是"正常连着"也不是"连不上"，说成任何一种都是假话。
  // 这时候唯一诚实的表述是"还没选源"，并把人指向数据源页。
  if (!datasource.configured) {
    return (
      <div className="workspace-context">
        <span>当前空间 /</span>
        <div className="context-chip" title={datasource.hint || undefined}>
          <i className="idle-dot" />未设默认源
        </div>
        <div className="context-chip hide-mobile">按查询选源</div>
      </div>
    )
  }

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
      <div className="context-chip hide-mobile">{placeOfHost(datasource.type, datasource.detail)}</div>
      {/* 内置源没有环境声明。READ-ONLY 对 askdb 的所有连接都成立：
          duckdb 以 read_only 打开，postgres 会话强制 default_transaction_read_only */}
      <div className="context-chip hide-mobile">READ-ONLY</div>
    </div>
  )
}

const ENV_LABEL: Record<string, string> = { prod_ro: 'PROD-RO', test: 'TEST' }

/** 库在哪。duckdb 是本机文件；其余看主机是不是回环地址。 */
function placeOfHost(type: string, where: string): string {
  if (type === 'duckdb') return '本机文件'
  return /(^|@|\s)(127\.0\.0\.1|localhost)\b/.test(where) ? '本机' : '远端'
}

export function AppShell({ activeView, health, source, onNavigate, onQuickNew, me, onOpenLogin, onSignOut, notice, children }: AppShellProps) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Brand />
        <WorkspaceContext health={health} source={source} />
        <div className="top-actions">
          <span className="secure-label"><i className="online-dot" /> 数据不出域</span>
          <Identity me={me} onOpenLogin={onOpenLogin} onSignOut={onSignOut} />
        </div>
      </header>

      <aside className="sidebar">
        <button className="quick-new" onClick={onQuickNew}><span>发起快捷查询</span><span>↗</span></button>
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
                {item.badge && <span className="nav-badge">{item.badge}</span>}
              </button>
            ))}
          </div>
        ))}
        <div className="phase-card">
          <span>CURRENT RELEASE</span>
          <strong>Phase 1 · 内网试点</strong>
          <div className="progress"><i /></div>
          <p>测试库与生产只读镜像已接入，治理能力正在配置。</p>
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
  /** 原型每页标题上方都有一条阶段/能力标（如 "Phase 2 · Full Traceability"） */
  eyebrow?: string
  title: string
  description: string
  action?: React.ReactNode
}) {
  return (
    <div className="page-head">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  )
}
