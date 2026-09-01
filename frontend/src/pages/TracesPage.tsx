import { useEffect, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink,
  type AuditItem, type AuditStats, type Replay, type ReplayResult,
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
        eyebrow="Phase 2 · Agent Observability"
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
          <TraceDetail replay={currentReplay} traceId={selected} replayOn={!!stats?.replay_api} />
        </section>
      </div>

      <div className="observe-grid">
        <article className="integration-card">
          <div>
            <h3>观测后端</h3>
            <p>
              与审计记录同源上报：一次调用完成后按同一条记录送一次 trace，观测面与审计面天然一致。
              trace id 复用本地 trace_id，两边可按 id 深链互跳。异步批送，观测失联不反噬主链路。
            </p>
          </div>
          <span className={`status ${tracing?.enabled ? '' : 'wait'}`}>
            {tracing?.enabled
              ? `${tracing.backend === 'langfuse' ? 'LANGFUSE' : 'LANGSMITH'} · ${tracing.project}`
              : 'NOT CONNECTED'}
          </span>
          <div className="attribute-list">
            {['trace_id', 'org_id', 'kind', 'rejected_by', 'attempts', 'cost_cny', 'tables_hit']
              .map(field => <code key={field}>{field}</code>)}
          </div>
        </article>

        {/* 原型这块承诺提示词被抹掉、SQL 只送哈希。askdb 的实际策略不是这样：
            observe.py 把问题原文当 input、SQL 全文当 output 送出去。
            照抄就成了一句假的隐私承诺，而假承诺比没有承诺更危险。 */}
        <article className="integration-card">
          <div>
            <h3>上报数据边界</h3>
            <p>
              上报的是步骤元数据、SQL 文本与 token 计量。<b>结果行与注入提示词不上报</b> ——
              这两项同样不出 <span className="mono">/api/replay</span>（字段白名单，回放接口设计说明 §4.2）。
            </p>
          </div>
          <span className="status">元数据 + SQL 文本</span>
          <div className="attribute-list">
            <code>question: 原文上报</code>
            <code>sql: 全文上报</code>
            <code>结果行: 不上报</code>
            <code>schema 提示词: 不上报</code>
          </div>
        </article>
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

function TraceDetail({ replay, traceId, replayOn }: {
  replay: ReplayResult | null
  traceId: string | null
  replayOn: boolean
}) {
  if (!traceId) return <p className="drawer-note">左侧选一条调用查看节点明细。</p>

  // 连真实数据源的实例默认关闭回放。这里要说清楚，而不是让人以为页面坏了
  if (!replayOn) {
    return (
      <>
        <p className="drawer-note">
          判定链路回放未开启（<span className="mono">observability.replay_api</span>），
          取不到节点级明细。连真实数据源的实例默认关闭这一项。
        </p>
        <pre className="drawer-code">askdb replay {traceId}</pre>
      </>
    )
  }
  if (!replay) return <p className="drawer-note">读取中…</p>
  if (replay.status === 'rate_limited') {
    return <p className="drawer-note">回放接口被限流了，稍等一分钟再试。每次回放都要遍历检查点库。</p>
  }
  if (replay.status === 'not_found') {
    return <p className="drawer-note">取不到这条记录：回放开关未开启，或记录不存在。两种情况接口都返回 404，不做区分。</p>
  }

  const d: Replay = replay.data
  const steps = d.steps ?? []
  const outcome = d.rejected_by
    ? (d.rejected_by === 'INTERRUPTED' ? 'INTERRUPTED' : `BLOCKED · ${d.rejected_by}`)
    : 'SUCCESS'

  return (
    <>
      <div className="trace-title">
        <div>
          <h3>{d.question || `（${KIND_NAMES[d.kind] ?? d.kind}）`}</h3>
          <p>{d.trace_id} · {outcome} · {d.multi_step ? 'MULTI-STEP' : 'ONE-SHOT'}</p>
        </div>
        <span className={`status ${d.rejected_by ? 'bad' : ''}`}>{KIND_NAMES[d.kind] ?? d.kind}</span>
      </div>

      <div className="trace-facts">
        <div><span>总耗时</span><strong>{secs(d.elapsed_ms)}</strong></div>
        <div><span>轮次</span><strong>{d.attempts ?? '—'}</strong></div>
        <div><span>Token</span><strong>{(d.tok_in ?? 0)}+{(d.tok_out ?? 0)}</strong></div>
        <div><span>成本</span><strong>¥{d.cost_cny ?? 0}</strong></div>
        <div><span>命中表</span><strong>{d.tables_hit?.join(' · ') || '—'}</strong></div>
        <div><span>命中口径</span><strong>{d.metrics_hit?.join(' · ') || '—'}</strong></div>
        <div><span>返回行</span><strong>{d.rows_returned ?? '—'}</strong></div>
        <div><span>命中规则</span><strong>{d.rules_fired?.join(' · ') || '—'}</strong></div>
      </div>

      {steps.length > 0 && (
        <div className="trace-flow">
          {steps.map((step, i) => (
            <span key={`${step.step}-${i}`} className={step.status === 'ok' ? '' : 'bad'}>
              <strong>{STEP_NAMES[step.step] ?? step.step}</strong>
              <small>{step.ms} ms</small>
              {i < steps.length - 1 && <i>→</i>}
            </span>
          ))}
        </div>
      )}

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
                <td>
                  <span className={`status ${step.status === 'ok' ? '' : 'bad'}`}>
                    {step.status.toUpperCase()}
                  </span>
                </td>
              </tr>
            ))}
            {steps.length === 0 && (
              <tr><td colSpan={6} className="audit-empty">该记录没有步骤明细</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {d.snapshots.length > 0 && (
        <p className="drawer-note">
          该调用另有 {d.snapshots.length} 份检查点快照，可在审计中心的「复放」里逐轮查看。
        </p>
      )}
    </>
  )
}
