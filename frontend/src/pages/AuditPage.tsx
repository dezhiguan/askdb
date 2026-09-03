import { PageHeader } from '../components/AppShell'
import { useEffect, useMemo, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink, tracingReachable,
  type AuditItem, type AuditList, type AuditStats, type Replay, type ReplayResult,
} from '../api'
import { KIND_NAMES, STEP_NAMES } from '../traceSteps'


function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)

/** 后端没有这个字段时统一占位，保持与原型一致的排版，不编造数值。 */
const DASH = '—'

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

  // 导出的是「当前这一屏、当前这组筛选」的真实审计行，不额外回源，
  // 也不把 SQL 文本塞进来 —— 列表本来就不含 SQL，导出同样不含。
  const exportReport = () => {
    if (!list || list.items.length === 0) return
    const head = ['时间', 'trace_id', '用户', '角色', '自然语言问题', '数据源', '策略结果', '耗时(s)', '成本(CNY)']
    const cell = (v: string) => `"${v.replace(/"/g, '""')}"`
    const rows = list.items.map(item => [
      fmtTime(item.ts), item.trace_id, DASH, item.role || DASH, item.question ?? '',
      DASH, guardText(item), (item.elapsed_ms / 1000).toFixed(1), String(item.cost_cny ?? 0),
    ].map(cell).join(','))
    const blob = new Blob(['﻿' + [head.map(cell).join(','), ...rows].join('\r\n')],
      { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `askdb-audit-p${list.page}-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="page">
      <PageHeader
        title="审计中心"
        description="追踪谁在什么时间，以什么权限提出了什么问题，最终执行了哪条 SQL。"
        action={
          <div className="card-actions">
            {/* 成本分布原来挂在统计卡上，那一排撤掉后入口挪到这里 —— 抽屉本身是真功能 */}
            <button className="ghost" onClick={() => setDrawer({ mode: 'cost' })}>成本分布</button>
            <button className="ghost" onClick={exportReport} disabled={!list || list.items.length === 0}>
              导出审计报告
            </button>
          </div>
        }
      />

      {error && <div className="audit-error">读取审计数据失败：{error}</div>}

      <StatTiles stats={stats} />

      <div className="audit-filters">
        <input
          value={queryInput}
          onChange={event => setQueryInput(event.target.value)}
          placeholder="搜索 trace ID 或自然语言问题…"
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
        <button
          className="secondary"
          onClick={() => { setQuery(queryInput.trim()); setPage(1) }}
        >筛选</button>
      </div>

      <section className="card table-scroll">
        <table className="audit-table">
          <thead>
            <tr>
              <th>时间</th><th>trace</th><th>用户 / 角色</th><th>自然语言问题</th>
              <th>数据源</th><th>策略结果</th>
              <th className="num">耗时</th><th className="num">成本</th><th>复放</th><th>观测</th>
            </tr>
          </thead>
          <tbody>
            {list?.items.map(item => (
              <AuditRow key={item.trace_id + item.ts} item={item} stats={stats} onReplay={openReplay} />
            ))}
            {list && list.items.length === 0 && !loading && (
              <tr><td colSpan={10} className="audit-empty">没有匹配的记录</td></tr>
            )}
            {!list && loading && <tr><td colSpan={10} className="audit-empty">读取中…</td></tr>}
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

/** 今日与昨日的调用量对比。窗口里不足两天、或昨天为 0 时不做除法，直接给占位。 */
function todayVsYesterday(stats: AuditStats): { today: number; delta: string } {
  const daily = stats.daily
  if (daily.length === 0) return { today: 0, delta: DASH }
  const today = daily[daily.length - 1].calls
  if (daily.length < 2) return { today, delta: DASH }
  const yesterday = daily[daily.length - 2].calls
  if (yesterday === 0) return { today, delta: DASH }
  const ratio = Math.round((today - yesterday) / yesterday * 100)
  return { today, delta: `较昨日 ${ratio >= 0 ? '+' : ''}${ratio}%` }
}

function StatTiles({ stats }: { stats: AuditStats | null }) {
  if (!stats) return <div className="stats"><div className="stat"><span>读取中…</span></div></div>

  const { today, delta } = todayVsYesterday(stats)
  const passed = stats.calls - stats.blocked
  const passRate = stats.calls > 0 ? `通过率 ${(passed / stats.calls * 100).toFixed(1)}%` : DASH

  return (
    <div className="stats">
      <div className="stat"><span>今日查询</span><strong>{today.toLocaleString()}</strong><small>{delta}</small></div>
      <div className="stat">
        <span>策略通过</span><strong>{passed.toLocaleString()}</strong>
        <small>{passRate} · 近 {stats.days} 天</small>
      </div>
      {/* 人工审批环节后端尚未落库，按原型排版占位，不拿别的指标顶替 */}
      <div className="stat" title="人工审批环节尚未接入，审计记录里没有这项">
        <span>人工审批</span><strong>{DASH}</strong><small>平均 {DASH}</small>
      </div>
      <div className="stat">
        <span>安全拦截</span><strong>{stats.blocked}</strong>
        <small>拦截率 {pct(stats.block_rate)} · 近 {stats.days} 天</small>
      </div>
    </div>
  )
}

/** 观测列为什么点不了 —— 三种原因说清楚，别都甩一句"未接入"。 */
function observeHint(item: AuditItem, stats: AuditStats | null): string {
  if (item.kind === 'sql') return '直查不经模型，没有 run 树'
  if (!stats || !stats.tracing.enabled) return '调用链观测未接入'
  if (!tracingReachable(stats.tracing)) {
    // 自托管实例只在内网活着。站内的「复放」是同一条链路的权威来源，
    // 不是降级替代 —— 本地 trace 才是复放依据，观测后端只是旁路。
    return '观测后端仅内网可达；这条链路请用左侧「复放」查看'
  }
  return '调用链观测未接入'
}

function guardText(item: AuditItem): string {
  if (item.ok) return '通过'
  if (item.rejected_by === 'INTERRUPTED') return '中断 · 可续跑'
  return `${item.rejected_by} 拦截`
}

function AuditRow({ item, stats, onReplay }: {
  item: AuditItem
  stats: AuditStats | null
  onReplay: (traceId: string) => void
}) {
  const replayOn = !!stats?.replay_api
  const link = stats && item.kind !== 'sql' ? tracingLink(stats.tracing, item.trace_id) : null

  return (
    // 整行可点开判定链路复放（原型行为）；回放开关关着时行不可点，只当静态记录
    <tr
      className={replayOn ? 'audit-row' : undefined}
      onClick={replayOn ? () => onReplay(item.trace_id) : undefined}
    >
      <td className="mono">{fmtTime(item.ts)}</td>
      <td className="mono">{item.trace_id}</td>
      {/* 原型是「林晓 / 产品」一格。审计记录里没有调用者账号，只有可见范围（角色），
          所以账号位恒为 —，等后端落库再填 */}
      <td title="审计记录未落调用者账号，仅记录生效角色">
        <span className="audit-na">{DASH}</span> / {item.role || DASH}
      </td>
      <td className="audit-question" title={item.question ?? ''}>{item.question}</td>
      {/* 单实例单数据源，逐条记录里不存数据源名 */}
      <td className="audit-na" title="审计记录未按条落数据源">{DASH}</td>
      <td><GuardBadge item={item} /></td>
      <td className="num">{(item.elapsed_ms / 1000).toFixed(1)}s</td>
      <td className="num">¥{item.cost_cny ?? 0}</td>
      <td onClick={event => event.stopPropagation()}>
        {replayOn
          ? <button className="link-button" onClick={() => onReplay(item.trace_id)}>复放</button>
          : <span className="link-disabled" title="replay_api 未开启（连真实数据源的实例默认关闭）">复放</span>}
      </td>
      <td onClick={event => event.stopPropagation()}>
        {link
          ? <a className="link-button" href={link} target="_blank" rel="noopener noreferrer"
               title={`在项目 ${stats?.tracing.project} 内按 trace_id 过滤`}>
              {stats?.tracing.backend === 'langfuse' ? 'Langfuse' : 'LangSmith'} ↗
            </a>
          : <span className="link-disabled" title={observeHint(item, stats)}>{DASH}</span>}
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
      <aside className="drawer show audit-drawer">
        <div className="drawer-head">
          <div>
            <div className="eyebrow">{drawer.mode === 'cost' ? 'COST BREAKDOWN' : `AUDIT-${drawer.traceId}`}</div>
            <h3>{drawer.mode === 'cost' ? '成本与调用分布' : '查询审计详情'}</h3>
          </div>
          <button className="drawer-close" onClick={onClose} aria-label="关闭">×</button>
        </div>
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
        <div className="code-line"><span>askdb replay {traceId}</span></div>
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
        ? <div className="timeline">
            {d.steps.map((step, i) => (
              <div className={`timeline-row${step.status === 'ok' ? '' : ' bad'}`} key={`${step.step}-${i}`}>
                <strong>
                  {step.ms} ms · {STEP_NAMES[step.step] ?? step.step}
                  {step.status === 'ok' ? '' : ' · 未通过'}
                </strong>
                <small>
                  {step.note || '无补充说明'}
                  {step.tok_in ? ` · ${step.tok_in}+${step.tok_out} tok` : ''}
                </small>
              </div>
            ))}
          </div>
        : <p className="drawer-note">该记录没有步骤明细</p>}

      {d.snapshots.length > 0 && <>
        <h4>检查点快照（{d.snapshots.length}）</h4>
        <div className="timeline">
          {d.snapshots.map((snap, i) => (
            <div className={`timeline-row${snap.rejected_by ? ' bad' : ''}`} key={i}>
              <strong>第 {(snap.attempt ?? 0) + 1} 轮{snap.next?.length ? ` · 下一节点 ${snap.next.join(',')}` : ' · 终态'}</strong>
              <small>{snap.rejected_by ? `拦截：${snap.rejected_by} ${snap.error ?? ''}` : '无拦截'}</small>
            </div>
          ))}
        </div>
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
