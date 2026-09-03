import { Fragment, useEffect, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink,
  type AuditItem, type AuditStats, type Replay, type ReplayResult, type ReplayStep,
} from '../api'
import type { ModalName, View } from '../types'
import { KIND_NAMES, STEP_NAMES, STEP_TYPE } from '../traceSteps'


function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const secs = (ms: number | null | undefined) => ms == null ? '—' : `${(ms / 1000).toFixed(2)}s`

/** 后端在这条 trace 上没有记录的字段，按原型的版位留占位符，不编数。 */
const NA = '—'

/** 工具/数据库类节点数 —— 原型「工具调用」那一格的真实口径。 */
const toolCalls = (steps: ReplayStep[]) =>
  steps.filter(s => STEP_TYPE[s.step] === 'TOOL' || STEP_TYPE[s.step] === 'DB').length

export function TracesPage({ onNavigate, onOpenModal }: {
  /** App 未传时退回点击侧栏导航（见 goTasks 注释） */
  onNavigate?: (view: View) => void
  /** 「接入 Langfuse」弹窗由 App 的 ModalLayer 挂载，未传时按钮置灰 */
  onOpenModal?: (modal: ModalName) => void
} = {}) {
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
  const currentItem = items?.find(i => i.trace_id === selected) ?? null

  const tracing = stats?.tracing
  const link = stats && selected && tracing?.enabled ? tracingLink(tracing, selected) : null

  /** 「← 返回任务中心」。
   *
   *  这一页由 App 无参渲染（<TracesPage />），拿不到 setView。改 App.tsx 不在本次
   *  改动范围内，所以留一条不新增依赖、不改别的文件的退路：直接点侧栏那颗导航按钮。
   *  一旦 App 传下 onNavigate，走的就是正规路径，这段自动不执行。 */
  const goTasks = () => {
    if (onNavigate) { onNavigate('tasks'); return }
    const target = Array.from(document.querySelectorAll<HTMLButtonElement>('.sidebar .nav-item'))
      .find(button => button.querySelector('strong')?.textContent === '任务中心')
    target?.click()
  }

  return (
    <div className="page traces-page">
      {/* 通用 PageHeader 没有 eyebrow 位，这里按原型直接写出 .page-head */}
      <div className="page-head">
        <div>
          <div className="eyebrow">Phase 2 · Agent Observability</div>
          <h1>智能体执行追踪</h1>
          <p>查看每次查询经过的模型、工具、策略和数据库节点，为 Langfuse/OpenTelemetry 预留统一 Trace 结构。</p>
        </div>
        <div className="card-actions">
          <button className="ghost" onClick={goTasks}>← 返回任务中心</button>
          <button
            className="ghost"
            disabled={!currentItem}
            title={currentItem ? '导出当前 trace 的 OTLP/JSON' : '先选一条调用'}
            onClick={() => currentItem && exportOtel(currentItem, currentReplay)}
          >
            导出 OpenTelemetry
          </button>
          {/* 观测后端已接入时，这颗按钮就是真正有用的那件事：跳到后端看这条 trace。
              没接入才回到原型的「接入 Langfuse」。 */}
          {link
            ? <a className="primary" href={link} target="_blank" rel="noopener noreferrer">
                在 {tracing?.backend === 'langfuse' ? 'Langfuse' : 'LangSmith'} 打开 ↗
              </a>
            : <button
                className="primary"
                disabled={!onOpenModal}
                title={onOpenModal ? undefined : '接入向导由应用外壳挂载，当前实例未启用'}
                onClick={() => onOpenModal?.('langfuse')}
              >
                接入 Langfuse
              </button>}
        </div>
      </div>

      {error && <div className="audit-error">读取追踪数据失败：{error}</div>}

      <StatTiles stats={stats} />

      <div className="trace-layout">
        <div className="card">
          <div className="card-head">
            <div><strong>最近执行</strong><p>点击查看节点级 Span</p></div>
            <span className="status">{items ? `${items.length} 条` : '读取中'}</span>
          </div>
          <div className="trace-list">
            {items?.map(item => (
              <button
                className={`trace-item ${selected === item.trace_id ? 'active' : ''}`}
                key={item.trace_id + item.ts}
                onClick={() => setSelected(item.trace_id)}
              >
                <i className={`trace-status ${item.ok ? '' : 'warn'}`}>{item.ok ? '✓' : '!'}</i>
                <span>
                  <strong>{item.question || `（${KIND_NAMES[item.kind] ?? item.kind}）`}</strong>
                  <small>
                    {item.role || '未记录'} · {fmtTime(item.ts)} ·{' '}
                    {item.ok ? secs(item.elapsed_ms) : item.rejected_by === 'INTERRUPTED' ? '已中断' : '已拦截'}
                  </small>
                </span>
                <code>{item.trace_id.slice(0, 6)}</code>
              </button>
            ))}
            {items?.length === 0 && <div className="audit-empty">窗口内没有调用记录</div>}
          </div>
        </div>

        <div className="card trace-detail">
          <TraceDetail item={currentItem} replay={currentReplay} replayOn={!!stats?.replay_api} />
        </div>
      </div>

      <ObserveGrid stats={stats} />
    </div>
  )
}

function StatTiles({ stats }: { stats: AuditStats | null }) {
  if (!stats) return <div className="stats"><div className="stat"><span>读取中…</span></div></div>

  const modelCalls = Object.values(stats.by_model).reduce((sum, m) => sum + m.calls, 0)
  const avgTokens = modelCalls > 0 ? Math.round((stats.tok_in + stats.tok_out) / modelCalls) : null
  const pct = (v: number | null) => v == null ? NA : `${Math.round(v * 100)}%`

  return (
    <div className="stats">
      <div className="stat">
        <span>近 {stats.days} 天调用</span><strong>{stats.calls.toLocaleString()}</strong>
        <small>拦截 {stats.blocked} 次</small>
      </div>
      <div className="stat">
        <span>P95 总耗时</span><strong>{secs(stats.elapsed_p95_ms)}</strong>
        {/* 样本量必须一起给：7 次调用的 P95 基本等于最慢那次，当成稳定指标读会出错 */}
        <small>P50 {secs(stats.elapsed_p50_ms)} · 样本 {stats.calls} 次</small>
      </div>
      <div className="stat">
        <span>具备步骤级 trace</span><strong>{pct(stats.trace_complete)}</strong>
        <small>按记录如实计算</small>
      </div>
      <div className="stat">
        <span>平均 Token</span><strong>{avgTokens?.toLocaleString() ?? NA}</strong>
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
  const steps = d?.steps ?? []
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
        <span className={`status ${item.ok ? '' : 'wait'}`}>{KIND_NAMES[item.kind] ?? item.kind}</span>
      </div>

      {/* 前六格照原型的字段与顺序；模型 / SQL Hash / 数据源 askdb 没有落在 trace 上，
          留占位不编数。后六格是本实例真有、原型没有的事实，接着排满第二行。 */}
      <div className="trace-facts">
        <div className="trace-fact"><span>总耗时</span><strong>{secs(item.elapsed_ms)}</strong></div>
        <div className="trace-fact"><span>模型</span><strong>{NA}</strong></div>
        <div className="trace-fact"><span>Token</span><strong>{d ? `${d.tok_in ?? 0}+${d.tok_out ?? 0}` : NA}</strong></div>
        <div className="trace-fact"><span>工具调用</span><strong>{steps.length ? toolCalls(steps) : NA}</strong></div>
        <div className="trace-fact"><span>SQL Hash</span><strong>{NA}</strong></div>
        <div className="trace-fact"><span>数据源</span><strong>{NA}</strong></div>

        <div className="trace-fact"><span>角色</span><strong>{item.role || NA}</strong></div>
        <div className="trace-fact"><span>轮次</span><strong>{item.attempts ?? NA}</strong></div>
        <div className="trace-fact"><span>返回行</span><strong>{item.rows_returned ?? NA}</strong></div>
        <div className="trace-fact"><span>成本</span><strong>¥{item.cost_cny ?? 0}</strong></div>
        <div className="trace-fact"><span>步数</span><strong>{d?.step_count ?? item.step_count ?? NA}</strong></div>
        <div className="trace-fact"><span>会话线程</span><strong title={item.thread_id ?? ''}>{item.thread_id || NA}</strong></div>

        <div className="trace-fact wide"><span>命中表</span><strong>{d?.tables_hit?.join(' · ') || NA}</strong></div>
        <div className="trace-fact wide"><span>命中口径</span><strong>{d?.metrics_hit?.join(' · ') || NA}</strong></div>
        <div className="trace-fact wide"><span>命中规则</span><strong>{d?.rules_fired?.join(' · ') || NA}</strong></div>
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
            <div className={`trace-node ${(STEP_TYPE[step.step] ?? '').toLowerCase()} ${step.status === 'ok' ? '' : 'warn'}`}>
              <strong>{STEP_NAMES[step.step] ?? step.step}</strong>
              <small>{step.ms}ms</small>
            </div>
            {i < steps.length - 1 && <i className="trace-arrow">→</i>}
          </Fragment>
        ))}
      </div>

      <div className="span-table">
        <div className="span-table-title">
          <strong>Span 明细</strong><span>按开始时间排序</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>类型</th><th>Span</th><th>输入摘要</th><th>输出摘要</th><th>耗时</th><th>状态</th></tr>
            </thead>
            <tbody>
              {steps.map((step, i) => (
                <tr key={`${step.step}-${i}`}>
                  <td><span className={`span-type ${(STEP_TYPE[step.step] ?? 'sys').toLowerCase()}`}>{STEP_TYPE[step.step] ?? 'SYS'}</span></td>
                  <td>{STEP_NAMES[step.step] ?? step.step}</td>
                  {/* 原型这两列是「输入/输出摘要」。askdb 只记一条 note（该步的结果说明），
                      放在输出侧；输入侧只有 prompt token 数是真的，没有就留占位。 */}
                  <td>{step.tok_in ? `prompt ${step.tok_in.toLocaleString()} tok` : NA}</td>
                  <td className="span-note" title={step.note ?? ''}>
                    {step.note || NA}{step.tok_out ? ` · ${step.tok_out.toLocaleString()} tok` : ''}
                  </td>
                  <td>{step.ms}ms</td>
                  <td className={step.status === 'ok' ? 'good' : 'bad'}>{step.status.toUpperCase()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

/** 原型底部的两张集成卡。左卡的「已接入 / 未接入」按 /api/audit/stats 的 tracing 如实显示，
 *  右卡的隐私口径按 replay_api 开关如实显示 —— 这两句话写死就是在替部署方担保。 */
function ObserveGrid({ stats }: { stats: AuditStats | null }) {
  const tracing = stats?.tracing
  const connected = !!tracing?.enabled
  const backend = tracing?.backend === 'langsmith' ? 'LangSmith' : 'Langfuse'
  const replayOn = !!stats?.replay_api

  return (
    <div className="observe-grid">
      <div className="integration-card">
        <div className="integration-top">
          <div>
            <h3>{backend} 集成预留</h3>
            <p>Agent Harness 统一产生 Trace/Span；后期接入 {backend} 时不需要改动业务节点。</p>
          </div>
          <span className={`status ${connected ? '' : 'wait'}`}>{connected ? 'CONNECTED' : 'NOT CONNECTED'}</span>
        </div>
        <div className="attribute-list">
          <code>trace_id</code><code>role</code><code>thread_id</code><code>kind</code>
          <code>tables_hit</code><code>rules_fired</code><code>rejected_by</code><code>elapsed_ms</code>
        </div>
      </div>
      <div className="integration-card">
        <div className="integration-top">
          <div>
            <h3>隐私上报策略</h3>
            <p>默认只发送元数据和统计量；原始 Prompt、SQL 和查询结果保持关闭。</p>
          </div>
          <span className={`status ${replayOn ? 'wait' : ''}`}>{replayOn ? 'REPLAY OPEN' : 'SAFE DEFAULT'}</span>
        </div>
        <div className="attribute-list">
          <code>prompt: REDACTED</code>
          <code>sql: {replayOn ? 'REPLAY API' : 'OFF'}</code>
          <code>result: OFF</code>
        </div>
      </div>
    </div>
  )
}

/* ---------- 导出 OpenTelemetry ----------
 *
 * 原型这颗按钮只弹一句提示。这里按 OTLP/JSON 的 resourceSpans 结构把当前这条 trace
 * 真的写成文件下载 —— 不引依赖，浏览器 Blob 就够。取不到 replay 时退化成一条根 span，
 * 那也是真实的（审计流水里确实只有这一层）。
 */
function hex16(input: string): string {
  // OTLP 的 traceId/spanId 必须是定长 hex，askdb 的 trace_id 不是。做个稳定摘要，
  // 原值同时写进 attributes，回查不丢。
  let h1 = 0x811c9dc5, h2 = 0x01000193
  for (let i = 0; i < input.length; i++) {
    h1 = Math.imul(h1 ^ input.charCodeAt(i), 16777619) >>> 0
    h2 = Math.imul(h2 + input.charCodeAt(i), 2654435761) >>> 0
  }
  return (h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0'))
}

const attr = (key: string, value: string | number | boolean) => ({
  key,
  value: typeof value === 'number'
    ? { intValue: String(Math.round(value)) }
    : typeof value === 'boolean' ? { boolValue: value } : { stringValue: value },
})

function exportOtel(item: AuditItem, replay: ReplayResult | null) {
  const data: Replay | null = replay?.status === 'ok' ? replay.data : null
  const traceId = hex16(item.trace_id) + hex16(item.trace_id + '#')
  const startNs = BigInt(new Date(item.ts).getTime() || Date.now()) * 1000000n

  const spans: unknown[] = [{
    traceId,
    spanId: hex16(item.trace_id + ':root'),
    name: `askdb.${item.kind}`,
    kind: 1,
    startTimeUnixNano: String(startNs),
    endTimeUnixNano: String(startNs + BigInt(Math.round(item.elapsed_ms)) * 1000000n),
    attributes: [
      attr('askdb.trace_id', item.trace_id),
      attr('askdb.kind', item.kind),
      attr('askdb.role', item.role || 'unknown'),
      attr('askdb.multi_step', !!item.multi_step),
      attr('askdb.attempts', item.attempts ?? 0),
      attr('askdb.rows_returned', item.rows_returned ?? 0),
      attr('askdb.cost_cny', String(item.cost_cny ?? 0)),
      ...(item.rejected_by ? [attr('askdb.rejected_by', item.rejected_by)] : []),
    ],
    status: { code: item.ok ? 1 : 2 },
  }]

  let cursor = startNs
  for (const [i, step] of (data?.steps ?? []).entries()) {
    const end = cursor + BigInt(Math.round(step.ms)) * 1000000n
    spans.push({
      traceId,
      spanId: hex16(`${item.trace_id}:${step.step}:${i}`),
      parentSpanId: hex16(item.trace_id + ':root'),
      name: step.step,
      kind: 1,
      startTimeUnixNano: String(cursor),
      endTimeUnixNano: String(end),
      attributes: [
        attr('askdb.span_type', STEP_TYPE[step.step] ?? 'SYS'),
        attr('askdb.step_label', STEP_NAMES[step.step] ?? step.step),
        ...(step.tok_in ? [attr('llm.usage.prompt_tokens', step.tok_in)] : []),
        ...(step.tok_out ? [attr('llm.usage.completion_tokens', step.tok_out)] : []),
        ...(step.note ? [attr('askdb.note', step.note)] : []),
      ],
      status: { code: step.status === 'ok' ? 1 : 2 },
    })
    cursor = end
  }

  const payload = {
    resourceSpans: [{
      resource: { attributes: [attr('service.name', 'askdb'), attr('askdb.trace_id', item.trace_id)] },
      scopeSpans: [{ scope: { name: 'askdb.agent' }, spans }],
    }],
  }

  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `otel-${item.trace_id}.json`
  a.click()
  URL.revokeObjectURL(url)
}
