import { PageHeader } from '../components/AppShell'
import { useEffect, useMemo, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchReplay, tracingLink, tracingReachable,
  type AuditItem, type AuditList, type AuditStats, type Me, type Replay, type ReplayResult,
} from '../api'
import { KIND_NAMES, STEP_NAMES, stepFailed } from '../traceSteps'
import { FilterBar, FilterChips, FilterSearch, type FilterChip } from '../components/FilterBar'
import { personName } from '../person'
import { rolesLabel } from '../roles'
import type { View } from '../types'


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

/** 下拉里"不筛"那一项的取值。**不能用空串**：空串是合法取值（未记录数据源 /
 *  匿名发起），拿它当哨兵那两档就永远选不中。选中它时对外传 undefined。 */
const ALL = '\u0000all'

const AUDIT_KIND_LABEL: Record<string, string> = {
  ask: '提问', sql: '直查 SQL', resume: '续跑',
}
const AUDIT_STATUS_LABEL: Record<string, string> = {
  ok: '通过', rejected: '已拦截', interrupted: '中断',
}
/** 与任务中心、成员名册同一套时间档（后端 audit.SINCE_CHOICES） */
const AUDIT_SINCE_LABEL: Record<string, string> = {
  all: '全部时间', today: '今天', '7d': '近 7 天', '30d': '近 30 天',
}

type Drawer =
  | { mode: 'replay'; traceId: string; result: ReplayResult | null }
  | { mode: 'cost' }
  | null

export function AuditPage({ me, onNavigate }: {
  me: Me | null
  /** 「观测」列要用它跳到执行追踪页并定位那条 trace（第二个参数就是 trace_id） */
  onNavigate: (view: View, focus?: string) => void
}) {
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
  const [query, setQuery] = useState('')
  /* 三个条件的"不筛"用 undefined 表示，不是空串：空串是**合法取值**
     （未记录数据源 / 匿名发起），拿它当哨兵那两档就永远选不中。 */
  const [status, setStatus] = useState('')
  const [source, setSource] = useState<string | undefined>(undefined)
  const [user, setUser] = useState<string | undefined>(undefined)
  const [since, setSince] = useState('all')
  const [drawer, setDrawer] = useState<Drawer>(null)

  useEffect(() => {
    let alive = true
    fetchAuditStats()
      .then(value => { if (alive) setStats(value) })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

  // 加载态由「已加载的那次请求」与「当前想要的那次请求」是否同一把 key 推导，
  // 而不是在 effect 里同步 setLoading —— 后者会多触发一轮渲染
  const requestKey = `${page}|${pageSize}|${query}|${kind}|${status}|${source}|${user}|${since}`
  const [loaded, setLoaded] = useState<{ key: string; data: AuditList } | null>(null)

  useEffect(() => {
    let alive = true
    fetchAudit({ page, pageSize, q: query, kind, status, source, user, since })
      .then(value => { if (alive) { setLoaded({ key: requestKey, data: value }); setError('') } })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [requestKey, page, pageSize, query, kind, status, source, user, since])

  // 任一条件变化就回到第一页 —— 停在第 6 页而筛完只剩 2 条，看到的是一片空白
  useEffect(() => { setPage(1) }, [query, kind, status, source, user, since, pageSize])

  const list = loaded?.data ?? null
  const loading = loaded?.key !== requestKey
  const pages = list ? Math.max(Math.ceil(list.total / list.page_size), 1) : 1

  /* 已选条件。每个都能单独摘掉 —— 一次只错一个条件时不该逼人整条重来。 */
  const auditChips: FilterChip[] = [
    query ? { label: '关键词', value: query, onClear: () => setQuery('') } : null,
    kind ? { label: '类型', value: AUDIT_KIND_LABEL[kind] ?? kind, onClear: () => setKind('') } : null,
    status ? { label: '结果', value: AUDIT_STATUS_LABEL[status] ?? status, onClear: () => setStatus('') } : null,
    source !== undefined
      ? {
        label: '数据源',
        value: (list?.sources ?? []).find(item => item.id === source)?.name ?? source,
        onClear: () => setSource(undefined),
      } : null,
    user !== undefined
      ? {
        label: '发起人',
        value: (() => {
          const hit = (list?.users ?? []).find(item => item.id === user)
          return personName(hit && hit.name !== hit.id ? hit.name : '', user, me) || hit?.name || user
        })(),
        onClear: () => setUser(undefined),
      } : null,
    since !== 'all'
      ? { label: '时间', value: AUDIT_SINCE_LABEL[since] ?? since, onClear: () => setSince('all') } : null,
  ].filter(Boolean) as FilterChip[]
  const resetFilters = () => {
    setQuery(''); setKind(''); setStatus('')
    setSource(undefined); setUser(undefined); setSince('all')
  }

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
    // 姓名与账号**各占一列**：表格里只显示人名（那是给人看的），
    // 导出的是审计凭据，事后要按账号对得上人
    const head = ['时间', 'trace_id', '姓名', '账号', '角色', '自然语言问题', '数据源', '策略结果', '耗时(s)', '成本(CNY)']
    const cell = (v: string) => `"${v.replace(/"/g, '""')}"`
    const rows = list.items.map(item => [
      fmtTime(item.ts), item.trace_id, item.user ? who(item, me) : DASH, item.user || DASH,
      item.role || DASH, item.question ?? '',
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

      {/* 筛选条与任务中心、成员名册同一套结构与类名（components/FilterBar）。
          原来这里只有关键词 + 类型两项，而接口早就支持按策略结果、数据源筛
          —— 执行追踪页用着，审计页没有入口。发起人与时间是这次一并补的。 */}
      <FilterBar standalone>
        <FilterSearch
          value={query}
          onCommit={setQuery}
          placeholder={textVisible(list) ? '搜索 trace ID 或自然语言问题…' : '搜索 trace ID…'}
        />
        <select className={kind ? 'on' : ''} aria-label="按类型筛选"
                value={kind} onChange={event => setKind(event.target.value)}>
          <option value="">全部类型</option>
          <option value="ask">提问</option>
          <option value="sql">直查 SQL</option>
          <option value="resume">续跑</option>
        </select>
        <select className={status ? 'on' : ''} aria-label="按策略结果筛选"
                value={status} onChange={event => setStatus(event.target.value)}>
          <option value="">全部结果</option>
          <option value="ok">通过</option>
          <option value="rejected">已拦截</option>
          <option value="interrupted">中断</option>
        </select>
        <select className={source === undefined ? '' : 'on'} aria-label="按数据源筛选"
                value={source ?? ALL}
                onChange={event => setSource(event.target.value === ALL ? undefined : event.target.value)}>
          <option value={ALL}>全部数据源</option>
          {(list?.sources ?? []).map(item => (
            <option key={item.id} value={item.id}>{item.name}</option>
          ))}
        </select>
        {/* 看不到原文的身份不给这个入口：后端对它 403，理由是"某某有 12 条命中"
            本身就把内容说出去了。给一个必然报错的下拉比没有更糟。 */}
        {textVisible(list) && (
          <select className={user === undefined ? '' : 'on'} aria-label="按发起人筛选"
                  value={user ?? ALL}
                  onChange={event => setUser(event.target.value === ALL ? undefined : event.target.value)}>
            <option value={ALL}>全部发起人</option>
            {(list?.users ?? []).map(item => (
              <option key={item.id} value={item.id}>
                {personName(item.name === item.id ? '' : item.name, item.id, me) || item.name || '（匿名）'}
              </option>
            ))}
          </select>
        )}
        <select className={since === 'all' ? '' : 'on'} aria-label="按时间筛选"
                value={since} onChange={event => setSince(event.target.value)}>
          {Object.entries(AUDIT_SINCE_LABEL).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <button className="ghost" disabled={!auditChips.length} onClick={resetFilters}>重置</button>
      </FilterBar>
      <FilterChips standalone chips={auditChips}
                   matched={list?.total ?? 0} total={list?.total_all ?? 0} />

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
              <AuditRow key={item.trace_id + item.ts} item={item} stats={stats} me={me}
                textVisible={textVisible(list)} onReplay={openReplay}
                onObserve={traceId => onNavigate('traces', traceId)} />
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
            {/* 每页条数原来在上面的筛选条里，与任务中心不是同一处 —— 收进分页条 */}
            <select value={pageSize} onChange={event => { setPageSize(Number(event.target.value)); setPage(1) }}>
              {[10, 20, 50].map(size => <option key={size} value={size}>每页 {size} 条</option>)}
            </select>
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

/** 观测列仍然点不了时，为什么 —— 只剩直查这一种，说清楚它没有 run 树。
 *  外部观测后端未接入 / 仅内网可达都不再是禁用理由：那两种情形下这一列
 *  改指站内执行追踪页（同一条 trace 的本地链路），见 AuditRow。 */
function observeHint(item: AuditItem): string {
  if (item.kind === 'sql') return '直查不经模型，没有 run 树'
  return '这条记录没有可展开的链路'
}

/** 观测列跳站内执行追踪时的说明：为什么是站内，而不是某个外部后端。 */
function inSiteObserveHint(stats: AuditStats | null): string {
  if (stats?.tracing.enabled && !tracingReachable(stats.tracing)) {
    // 自托管实例只在内网活着，外链点下去打的是访客自己机器的端口。
    // 本地 trace 本来就是复放依据，观测后端只是旁路 —— 指回站内不是降级。
    return '外部观测后端仅内网可达；在站内执行追踪页看这条链路'
  }
  return '在站内执行追踪页展开这条链路的节点与 Span'
}

function guardText(item: AuditItem): string {
  if (item.ok) return '通过'
  if (item.rejected_by === 'INTERRUPTED') return '中断 · 可续跑'
  return `${item.rejected_by} 拦截`
}

/** 这一行显示的发起人：有姓名就显示人（官德志），否则退回账号（guandezhi）。
 *  退回不是兜底凑数 —— 名册里没登记的账号、以及未登录时（姓名是 PII，
 *  后端不下发）就是只有账号，显示账号是如实说清楚"只知道这些"。
 *  当前登录者若接口漏了 user_name，用 me.display_name（与成员名册「姓名」同值）。 */
function who(item: AuditItem, me: Me | null): string {
  return personName(item.user_name, item.user, me)
}

function AuditRow({ item, stats, textVisible, onReplay, onObserve, me }: {
  item: AuditItem
  stats: AuditStats | null
  /** 问题原文可见（= 已登录）。复放返回的是 SQL 全文，比这一行更敏感，同一判据 */
  textVisible: boolean
  onReplay: (traceId: string) => void
  /** 没有可达的外部观测后端时，观测列跳站内执行追踪页 */
  onObserve: (traceId: string) => void
  me: Me | null
}) {
  const replayOn = !!stats?.replay_api && textVisible
  const link = stats && item.kind !== 'sql' ? tracingLink(stats.tracing, item.trace_id) : null
  const name = who(item, me)

  return (
    // 整行可点开判定链路复放（原型行为）；回放开关关着时行不可点，只当静态记录
    <tr
      className={replayOn ? 'audit-row' : undefined}
      onClick={replayOn ? () => onReplay(item.trace_id) : undefined}
    >
      <td className="mono">{fmtTime(item.ts)}</td>
      <td className="mono">{item.trace_id}</td>
      {/* 原型是「林晓 / 产品」一格：显示的是**人**，不是网关用户名 ——
          姓名由后端按名册补在 user_name 上（未登录不下发，姓名是 PII），
          取不到就退回账号。角色仍由审计记录如实给出。
          空账号有两种来路，措辞必须分开：未登录看这一页时后端会连同问题原文
          一起把发起人抹掉（textVisible=false），而登录后仍为空的那些，是那次
          调用本来就没有登录发起 —— 都写成 — 会把"你看不到"读成"没有人"。 */}
      <td title={item.user
        ? `发起人 ${name !== item.user ? `${name}（${item.user}）` : item.user} · 生效角色 ${rolesLabel(item.role) || DASH}（${item.role || DASH}）`
        : textVisible ? '这次调用未登录发起，只记录了生效角色' : '登录后可见发起人'}>
        {item.user
          ? name
          : <span className="audit-na">{textVisible ? '匿名' : DASH}</span>} / {rolesLabel(item.role) || DASH}
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
        {/* 有可达的外部后端就去那儿；没有（未接入 / 只在内网活着）**不再置灰** ——
            这条 trace 的节点链与 Span 站内本来就有一份（/api/trace，不挂在回放
            开关上），指过去比给一个点不动的破折号有用得多。直查没有 run 树，
            仍然只能是禁用态。 */}
        {link
          ? <a className="link-button" href={link} target="_blank" rel="noopener noreferrer"
               title={`在项目 ${stats?.tracing.project} 内按 trace_id 过滤`}>
              {stats?.tracing.backend === 'langfuse' ? 'Langfuse' : 'LangSmith'} ↗
            </a>
          : item.kind !== 'sql'
            ? <button className="link-button" title={inSiteObserveHint(stats)}
                      onClick={() => onObserve(item.trace_id)}>链路 →</button>
            : <span className="link-disabled" title={observeHint(item)}>{DASH}</span>}
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
              <div className={`timeline-row${stepFailed(step.status) ? ' bad' : ''}`} key={`${step.step}-${i}`}>
                <strong>
                  {step.ms} ms · {STEP_NAMES[step.step] ?? step.step}
                  {stepFailed(step.status) ? ' · 未通过' : ''}
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
  const [hover, setHover] = useState<number | null>(null)
  if (!stats) return <p className="drawer-note">读取中…</p>

  const max = Math.max(...days.map(d => d.cost_cny), 0.000001)
  // 悬停的是哪一根。原来只挂了 title —— 浏览器那个 tooltip 要停住一秒多才出，
  // 而这张图**全部的意思**就在那几个数字上：柱子只表达相对高低，成本 ¥0.28
  // 和 ¥0.0003 在这里可以画得一样高（按当日最大值归一），不给数就读不出量级。
  const hot = hover == null ? null : days[hover]
  return (
    <>
      <p className="replay-question">
        ¥{stats.cost_cny} · {stats.calls} 次调用 · {(stats.tok_in + stats.tok_out).toLocaleString()} tok
      </p>
      <div className="cost-bars" onMouseLeave={() => setHover(null)}>
        {days.map((day, i) => (
          // button 而不是 i：键盘 Tab 也要能逐日看数，读屏才念得出来。
          // title 保留 —— 触屏上长按仍走它，那是这里唯一的兜底。
          <button key={day.date} type="button" className={hover === i ? 'on' : ''}
                  onMouseEnter={() => setHover(i)}
                  onFocus={() => setHover(i)} onBlur={() => setHover(null)}
                  aria-label={`${day.date} 成本 ¥${day.cost_cny}，${day.calls} 次调用`}
                  title={`${day.date} · ¥${day.cost_cny} · ${day.calls} 次`}>
            <span style={{ height: `${Math.max(3, Math.round(day.cost_cny / max * 100))}%` }} />
          </button>
        ))}
        {hot && (
          // 贴着那一根出，落在图内：抽屉本身会滚，绝对定位到 body 上会飘走。
          // 靠右几根时改成向左展开，否则会被图的右边界切掉。
          <div className={`cost-tip ${hover != null && hover > days.length - 4 ? 'left' : ''}`}
               style={{ left: `${((hover ?? 0) + 0.5) / days.length * 100}%` }}>
            <b>{hot.date}</b>
            <span>¥{hot.cost_cny}</span>
            <span>{hot.calls.toLocaleString()} 次调用</span>
          </div>
        )}
      </div>
      <div className="cost-axis">
        <span>{days[0]?.date ?? ''}</span>
        <span>按日成本 · 高低按当日最大值归一</span>
        <span>{days[days.length - 1]?.date ?? ''}</span>
      </div>

      <h4>按模型</h4>
      <table className="drawer-table">
        <thead><tr><th>模型</th><th className="num">次数</th><th className="num">成本</th></tr></thead>
        <tbody>
          {/* 按成本降序：嵌入模型的次数可能与生成模型同量级，但金额差两个数量级，
              按插入序排会让"钱到底花在哪"要靠一行行读 */}
          {Object.entries(stats.by_model)
            .sort((a, b) => b[1].cost_cny - a[1].cost_cny)
            .map(([model, value]) => (
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
