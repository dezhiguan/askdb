import type { DataSource, ResultTab, View } from '../types'

export function TrustSidebar({ source, evidenceReady, onResultTab, onNavigate }: {
  source: DataSource
  evidenceReady: boolean
  onResultTab: (tab: ResultTab) => void
  onNavigate: (view: View) => void
}) {
  return (
    <aside className="trust-sidebar">
      <section className="assurance-card">
        <div className="assurance-hero">
          <div className="score-ring">96</div>
          <div><strong>安全准入已通过</strong><small>当前身份可在只读边界内执行查询</small></div>
          <span className="ready-badge">READY</span>
        </div>
        <div className="assurance-grid">
          <div><span>身份</span><strong>SSO · PRODUCT</strong></div>
          <div><span>数据库角色</span><strong>PROD-RO</strong></div>
          <div><span>数据保护</span><strong>MASK · AUDIT</strong></div>
        </div>
      </section>

      <section className="side-card">
        <div className="side-head"><div><strong>本次执行策略</strong><small>SQL 执行前强制应用</small></div><button onClick={() => onNavigate('permissions')}>查看策略 →</button></div>
        <div className="policy-grid">
          <div><span>数据源 / 环境</span><strong>{source.shortName} · PROD-RO</strong></div>
          <div><span>允许数据范围</span><strong>最近 90 DAYS</strong></div>
          <div><span>返回上限</span><strong>100 ROWS</strong></div>
          <div><span>执行超时</span><strong>15 SEC</strong></div>
        </div>
      </section>

      <section className="side-card">
        <div className="side-head">
          <div><strong>可验证输出</strong><small>查询完成后生成复核证据</small></div>
          <span className={`status ${evidenceReady ? '' : 'wait'}`}>{evidenceReady ? '已生成' : '等待执行'}</span>
        </div>
        <div className="evidence-actions">
          <button disabled={!evidenceReady} onClick={() => onResultTab('sql')}><i>SQL</i><span><strong>原生 SQL</strong><small>查看、复制并在客户端复核</small></span><b>→</b></button>
          <button disabled={!evidenceReady} onClick={() => onNavigate('traces')}><i>TR</i><span><strong>Agent 执行链路</strong><small>查看模型、工具和策略节点</small></span><b>→</b></button>
        </div>
        <div className="evidence-footer">
          <div><span>QUERY ID</span><code>{evidenceReady ? 'QRY-183108-7A2F' : '尚未生成'}</code></div>
          <div><span>SQL SHA-256</span><code>{evidenceReady ? '8ad2…91cf' : '尚未生成'}</code></div>
        </div>
      </section>
      <button className="audit-link" onClick={() => onNavigate('audit')}><span>历史查询与审计记录</span><span>打开审计中心 →</span></button>
    </aside>
  )
}
