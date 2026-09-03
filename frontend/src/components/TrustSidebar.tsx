import type { AskResult, Me } from '../api'
import type { ResultTab, View } from '../types'
import type { HealthState } from '../useHealth'
import { useSqlDigest } from './ResultTabs'

/** 右栏三块：能不能执行、按什么策略执行、执行完拿什么复核。
 *
 *  版式、标签与文案一律照原型（trusted-data-agent-prototype.html 第 2196-2216 行）。
 *  格子里的值取真实数据；askdb 没有对应维度的（数据时间窗口）按原型排版留「—」。
 *  评分环照原型固定显示 96 —— askdb 没有"结果可信度"这个口径，它是设计稿上的
 *  展示值，等真有准入评分再接。
 */
export function TrustSidebar({ health, source, result, me, onResultTab, onNavigate }: {
  health: HealthState
  /** 工作台当前选中的数据源。切源时「本次执行策略」要跟着变 —— 护栏与
   *  租户是实例级、切源不变（原型亦如此），真正随源变的只有数据源身份这一项。 */
  source?: { name: string; dialect: string; env?: string }
  result: AskResult | null
  /** 「身份」一格要的当前登录态 */
  me?: Me | null
  onResultTab: (tab: ResultTab) => void
  onNavigate: (view: View) => void
}) {
  const ready = health.status === 'ready' ? health.health : null
  const canExecute = !!ready?.datasource.ok
  const canAsk = canExecute && !!ready?.llm.ok
  // 与结论卡上的摘要同源同算法：两处显示同一条 SQL 的哈希，不能各算各的
  const hash = useSqlDigest(result?.sql_final || result?.sql_raw || '')

  const admission = !ready ? { label: '读取中', tone: 'wait' }
    : !ready.datasource.ok ? { label: '数据源不可用', tone: 'bad' }
    : !ready.llm.ok ? { label: '仅直查 SQL', tone: 'wait' }
    : { label: 'READY', tone: '' }

  return (
    // side-stack 是原型的类名；trust-sidebar 保留，窄屏断点按它排版
    <aside className="trust-sidebar side-stack">
      <div className="assurance-card">
        <div className="assurance-hero">
          {/* 照原型固定 96。askdb 没有"结果可信度"这个口径，这是设计稿上的展示值 */}
          <div className="score-ring">96</div>
          <div className="assurance-hero-copy">
            <strong>{canAsk ? '安全准入已通过' : canExecute ? '只读执行可用' : '暂不可执行'}</strong>
            <small>
              {canAsk ? '当前身份可在只读边界内执行查询'
                : canExecute ? '未配模型密钥，自然语言提问不可用，直查 SQL 仍可用'
                : ready?.datasource.hint || '数据源连接不可用'}
            </small>
          </div>
          <span className={`ready-badge ${admission.tone}`}>{admission.label}</span>
        </div>
        {/* 三格标签照原型：身份 / 数据库角色 / 数据保护 */}
        <div className="assurance-grid">
          <div className="assurance-item" title={me?.username ? `可见表 ${me.scope.tables.length} 张 · 行上限 ${me.scope.max_rows}` : '未登录，按匿名可见范围执行'}>
            <span>身份</span><strong>{identityLabel(me)}</strong>
          </div>
          {/* askdb 的每一条连接都是只读（duckdb read_only / postgres 只读事务）；
              运行时源声明了环境就报环境 */}
          <div className="assurance-item">
            <span>数据库角色</span><strong>{dbRoleLabel(source?.env)}</strong>
          </div>
          <div className="assurance-item" title={ready?.tenant.enabled ? `租户隔离 ${ready.tenant.mode} · ${ready.tenant.column}=${ready.tenant.org_id}` : '单租户实例'}>
            <span>数据保护</span>
            <strong>{ready ? (ready.tenant.enabled ? 'RLS · AUDIT' : 'AUDIT') : '—'}</strong>
          </div>
        </div>
      </div>

      <div className="side-section-card">
        <div className="side-section-head">
          <div><strong>本次执行策略</strong><small>SQL 执行前强制应用</small></div>
          <button onClick={() => onNavigate('permissions')}>查看策略 →</button>
        </div>
        <div className="policy-rail">
          <div className="policy-chip">
            <span>数据源 / 环境</span>
            <strong>{source ? `${source.name} · 只读` : (ready?.datasource.detail ?? '—')}</strong>
          </div>
          {/* 原型第二格是「允许数据范围 · 最近 90 DAYS」。askdb 没有数据时间窗口
              这一策略维度，按原型排版留「—」，接入后再填 */}
          <div className="policy-chip" title="askdb 尚未接入数据时间窗口策略">
            <span>允许数据范围</span><strong>—</strong>
          </div>
          <div className="policy-chip">
            <span>返回上限</span><strong>{ready ? `${ready.guard.max_rows} ROWS` : '—'}</strong>
          </div>
          {/* 原型用 SEC，不用 MS */}
          <div className="policy-chip">
            <span>执行超时</span><strong>{ready ? `${Math.round(ready.guard.timeout_ms / 1000)} SEC` : '—'}</strong>
          </div>
        </div>
      </div>

      <div className="side-section-card">
        <div className="side-section-head">
          <div><strong>可验证输出</strong><small>查询完成后生成复核证据</small></div>
          <span className={`status ${result ? '' : 'wait'}`}>{result ? '已生成' : '等待执行'}</span>
        </div>
        <div className="evidence-actions">
          <button className="evidence-action" disabled={!result} onClick={() => onResultTab('sql')}>
            <i>SQL</i><span><strong>原生 SQL</strong><small>查看、复制并在客户端复核</small></span><b>→</b>
          </button>
          <button className="evidence-action open-trace" disabled={!result} onClick={() => onResultTab('chain')}>
            <i>TR</i><span><strong>Agent 执行链路</strong><small>查看模型、工具和策略节点</small></span><b>→</b>
          </button>
        </div>
        <div className="evidence-footer">
          <div><span>QUERY ID</span><code>{result?.trace_id || '尚未生成'}</code></div>
          <div>
            <span>SQL SHA-256</span>
            <code>{!result ? '尚未生成' : hash ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : '—'}</code>
          </div>
          {/* 原型只有上面两格。耗时与成本是这套实现真实产出的账，本页没有第二处
              能看到它 —— 补成通栏第三格，不占原型两格的版位。 */}
          <div className="evidence-footer-wide">
            <span>耗时 / 成本</span>
            <code>{result ? `${result.elapsed_ms ?? 0}ms · ¥${(result.cost_cny ?? 0).toFixed(4)}` : '尚未生成'}</code>
          </div>
        </div>
      </div>

      <button className="audit-link side-audit-link" onClick={() => onNavigate('audit')}>
        <span>历史查询与审计记录</span><span>打开审计中心 →</span>
      </button>
    </aside>
  )
}

/** 「身份」一格。原型写死 `SSO · PRODUCT`；这里报真实登录态与角色。 */
function identityLabel(me?: Me | null): string {
  if (!me) return '—'
  if (!me.username) return '匿名 · GUEST'
  const role = (me.roles[0] ?? 'USER').toUpperCase()
  return `${me.display_name || me.username} · ${role}`
}

/** 「数据库角色」一格。askdb 的连接一律只读，运行时源声明了环境就报环境。 */
function dbRoleLabel(env?: string): string {
  if (env === 'prod_ro') return 'PROD-RO'
  if (env === 'test') return 'TEST-RO'
  return 'READ-ONLY'
}
