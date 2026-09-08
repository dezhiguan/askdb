import { PageHeader } from '../components/AppShell'
import { useEffect, useMemo, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink, tracingReachable,
  type AuditItem, type AuditList, type AuditStats, type Me, type Replay, type ReplayResult,
} from '../api'
import { KIND_NAMES, STEP_NAMES } from '../traceSteps'


function fmtTime(ts: string): string {
  /* 空值必须先挡掉。**new Date(null) 不是 NaN，是纪元 0** —— 只判 NaN 的话，
     没有 ts 的老审计记录会被格式化成「1970-01-01 08:00」，即凭空编出一个
     看起来合理的时间。宁可显示占位，也不要显示一个假的。 */
  if (!ts) return '—'
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

export function AuditPage({ me }: { me: Me | null }) {
  // 导出是**把数据带出这套系统**，比在页面上看一眼重一档：带走的那份文件
  // 之后谁看、存在哪里，审计里都记不到。所以这一个动作要求登录，
  // 与页面上原文可见与否（那是 text_visible 说了算）是两条独立的判据。
  const signedIn = !!me?.username
  // 原文可见与否**以后端返回的 text_visible 为准**，不用 me 自己推。
  // 前端推一遍就多一处判据，两处迟早分叉 —— 而这一处分叉的后果是
  // 页面显示"已遮蔽"、接口却照样把原文发了出来。
  const textVisible = (list: AuditList | null) => list?.text_visible !== false
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
      fmtTime(item.ts), item.trace_id, item.user || DASH, item.role || DASH, item.question ?? '',
      item.source_name || item.source || DASH,
      guardText(item), (item.elapsed_ms / 1000).toFixed(1), String(item.cost_cny ?? 0),
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
            {/* 导出的就是问题原文 CSV。未登录一律禁用：文件一旦落地就脱离了
                这套系统的审计，至少要让带走它的人有名有姓。原文在页面上是否
                可见另有 text_visible 管，两者都不满足就都不给导 */}
            <button
              className="ghost"
              onClick={exportReport}
              disabled={!signedIn || !list || list.items.length === 0 || !textVisible(list)}
              title={!signedIn
                ? '导出审计报告需要登录：文件带走后不再受这套系统的审计约束；未登录可以看统计、护栏结果与耗时成本'
                : (list && !textVisible(list)
                  ? '导出内容包含各条记录的问题原文，当前身份看不到原文，因此不能导出'
                  : undefined)}
            >
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
              <AuditRow key={item.trace_id + item.ts} item={item} stats={stats}
                textVisible={textVisible(list)} onReplay={openReplay} />
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

/** 审批放行耗时。分钟到小时是这条链路的常态（要等人），
 *  所以不套用别处的 ms/s 写法 —— 「4231s」没人读得出那是一小时多。 */
function fmtWait(ms: number | null | undefined): string {
  if (ms == null) return DASH
  const sec = Math.round(ms / 1000)
  if (sec < 60) return `${sec} 秒`
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟`
  const h = sec / 3600
  return h < 24 ? `${h.toFixed(1)} 小时` : `${(h / 24).toFixed(1)} 天`
}

/** 这张卡的两个数窗口不同，不解释清楚会被当成同一个口径读。
 *  只进 title，不落到页面上 —— 版式照原型，旁白不上屏。 */
function approvalHint(stats: AuditStats): string {
  const a = stats.approval
  if (!a || a.pending == null) return '审批流水这次没读出来，不是没有待审批'
  const tail = a.decided ? `；平均放行按近 ${stats.days} 天做出的 ${a.decided} 次决策计算`
                         : `；近 ${stats.days} 天没有做出过审批决策，故无平均值`
  return `当前 ${a.pending} 条待审批（与任务中心同源，不受时间窗约束）${tail}`
}

function StatTiles({ stats }: { stats: AuditStats | null }) {
  if (!stats) return <div className="stats"><div className="stat"><span>读取中…</span></div></div>

  const { today, delta } = todayVsYesterday(stats)
  const passed = stats.calls - stats.blocked
  const passRate = stats.calls > 0 ? `通过率 ${(passed / stats.calls * 100).toFixed(1)}%` : DASH
  // 老后端不返回 approval —— 缺字段与"读不出来"是同一件事，都走 null 分支
  const approval = stats.approval ?? { pending: null, decided: null, avg_decide_ms: null }

  return (
    <div className="stats">
      <div className="stat"><span>今日查询</span><strong>{today.toLocaleString()}</strong><small>{delta}</small></div>
      <div className="stat">
        <span>策略通过</span><strong>{passed.toLocaleString()}</strong>
        <small>{passRate} · 近 {stats.days} 天</small>
      </div>
      <div className="stat" title={approvalHint(stats)}>
        <span>人工审批</span><strong>{approval.pending ?? DASH}</strong>
        <small>平均 {fmtWait(approval.avg_decide_ms)} · 近 {stats.days} 天</small>
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

function AuditRow({ item, stats, textVisible, onReplay }: {
  item: AuditItem
  stats: AuditStats | null
  /** 问题原文可见（= 已登录）。复放返回的是 SQL 全文，比这一行更敏感，同一判据 */
  textVisible: boolean
  onReplay: (traceId: string) => void
}) {
  const replayOn = !!stats?.replay_api && textVisible
  const link = stats && item.kind !== 'sql' ? tracingLink(stats.tracing, item.trace_id) : null

  return (
    // 整行可点开判定链路复放（原型行为）；回放开关关着时行不可点，只当静态记录
    <tr
      className={replayOn ? 'audit-row' : undefined}
      onClick={replayOn ? () => onReplay(item.trace_id) : undefined}
    >
      <td className="mono">{fmtTime(item.ts)}</td>
      <td className="mono">{item.trace_id}</td>
      {/* 原型是「林晓 / 产品」一格。账号与角色都由审计记录如实给出。
          空账号有两种来路，措辞必须分开：未登录看这一页时后端会连同问题原文
          一起把发起人抹掉（textVisible=false），而登录后仍为空的那些，是那次
          调用本来就没有登录发起 —— 都写成 — 会把"你看不到"读成"没有人"。 */}
      <td title={item.user
        ? `发起人 ${item.user} · 生效角色 ${item.role || DASH}`
        : textVisible ? '这次调用未登录发起，只记录了生效角色' : '登录后可见发起人'}>
        {item.user
          ? item.user
          : <span className="audit-na">{textVisible ? '匿名' : DASH}</span>} / {item.role || DASH}
      </td>
      <td className="audit-question" title={item.question ?? ''}>
        {/* question 为 null 是"看不到"，不是"没问过" —— 空着会被读成后者 */}
        {item.question ?? <span className="mask">登录后可见</span>}
      </td>
      {/* 多源之后每条记录都落了打在哪个源上（少了它，同一条 SQL 在不同源上的
          结果事后对不上账）。名字缺失时退回 id，与执行追踪页同一口径；
          两者都没有的是多源之前的老记录，如实说清楚而不是含糊一个 —。 */}
      <td className={item.source_name || item.source ? undefined : 'audit-na'}
          title={item.source
            ? `${item.source_name || DASH} · source_id ${item.source}`
            : '这条记录写在多源之前，没有落数据源'}>
        {item.source_name || item.source || DASH}
      </td>
      <td><GuardBadge item={item} /></td>
      <td className="num">{(item.elapsed_ms / 1000).toFixed(1)}s</td>
      <td className="num">¥{item.cost_cny ?? 0}</td>
      <td onClick={event => event.stopPropagation()}>
        {replayOn
          ? <button className="link-button" onClick={() => onReplay(item.trace_id)}>复放</button>
          : <span
              className="link-disabled"
              title={!textVisible
                ? '复放会返回这次调用的 SQL 全文与问题原文，登录后才能查看'
                : 'replay_api 未开启（连真实数据源的实例默认关闭）'}
            >复放</span>}
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
  // 续跑前置校验没过（权限收窄 / 库连不上 / 表结构变了）。**不是拦截也不是终态** ——
  // 检查点还在，条件恢复后这条线程照样能续，措辞上不能和护栏拦截混为一谈。
  if (item.rejected_by === 'RESUME_BLOCKED') {
    return <span className="status wait">续跑校验未过 · 可重试</span>
  }
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
