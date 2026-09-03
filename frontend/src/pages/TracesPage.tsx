import { Fragment, useEffect, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink,
  type AuditItem, type AuditStats, type ReplayResult,
} from '../api'
import { PageHeader } from '../components/AppShell'
import { KIND_NAMES, STEP_NAMES, STEP_TYPE } from '../traceSteps'


function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const secs = (ms: number | null | undefined) => ms == null ? '—' : `${(ms / 1000).toFixed(2)}s`

export function TracesPage() {
  const [stats, setStats] = useState<AuditStats | null>(null)
  const [items, setItems] = useState<AuditItem[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  // 存成 {key, result}，切换 trace 时靠 key 不匹配自然回到「读取中」，
  // 不需要在 effect 里先同步 setReplay(null) —— 那会多触发一轮渲染
  const [replay, setReplay] = useState<{ key: string; result: ReplayResult } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    Promise.all([fetchAuditStats(), fetchAudit({ page: 1, pageSize: 12, q: '', kind: '' })])
      .then(([s, list]) => {
        if (!alive) return
        setStats(s)
        setItems(list.items)
        // 进页面就该看到东西：默认选中最近一次调用，不要求先点一下
        if (list.items.length > 0) setSelected(list.items[0].trace_id)
      })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!selected) return
    let alive = true
    fetchReplay(selected).then(result => { if (alive) setReplay({ key: selected, result }) })
    return () => { alive = false }
  }, [selected])

  const currentReplay = replay && replay.key === selected ? replay.result : null

  const tracing = stats?.tracing
  const link = stats && selected && tracing?.enabled ? tracingLink(tracing, selected) : null

  return (
    <div className="page">
      <PageHeader
        title="执行追踪"
        description="按调用查看它经过的节点、每段耗时与 token。链路数据与审计记录同源，按 trace_id 一一对应。"
        action={link
          ? <a className="primary" href={link} target="_blank" rel="noopener noreferrer">
              在 {tracing?.backend === 'langfuse' ? 'Langfuse' : 'LangSmith'} 打开 ↗
            </a>
          : <button className="ghost" disabled title="配置 LANGFUSE_* 或 LANGSMITH_* 环境变量后启用">
              观测后端未接入
            </button>}
      />

      {error && <div className="audit-error">读取追踪数据失败：{error}</div>}

      <StatTiles stats={stats} />

      <div className="trace-layout">
        <section className="card trace-list">
          <div className="card-head">
            <strong>最近执行</strong>
            <span className="status">{items ? `${items.length} 条` : '读取中'}</span>
          </div>
          {items?.map(item => (
            <button
              className={selected === item.trace_id ? 'active' : ''}
              key={item.trace_id + item.ts}
              onClick={() => setSelected(item.trace_id)}
            >
              <i className={item.ok ? '' : 'bad'}>{item.ok ? '✓' : '✕'}</i>
              <span>
                <strong>{item.question || `（${KIND_NAMES[item.kind] ?? item.kind}）`}</strong>
                <small>{fmtTime(item.ts)} · {secs(item.elapsed_ms)}</small>
              </span>
              <code>{item.trace_id.slice(0, 6)}</code>
            </button>
          ))}
          {items?.length === 0 && <div className="audit-empty">窗口内没有调用记录</div>}
        </section>

        <section className="card trace-detail">
          <TraceDetail item={items?.find(i => i.trace_id === selected) ?? null} replay={currentReplay} replayOn={!!stats?.replay_api} />
        </section>
      </div>
    </div>
  )
}

function StatTiles({ stats }: { stats: AuditStats | null }) {
  if (!stats) return <div className="stats"><div><span>读取中…</span></div></div>

  const modelCalls = Object.values(stats.by_model).reduce((sum, m) => sum + m.calls, 0)
  const avgTokens = modelCalls > 0 ? Math.round((stats.tok_in + stats.tok_out) / modelCalls) : null
  const pct = (v: number | null) => v == null ? '—' : `${Math.round(v * 100)}%`

  return (
    <div className="stats">
      <div>
        <span>近 {stats.days} 天调用</span><strong>{stats.calls.toLocaleString()}</strong>
        <small>拦截 {stats.blocked} 次</small>
      </div>
      <div>
        <span>P95 总耗时</span><strong>{secs(stats.elapsed_p95_ms)}</strong>
        {/* 样本量必须一起给：7 次调用的 P95 基本等于最慢那次，当成稳定指标读会出错 */}
        <small>P50 {secs(stats.elapsed_p50_ms)} · 样本 {stats.calls} 次</small>
      </div>
      <div>
        <span>具备步骤级 trace</span><strong>{pct(stats.trace_complete)}</strong>
        <small>按记录如实计算</small>
      </div>
      <div>
        <span>平均 Token</span><strong>{avgTokens?.toLocaleString() ?? '—'}</strong>
        <small>{modelCalls > 0 ? `${modelCalls} 次经模型调用` : '窗口内没有经模型的调用'}</small>
      </div>
    </div>
  )
}

function TraceDetail({ item, replay, replayOn }: {
  item: AuditItem | null
  replay: ReplayResult | null
  replayOn: boolean
}) {
  if (!item) return <p className="trace-empty">左侧选一条调用查看节点明细。</p>

  const d = replay?.status === 'ok' ? replay.data : null
  const outcome = item.ok
    ? 'SUCCESS'
    : item.rejected_by === 'INTERRUPTED' ? 'INTERRUPTED' : `BLOCKED · ${item.rejected_by}`

  return (
    <>
      {/* 标题与事实网格只用审计流水里的字段 —— 回放关着时照样完整，
          不会出现右半屏一片空白 */}
      <div className="trace-detail-head">
        <div>
          <h3>{item.question || `（${KIND_NAMES[item.kind] ?? item.kind}）`}</h3>
          <p>{item.trace_id} · {outcome} · {item.multi_step ? 'MULTI-STEP' : 'ONE-SHOT'}</p>
        </div>
        <span className={`status ${item.ok ? '' : 'bad'}`}>{KIND_NAMES[item.kind] ?? item.kind}</span>
      </div>

      <div className="trace-facts">
        <div className="trace-fact"><span>总耗时</span><strong>{secs(item.elapsed_ms)}</strong></div>
        <div className="trace-fact"><span>角色</span><strong>{item.role || '—'}</strong></div>
        <div className="trace-fact"><span>轮次</span><strong>{item.attempts ?? '—'}</strong></div>
        <div className="trace-fact"><span>返回行</span><strong>{item.rows_returned ?? '—'}</strong></div>
        <div className="trace-fact"><span>成本</span><strong>¥{item.cost_cny ?? 0}</strong></div>
        {d && <>
          <div className="trace-fact"><span>Token</span><strong>{(d.tok_in ?? 0)}+{(d.tok_out ?? 0)}</strong></div>
          <div className="trace-fact"><span>步数</span><strong>{d.step_count ?? '—'}</strong></div>
          <div className="trace-fact"><span>命中表</span><strong>{d.tables_hit?.join(' · ') || '—'}</strong></div>
          <div className="trace-fact"><span>命中口径</span><strong>{d.metrics_hit?.join(' · ') || '—'}</strong></div>
          <div className="trace-fact"><span>命中规则</span><strong>{d.rules_fired?.join(' · ') || '—'}</strong></div>
        </>}
      </div>

      <TraceNodes item={item} replay={replay} replayOn={replayOn} />
    </>
  )
}

/** 链路条与 Span 明细。这两段的数据只有回放接口给得出来，
 *  所以降级说明放在这里 —— 而不是把整个详情区换成一句话。 */
function TraceNodes({ item, replay, replayOn }: {
  item: AuditItem
  replay: ReplayResult | null
  replayOn: boolean
}) {
  if (!replayOn) {
    return (
      <div className="trace-degraded">
        <strong>节点级明细需要开启判定链路回放</strong>
        <p>
          本实例未开启 <span className="mono">observability.replay_api</span> ——
          连真实数据源的实例默认关闭这一项，因为回放会返回 SQL 全文。
          上面的耗时、角色、轮次、成本取自审计流水，不受此开关影响。
        </p>
        <pre className="drawer-code">askdb replay {item.trace_id}</pre>
      </div>
    )
  }
  if (!replay) return <div className="trace-degraded"><p>读取中…</p></div>
  if (replay.status === 'rate_limited') {
    return (
      <div className="trace-degraded">
        <strong>回放接口被限流</strong>
        <p>每次回放都要遍历检查点库，它有独立限流，稍等一分钟再试。</p>
      </div>
    )
  }
  if (replay.status === 'not_found') {
    return (
      <div className="trace-degraded">
        <strong>取不到这条记录</strong>
        <p>回放开关未开启，或记录不存在 —— 两种情况接口都返回 404，不做区分。</p>
      </div>
    )
  }

  const steps = replay.data.steps ?? []
  if (steps.length === 0) {
    return (
      <div className="trace-degraded">
        <strong>该记录没有步骤明细</strong>
        <p>早于步骤级 trace 落地的调用会是这样；新调用都带完整节点链。</p>
      </div>
    )
  }

  return (
    <>
      <div className="trace-flow">
        {steps.map((step, i) => (
          <Fragment key={`${step.step}-${i}`}>
            <div className={`trace-node ${(STEP_TYPE[step.step] ?? '').toLowerCase()} ${step.status === 'ok' ? '' : 'bad'}`}>
              <strong>{STEP_NAMES[step.step] ?? step.step}</strong>
              <small>{step.ms} ms</small>
            </div>
            {i < steps.length - 1 && <i className="trace-arrow">→</i>}
          </Fragment>
        ))}
      </div>

      <div className="span-table">
        <div className="span-table-title">
          <strong>Span 明细</strong><span>按执行顺序</span>
        </div>
        <div className="table-scroll">
          <table className="audit-table">
            <thead>
              <tr><th>类型</th><th>节点</th><th>说明</th><th className="num">耗时</th><th className="num">Token</th><th>状态</th></tr>
            </thead>
            <tbody>
              {steps.map((step, i) => (
                <tr key={`${step.step}-${i}`}>
                  <td><span className={`span-type ${(STEP_TYPE[step.step] ?? 'sys').toLowerCase()}`}>{STEP_TYPE[step.step] ?? 'SYS'}</span></td>
                  <td>{STEP_NAMES[step.step] ?? step.step}</td>
                  <td className="audit-question" title={step.note ?? ''}>{step.note || <span className="dim">—</span>}</td>
                  <td className="num">{step.ms} ms</td>
                  <td className="num">{step.tok_in ? `${step.tok_in}+${step.tok_out}` : <span className="dim">—</span>}</td>
                  <td><span className={`status ${step.status === 'ok' ? '' : 'bad'}`}>{step.status.toUpperCase()}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
