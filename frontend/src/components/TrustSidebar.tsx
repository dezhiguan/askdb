import type { AskResult } from '../api'
import type { ResultTab, View } from '../types'
import type { HealthState } from '../useHealth'

/** 右栏三块：能不能执行、按什么策略执行、执行完拿什么复核。
 *
 *  原型这里有一个「可信度 96」的评分环 —— askdb 没有这个东西，也不该有：
 *  它保证的是**过程可信**（危险操作可拦、结果附 SQL 可自验、判定可追溯），
 *  不是**结果可信**。给一个分数等于替用户下了"这个答案有多可靠"的判断。
 *  环里换成今日剩余配额：真实、可对账，而且对外实例上它就是能不能再问的答案。
 */
export function TrustSidebar({ health, result, onResultTab, onNavigate }: {
  health: HealthState
  result: AskResult | null
  onResultTab: (tab: ResultTab) => void
  onNavigate: (view: View) => void
}) {
  const ready = health.status === 'ready' ? health.health : null
  const quota = ready?.quota
  const canExecute = !!ready?.datasource.ok
  const canAsk = canExecute && !!ready?.llm.ok

  const admission = !ready ? { label: '读取中', tone: 'wait' }
    : !ready.datasource.ok ? { label: '数据源不可用', tone: 'bad' }
    : !ready.llm.ok ? { label: '仅直查 SQL', tone: 'wait' }
    : { label: 'READY', tone: '' }

  return (
    <aside className="trust-sidebar">
      <section className="assurance-card">
        <div className="assurance-hero">
          <QuotaRing quota={quota} />
          <div>
            <strong>{canAsk ? '可执行只读查询' : canExecute ? '只读执行可用' : '暂不可执行'}</strong>
            <small>
              {canAsk ? '护栏在 SQL 执行前强制应用'
                : canExecute ? '未配模型密钥，自然语言提问不可用，直查 SQL 仍可用'
                : ready?.datasource.hint || '数据源连接不可用'}
            </small>
          </div>
          <span className={`ready-badge ${admission.tone}`}>{admission.label}</span>
        </div>
        <div className="assurance-grid">
          <div><span>数据源</span><strong>{ready ? ready.datasource.type : '—'}</strong></div>
          <div>
            <span>租户上下文</span>
            <strong>{ready ? (ready.tenant.enabled ? `${ready.tenant.column}=${ready.tenant.org_id}` : '单租户') : '—'}</strong>
          </div>
          <div><span>模型</span><strong>{ready ? (ready.llm.ok ? ready.llm.model : '未接') : '—'}</strong></div>
        </div>
      </section>

      <section className="side-card">
        <div className="side-head">
          <div><strong>本次执行策略</strong><small>SQL 执行前强制应用</small></div>
          <button onClick={() => onNavigate('sources')}>查看数据源 →</button>
        </div>
        <div className="policy-grid">
          <div><span>数据源</span><strong>{ready?.datasource.detail ?? '—'}</strong></div>
          <div>
            <span>租户隔离</span>
            <strong>{ready ? (ready.tenant.enabled ? ready.tenant.mode : '未启用') : '—'}</strong>
          </div>
          <div><span>返回上限 · R-13</span><strong>{ready ? `${ready.guard.max_rows} ROWS` : '—'}</strong></div>
          <div><span>执行超时 · R-12</span><strong>{ready ? `${ready.guard.timeout_ms} MS` : '—'}</strong></div>
        </div>
      </section>

      <section className="side-card">
        <div className="side-head">
          <div><strong>可验证输出</strong><small>查询完成后生成复核证据</small></div>
          <span className={`status ${result ? '' : 'wait'}`}>{result ? '已生成' : '等待执行'}</span>
        </div>
        <div className="evidence-actions">
          <button disabled={!result} onClick={() => onResultTab('sql')}>
            <i>SQL</i><span><strong>原生 SQL</strong><small>查看、复制并在客户端复核</small></span><b>→</b>
          </button>
          <button disabled={!result} onClick={() => onResultTab('chain')}>
            <i>TR</i><span><strong>执行链路</strong><small>节点、耗时与 token</small></span><b>→</b>
          </button>
        </div>
        <div className="evidence-footer">
          <div><span>TRACE ID</span><code>{result?.trace_id || '尚未生成'}</code></div>
          <div><span>耗时 / 成本</span>
            <code>{result ? `${result.elapsed_ms ?? 0}ms · ¥${(result.cost_cny ?? 0).toFixed(4)}` : '尚未生成'}</code>
          </div>
        </div>
      </section>

      <button className="audit-link" onClick={() => onNavigate('audit')}>
        <span>历史查询与审计记录</span><span>打开审计中心 →</span>
      </button>
    </aside>
  )
}

/** 今日剩余配额。配额没启用时不画环 —— 画一个满环等于暗示"有上限且还剩很多"。 */
function QuotaRing({ quota }: { quota: { limit: number; used: number; remaining: number | null } | undefined }) {
  if (!quota || quota.remaining == null || quota.limit <= 0) {
    return <div className="score-ring none" title="本实例未启用每日配额">∞</div>
  }
  const ratio = Math.max(0, Math.min(1, quota.remaining / quota.limit))
  return (
    <div
      className={`score-ring ${ratio < 0.15 ? 'low' : ''}`}
      style={{ '--ratio': ratio } as React.CSSProperties}
      title={`今日已用 ${quota.used} / ${quota.limit}`}
    >
      {quota.remaining}
    </div>
  )
}
