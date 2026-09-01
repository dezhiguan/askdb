import { useEffect, useMemo, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink,
  type AuditItem, type AuditList, type AuditStats, type Replay, type ReplayResult,
} from '../api'
import { PageHeader } from '../components/AppShell'

/** 图节点的中文名。后端给的是节点 id，页面上直接显示 id 没人看得懂。 */
const STEP_NAMES: Record<string, string> = {
  quota: '配额检查',
  schema_recall: 'Schema 召回',
  plan: '单步/多步判定',
  generate_sql: 'SQL 生成',
  guard: '静态校验',
  dry_run: 'EXPLAIN 干跑',
  execute: '只读执行',
  assess: '结果自检',
  reflect: '反思重试',
  finalize: '结果与溯源',
  interrupted: '执行中断',
}

const KIND_NAMES: Record<string, string> = { ask: '提问', sql: '直查', resume: '续跑' }

function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)

type Drawer =
  | { mode: 'replay'; traceId: string; result: ReplayResult | null }
  | { mode: 'cost' }
  | null

export function AuditPage() {
  const [stats, setStats] = useState<AuditStats | null>(null)
  const [error, setError] = useState('')

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [kind, setKind] = useState('')
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  const [drawer, setDrawer] = useState<Drawer>(null)

  // 输入即查会把每个字符都打成一次请求，审计文件是全量读的，代价不低
  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(queryInput.trim()); setPage(1) }, 300)
    return () => window.clearTimeout(timer)
  }, [queryInput])

  useEffect(() => {
    let alive = true
    fetchAuditStats()
      .then(value => { if (alive) setStats(value) })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

  // 加载态由「已加载的那次请求」与「当前想要的那次请求」是否同一把 key 推导，
  // 而不是在 effect 里同步 setLoading —— 后者会多触发一轮渲染
  const requestKey = `${page}|${pageSize}|${query}|${kind}`
  const [loaded, setLoaded] = useState<{ key: string; data: AuditList } | null>(null)

  useEffect(() => {
    let alive = true
    fetchAudit({ page, pageSize, q: query, kind })
      .then(value => { if (alive) { setLoaded({ key: requestKey, data: value }); setError('') } })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [requestKey, page, pageSize, query, kind])

  const list = loaded?.data ?? null
  const loading = loaded?.key !== requestKey
  const pages = list ? Math.max(Math.ceil(list.total / list.page_size), 1) : 1

  const openReplay = async (traceId: string) => {
    setDrawer({ mode: 'replay', traceId, result: null })
    const result = await fetchReplay(traceId)
    setDrawer(current =>
      current && current.mode === 'replay' && current.traceId === traceId
        ? { ...current, result }
        : current)
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="Phase 2 · Full Traceability"
        title="审计中心"
        description="一次调用一条记录，被护栏拦截的同样留痕。列表不含 SQL 文本，判定细节走复放。"
      />

      {error && <div className="audit-error">读取审计数据失败：{error}</div>}

      <StatTiles stats={stats} onOpenCost={() => setDrawer({ mode: 'cost' })} />

      <div className="audit-filters">
        <input
          value={queryInput}
          onChange={event => setQueryInput(event.target.value)}
          placeholder="按 trace_id 或问题关键词检索"
        />
        <select value={kind} onChange={event => { setKind(event.target.value); setPage(1) }}>
          <option value="">全部类型</option>
          <option value="ask">提问</option>
          <option value="sql">直查 SQL</option>
          <option value="resume">续跑</option>
        </select>
        <select value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1) }}>
          {[10, 20, 50].map(size => <option key={size} value={size}>每页 {size} 条</option>)}
        </select>
      </div>

      <section className="card table-scroll">
        <table className="audit-table">
          <thead>
            <tr>
              <th>时间</th><th>trace</th><th>类型</th><th>问题</th><th>护栏</th>
              <th className="num">耗时</th><th className="num">成本</th><th>复放</th><th>观测</th>
            </tr>
          </thead>
          <tbody>
            {list?.items.map(item => (
              <AuditRow key={item.trace_id + item.ts} item={item} stats={stats} onReplay={openReplay} />
            ))}
            {list && list.items.length === 0 && !loading && (
              <tr><td colSpan={9} className="audit-empty">没有匹配的记录</td></tr>
            )}
            {!list && loading && <tr><td colSpan={9} className="audit-empty">读取中…</td></tr>}
          </tbody>
        </table>
      </section>

      {list && list.total > 0 && (
        <div className="audit-pager">
          <span>共 {list.total} 条 · 第 {list.page} / {pages} 页</span>
          <span>
            <button className="ghost" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>‹ 上一页</button>
            <button className="ghost" disabled={page >= pages} onClick={() => setPage(p => p + 1)}>下一页 ›</button>
          </span>
        </div>
      )}

      {drawer && <AuditDrawer drawer={drawer} stats={stats} onClose={() => setDrawer(null)} />}
    </div>
  )
}

function StatTiles({ stats, onOpenCost }: { stats: AuditStats | null; onOpenCost: () => void }) {
  if (!stats) return <div className="stats stats-6"><div><span>读取中…</span></div></div>

  const tracing = stats.tracing
  return (
    <div className="stats stats-6">
      <div><span>近 {stats.days} 天调用</span><strong>{stats.calls.toLocaleString()}</strong></div>
      <div>
        <span>护栏拦截</span><strong>{stats.blocked}</strong>
        <small>拦截率 {pct(stats.block_rate)}</small>
      </div>
      <div>
        <span>具备步骤级 trace</span><strong>{pct(stats.trace_complete)}</strong>
        <small>按记录如实计算</small>
      </div>
      <button type="button" className="stat-clickable" onClick={onOpenCost}>
        <span>近 {stats.days} 天成本</span><strong>¥{stats.cost_cny}</strong>
        <small>点开看按日与按类分布 ↗</small>
      </button>
      <div>
        <span>判定链路回放</span><strong>{stats.replay_api ? '已开启' : '已关闭'}</strong>
        <small>observability.replay_api</small>
      </div>
      <div>
        <span>调用链观测</span>
        <strong>{tracing.enabled ? `${tracing.backend === 'langfuse' ? 'Langfuse' : 'LangSmith'} 已接入` : '未接入'}</strong>
        <small>{tracing.enabled ? `项目 ${tracing.project}` : '配置 LANGFUSE_* 或 LANGSMITH_* 后启用'}</small>
      </div>
    </div>
  )
}

function AuditRow({ item, stats, onReplay }: {
  item: AuditItem
  stats: AuditStats | null
  onReplay: (traceId: string) => void
}) {
  const replayOn = !!stats?.replay_api
  const link = stats && item.kind !== 'sql' ? tracingLink(stats.tracing, item.trace_id) : null

  return (
    <tr>
      <td className="mono">{fmtTime(item.ts)}</td>
      <td className="mono">{item.trace_id}</td>
      <td>{KIND_NAMES[item.kind] ?? item.kind}</td>
      <td className="audit-question" title={item.question ?? ''}>{item.question}</td>
      <td><GuardBadge item={item} /></td>
      <td className="num">{(item.elapsed_ms / 1000).toFixed(1)}s</td>
      <td className="num">¥{item.cost_cny ?? 0}</td>
      <td>
        {replayOn
          ? <button className="link-button" onClick={() => onReplay(item.trace_id)}>复放</button>
          : <span className="link-disabled" title="replay_api 未开启（连真实数据源的实例默认关闭）">复放</span>}
      </td>
      <td>
        {link
          ? <a className="link-button" href={link} target="_blank" rel="noopener noreferrer"
               title={`在项目 ${stats?.tracing.project} 内按 trace_id 过滤`}>
              {stats?.tracing.backend === 'langfuse' ? 'Langfuse' : 'LangSmith'} ↗
            </a>
          : <span className="link-disabled"
                  title={item.kind === 'sql' ? '直查不经模型，没有 run 树' : '调用链观测未接入'}>—</span>}
      </td>
    </tr>
  )
}

function GuardBadge({ item }: { item: AuditItem }) {
  if (item.ok) return <span className="status">通过</span>
  if (item.rejected_by === 'INTERRUPTED') return <span className="status wait">中断 · 可续跑</span>
  return <span className="status bad">{item.rejected_by} 拦截</span>
}

function AuditDrawer({ drawer, stats, onClose }: {
  drawer: NonNullable<Drawer>
  stats: AuditStats | null
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="audit-drawer">
        <header>
          <div>
            <div className="eyebrow">{drawer.mode === 'cost' ? 'COST BREAKDOWN' : 'TRACE REPLAY'}</div>
            <h3>{drawer.mode === 'cost' ? '成本与调用分布' : '判定链路复放'}</h3>
          </div>
          <button onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="drawer-body">
          {drawer.mode === 'cost'
            ? <CostBreakdown stats={stats} />
            : <ReplayView traceId={drawer.traceId} result={drawer.result} />}
        </div>
      </aside>
    </>
  )
}

function ReplayView({ traceId, result }: { traceId: string; result: ReplayResult | null }) {
  if (!result) return <p className="drawer-note">读取中…</p>

  if (result.status === 'rate_limited') {
    return <p className="drawer-note">回放接口被限流了，稍等一分钟再试。每次回放都要遍历检查点库，它不能当查询接口刷。</p>
  }
  if (result.status === 'not_found') {
    return (
      <>
        <p className="drawer-note">取不到这条记录：回放开关未开启，或记录不存在。</p>
        <p className="drawer-note">两种情况接口都返回 404，不做区分 —— 区分本身就是信息泄露。</p>
        <pre className="drawer-code">askdb replay {traceId}</pre>
      </>
    )
  }

  const d: Replay = result.data
  return (
    <>
      <p className="replay-question">{d.question}</p>
      <p className="drawer-note">
        {fmtTime(d.ts)} · {KIND_NAMES[d.kind] ?? d.kind} · org {d.org_id} · {d.attempts} 轮 ·
        ¥{d.cost_cny ?? 0} · {d.tok_in ?? 0}+{d.tok_out ?? 0} tok
        {d.thread_id && d.thread_id !== d.trace_id && <> · 关联线程 <span className="mono">{d.thread_id}</span></>}
      </p>

      <h4>步骤链</h4>
      {d.steps && d.steps.length > 0
        ? d.steps.map((step, i) => (
            <div className="replay-step" key={`${step.step}-${i}`}>
              <span className={`replay-dot ${step.status === 'ok' ? '' : 'bad'}`}>
                {step.status === 'ok' ? '✓' : '✕'}
              </span>
              <span>
                <b>{STEP_NAMES[step.step] ?? step.step}</b>
                {step.note && <small>{step.note}</small>}
              </span>
              <span className="replay-ms">
                {step.ms} ms{step.tok_in ? ` · ${step.tok_in}+${step.tok_out} tok` : ''}
              </span>
            </div>
          ))
        : <p className="drawer-note">该记录没有步骤明细</p>}

      {d.snapshots.length > 0 && <>
        <h4>检查点快照（{d.snapshots.length}）</h4>
        {d.snapshots.map((snap, i) => (
          <div className="replay-step" key={i}>
            <span className="replay-dot neutral">{i + 1}</span>
            <span>
              <b>第 {(snap.attempt ?? 0) + 1} 轮{snap.next?.length ? ` · 下一节点 ${snap.next.join(',')}` : ' · 终态'}</b>
              {snap.rejected_by && <small>拦截：{snap.rejected_by} {snap.error ?? ''}</small>}
            </span>
          </div>
        ))}
      </>}

      {d.sql_final && <>
        <h4>最终 SQL<span className="status">护栏通过 · 只读执行</span></h4>
        <pre className="drawer-code">{d.sql_final}</pre>
      </>}
      {d.sql_raw && d.sql_raw !== d.sql_final && (
        <details>
          <summary>模型原始 SQL（改写前）</summary>
          <pre className="drawer-code">{d.sql_raw}</pre>
        </details>
      )}

      <p className="drawer-note">结果行与注入提示词不在本接口返回范围（字段白名单，回放接口设计说明 §4.2）。</p>
    </>
  )
}

function CostBreakdown({ stats }: { stats: AuditStats | null }) {
  const days = useMemo(() => stats?.daily.slice(-14) ?? [], [stats])
  if (!stats) return <p className="drawer-note">读取中…</p>

  const max = Math.max(...days.map(d => d.cost_cny), 0.000001)
  return (
    <>
      <p className="replay-question">
        ¥{stats.cost_cny} · {stats.calls} 次调用 · {(stats.tok_in + stats.tok_out).toLocaleString()} tok
      </p>
      <div className="cost-bars">
        {days.map(day => (
          <i key={day.date}
             style={{ height: `${Math.max(3, Math.round(day.cost_cny / max * 100))}%` }}
             title={`${day.date} · ¥${day.cost_cny} · ${day.calls} 次`} />
        ))}
      </div>
      <div className="cost-axis">
        <span>{days[0]?.date ?? ''}</span>
        <span>按日成本 · 悬停看明细</span>
        <span>{days[days.length - 1]?.date ?? ''}</span>
      </div>

      <h4>按模型</h4>
      <table className="drawer-table">
        <thead><tr><th>模型</th><th className="num">次数</th><th className="num">成本</th></tr></thead>
        <tbody>
          {Object.entries(stats.by_model).map(([model, value]) => (
            <tr key={model}><td className="mono">{model}</td><td className="num">{value.calls}</td><td className="num">¥{value.cost_cny}</td></tr>
          ))}
          {Object.keys(stats.by_model).length === 0 && (
            <tr><td colSpan={3} className="audit-empty">窗口内没有经模型的调用</td></tr>
          )}
        </tbody>
      </table>

      <h4>按类型</h4>
      <table className="drawer-table">
        <tbody>
          {Object.entries(stats.by_kind).map(([k, v]) => (
            <tr key={k}><td>{KIND_NAMES[k] ?? k}</td><td className="num">{v}</td></tr>
          ))}
        </tbody>
      </table>

      <h4>按拦截规则</h4>
      <table className="drawer-table">
        <tbody>
          {Object.entries(stats.by_rule).map(([rule, count]) => (
            <tr key={rule}><td className="mono">{rule}</td><td className="num">{count}</td></tr>
          ))}
          {Object.keys(stats.by_rule).length === 0 && (
            <tr><td colSpan={2} className="audit-empty">窗口内没有拦截记录</td></tr>
          )}
        </tbody>
      </table>
    </>
  )
}
