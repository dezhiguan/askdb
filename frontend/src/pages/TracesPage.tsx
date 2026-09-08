import { PageHeader } from '../components/AppShell'
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchTraceChain, tracingLink,
  type AuditItem, type AuditStats, type ReplayStep, type TraceChain,
  type TraceChainResult, type Me,
} from '../api'
import type { ModalName, View } from '../types'
import { writeGuard } from '../writeGuard'
import { KIND_NAMES, STEP_NAMES, STEP_TYPE } from '../traceSteps'


function fmtTime(ts: string): string {
  /* 空值必须先挡掉。**new Date(null) 不是 NaN，是纪元 0** —— 只判 NaN 的话，
     没有 ts 的老审计记录会被格式化成「1970-01-01 08:00」，即凭空编出一个
     看起来合理的时间。宁可显示占位，也不要显示一个假的。 */
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const secs = (ms: number | null | undefined) => ms == null ? '—' : `${(ms / 1000).toFixed(2)}s`

/** 后端在这条 trace 上没有记录的字段，按原型的版位留占位符，不编数。 */
const NA = '—'

/** 滚动分页每次取多少条。12 条约是左栏一屏半 —— 一屏都填不满就触发不了滚动。 */
const PAGE_SIZE = 12

/** 工具/数据库类节点数 —— 原型「工具调用」那一格的真实口径。 */
const toolCalls = (steps: ReplayStep[]) =>
  steps.filter(s => STEP_TYPE[s.step] === 'TOOL' || STEP_TYPE[s.step] === 'DB').length

export function TracesPage({ focusTrace, onNavigate, onOpenModal, me }: {
  /** 要定位的 trace_id。工作台右栏「Agent 执行链路」带着刚跑完那条的 id
   *  进来，页面必须停在它身上 —— 默认选中的是最近一条，并发查询下那不是
   *  同一条，而两条长得一样，看的人不会发现自己在读别人的链路。
   *
   *  实现上就是把它当搜索词落进左栏：q 服务端本来就匹配 trace_id，
   *  于是列表收到这一条、右栏自然选中它，而搜索框里明摆着那个 id，
   *  清掉就回到完整流水 —— 不需要再造一套"高亮并滚动到某条"的机制。 */
  focusTrace?: string | null
  /** App 未传时退回点击侧栏导航（见 goTasks 注释） */
  onNavigate?: (view: View) => void
  /** 「接入 Langfuse」弹窗由 App 的 ModalLayer 挂载，未传时按钮置灰 */
  onOpenModal?: (modal: ModalName) => void
  me?: Me | null
} = {}) {
  /** 导出与接入向导按登录态置灰，与其余六页同一口径 ——
   *  展示（流水、节点链）匿名可见，动作要登录。 */
  const guard = writeGuard(me ?? null, '这个操作')
  const [stats, setStats] = useState<AuditStats | null>(null)
  // 原型第一格是「今日 Traces」。统计接口按窗口取，30 天那份不能拿来当今天讲，
  // 所以单独再要一份 days=1 —— 其余三格仍用 30 天窗口，样本太小的 P95 没有意义。
  const [today, setToday] = useState<AuditStats | null>(null)
  const [items, setItems] = useState<AuditItem[] | null>(null)
  /* 左栏改成"搜索 + 两个下拉 + 滚动分页"。三个筛选条件都走**服务端**：
     只筛已加载的那一页，等于"搜不到"和"这一页里没有"分不开。 */
  const [keyword, setKeyword] = useState(focusTrace ?? '')
  const [status, setStatus] = useState('')
  /** 数据源筛选。undefined = 不筛；'' 是合法取值（未记录数据源那一档） */
  const [source, setSource] = useState<string | undefined>(undefined)
  const [sources, setSources] = useState<{ id: string; name: string }[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loadingMore, setLoadingMore] = useState(false)
  const [selected, setSelected] = useState<string | null>(focusTrace ?? null)
  // 存成 {key, result}，切换 trace 时靠 key 不匹配自然回到「读取中」，
  // 不需要在 effect 里先同步 setChain(null) —— 那会多触发一轮渲染
  const [chain, setChain] = useState<{ key: string; result: TraceChainResult } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    Promise.all([fetchAuditStats(), fetchAuditStats(1)])
      .then(([s, t]) => { if (!alive) return; setStats(s); setToday(t) })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

  /* 输入即请求会把每个字母都打成一次查询。300ms 防抖后再落到 query 上，
     筛选条件一变回到第一页 —— 停在第 5 页而结果只剩 3 条会看到一片空白。 */
  const [query, setQuery] = useState(focusTrace ?? '')
  useEffect(() => {
    const timer = setTimeout(() => setQuery(keyword.trim()), 300)
    return () => clearTimeout(timer)
  }, [keyword])
  useEffect(() => { setPage(1) }, [query, status, source])

  useEffect(() => {
    let alive = true
    if (page === 1) setItems(null)
    else setLoadingMore(true)
    fetchAudit({ page, pageSize: PAGE_SIZE, q: query, kind: '', status, source })
      .then(list => {
        if (!alive) return
        setError('')
        setTotal(list.total)
        if (list.sources) setSources(list.sources)
        // 分页是**追加**：滚动加载的语义就是列表越来越长，不是换一页
        setItems(current => (page === 1 ? list.items : [...(current ?? []), ...list.items]))
        // 进页面就该看到东西：默认选中最近一次调用，不要求先点一下。
        // 换筛选条件后原来选中的那条可能已经不在列表里，同样回到第一条。
        if (page === 1) setSelected(list.items[0]?.trace_id ?? null)
      })
      .catch(e => { if (alive) setError(String(e.message || e)) })
      .finally(() => { if (alive) setLoadingMore(false) })
    return () => { alive = false }
  }, [page, query, status, source])

  const loaded = items?.length ?? 0
  const allLoaded = items !== null && loaded >= total

  /* 滚到底再往下拉一页。距底 40px 就触发：等到严格触底才加载，
     惯性滚动会先撞一下空白。 */
  const listRef = useRef<HTMLDivElement>(null)
  const loadMore = useCallback(() => {
    if (loadingMore || allLoaded || items === null) return
    setPage(current => current + 1)
  }, [loadingMore, allLoaded, items])
  const onListScroll = () => {
    const el = listRef.current
    if (!el) return
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) loadMore()
  }

  useEffect(() => {
    if (!selected) return
    let alive = true
    fetchTraceChain(selected).then(result => { if (alive) setChain({ key: selected, result }) })
    return () => { alive = false }
  }, [selected])

  // key 不匹配 = 还在读这一条。四种状态必须一直分开传到底：
  // 「读取中」「看不到」「读取失败」「拿到了但没有步骤」在页面上长得一样时，
  // 一次接口层的静默失效就会被读成"这条调用本来就没有链路"（2026-09-07 撞过）。
  const currentResult: TraceChainResult | { status: 'loading' } =
    chain && chain.key === selected ? chain.result : { status: 'loading' }
  const currentChain = currentResult.status === 'ok' ? currentResult.data : null
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
      <PageHeader
        title="智能体执行追踪"
        description="查看每次查询经过的模型、工具、策略和数据库节点，为 Langfuse/OpenTelemetry 预留统一 Trace 结构。"
        action={
          <div className="card-actions">
            <button className="ghost" onClick={goTasks}>← 返回任务中心</button>
            <button
              className="ghost"
              disabled={!currentItem || !guard.can}
              title={!guard.can ? guard.props.title
                : currentItem ? '导出当前 trace 的 OTLP/JSON' : '先选一条调用'}
              onClick={() => currentItem && exportOtel(currentItem, currentChain)}
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
                  disabled={!onOpenModal || !guard.can}
                  title={!guard.can ? guard.props.title
                    : onOpenModal ? undefined : '接入向导由应用外壳挂载，当前实例未启用'}
                  onClick={() => onOpenModal?.('langfuse')}
                >
                  接入 Langfuse
                </button>}
          </div>
        }
      />

      {error && <div className="audit-error">读取追踪数据失败：{error}</div>}

      <StatTiles stats={stats} today={today} />

      <div className="trace-layout">
        <div className="card">
          <div className="card-head">
            <div><strong>最近执行</strong><p>点击查看节点级 Span</p></div>
            <span className="status">{items ? 'LIVE' : '读取中'}</span>
          </div>

          <div className="trace-filters">
            <label className="trace-search">
              <i aria-hidden="true">⌕</i>
              <input
                type="search"
                value={keyword}
                placeholder="搜索问题、Trace ID、发起人…"
                aria-label="搜索执行记录"
                onChange={event => setKeyword(event.target.value)}
              />
            </label>
            <div className="trace-filter-row">
              <select value={status} aria-label="按状态筛选" onChange={event => setStatus(event.target.value)}>
                <option value="">全部状态</option>
                <option value="ok">已完成</option>
                <option value="rejected">已拦截</option>
                <option value="interrupted">已中断</option>
              </select>
              {/* '' 是"未记录数据源"，所以"不筛"只能用另一个值表示 —— 用 __all__，
                  不能拿空串兼任 */}
              <select
                value={source === undefined ? '__all__' : source}
                aria-label="按数据源筛选"
                onChange={event => setSource(event.target.value === '__all__' ? undefined : event.target.value)}
              >
                <option value="__all__">全部数据源</option>
                {sources.map(item => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="trace-list" ref={listRef} onScroll={onListScroll}>
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
            {items?.length === 0 && (
              <div className="audit-empty">
                {query || status || source !== undefined ? '没有符合条件的执行记录' : '窗口内没有调用记录'}
              </div>
            )}
            {loadingMore && <div className="trace-loading">加载中…</div>}
          </div>

          {/* 加载了多少 / 一共多少必须写出来：滚动分页最容易让人以为"就这些了" */}
          <div className="trace-foot">
            <span>已加载 {loaded} / {total} 条</span>
            <button
              className="ghost"
              disabled={allLoaded || loadingMore || items === null}
              onClick={loadMore}
            >
              {items === null ? '读取中…' : allLoaded ? '已全部加载' : loadingMore ? '加载中…' : '加载更多'}
            </button>
          </div>
        </div>

        <div className="card trace-detail">
          <TraceDetail item={currentItem} chain={currentChain} result={currentResult} />
        </div>
      </div>

    </div>
  )
}

function StatTiles({ stats, today }: { stats: AuditStats | null; today: AuditStats | null }) {
  if (!stats) return <div className="stats"><div className="stat"><span>读取中…</span></div></div>

  const modelCalls = Object.values(stats.by_model).reduce((sum, m) => sum + m.calls, 0)
  const avgTokens = modelCalls > 0 ? Math.round((stats.tok_in + stats.tok_out) / modelCalls) : null
  const pct = (v: number | null | undefined) => v == null ? NA : `${Math.round(v * 100)}%`

  return (
    <div className="stats">
      <div className="stat">
        <span>今日 Traces</span><strong>{(today?.calls ?? 0).toLocaleString()}</strong>
        {/* 没有调用时 trace_complete 是 null —— 那句话就不该出现，
            「— 已关联审计」是把一个没有的比例硬写成一行字 */}
        <small>{today?.calls ? `${pct(today.trace_complete)} 已关联审计` : '今日暂无调用'}</small>
      </div>
      <div className="stat">
        <span>P95 总耗时</span><strong>{secs(stats.elapsed_p95_ms)}</strong>
        {/* 样本量必须一起给：7 次调用的 P95 基本等于最慢那次，当成稳定指标读会出错 */}
        <small>P50 {secs(stats.elapsed_p50_ms)} · 样本 {stats.calls} 次</small>
      </div>
      {/* 按模型**节点**算（判定/生成/自检/反思），不是按整次调用算 ——
          一次提问里模型可能被调三四次，其中一次失败后重试成功，
          按调用算会把这些失败全部抹掉。 */}
      <div className="stat">
        <span>模型调用成功率</span><strong>{pct(stats.model_success)}</strong>
        <small>
          {stats.model_calls
            ? `${stats.model_calls.toLocaleString()} 次模型节点 · ${stats.model_failed} 次失败`
            : '窗口内没有经模型的节点'}
        </small>
      </div>
      <div className="stat">
        <span>平均 Token</span><strong>{avgTokens?.toLocaleString() ?? NA}</strong>
        <small>{modelCalls > 0 ? `${modelCalls} 次经模型调用` : '窗口内没有经模型的调用'}</small>
      </div>
    </div>
  )
}

function TraceDetail({ item, chain, result }: {
  item: AuditItem | null
  chain: TraceChain | null
  /** 节点链这一次取的结果。chain 是它的 ok 分支，两个都要传：
   *  上面六格只需要值，下面的 Span 表还要说清"为什么没有值"。 */
  result: TraceChainResult | { status: 'loading' }
}) {
  if (!item) return <p className="trace-empty">左侧选一条调用查看节点明细。</p>

  const steps = chain?.steps ?? []
  const outcome = item.ok
    ? 'SUCCESS'
    : item.rejected_by === 'INTERRUPTED' ? 'INTERRUPTED' : `BLOCKED · ${item.rejected_by}`

  return (
    <>
      <div className="trace-detail-head">
        <div>
          <h3>{item.question || `（${KIND_NAMES[item.kind] ?? item.kind}）`}</h3>
          <p>{item.trace_id} · {outcome} · {item.multi_step ? 'MULTI-STEP' : 'ONE-SHOT'}</p>
        </div>
        {/* 原型这枚角标是「可信度 96」。askdb 不打可信度分，版位留着不编数。 */}
        <span className={`status ${item.ok ? '' : 'wait'}`}>可信度 {NA}</span>
      </div>

      {/* 字段与顺序严格照原型的六格，一格不多。数据来自 /api/trace（节点链）
          与流水本身 —— 不经回放，所以未登录、回放关闭时这六格照样是满的。 */}
      <div className="trace-facts">
        <div className="trace-fact"><span>总耗时</span><strong>{secs(item.elapsed_ms)}</strong></div>
        <div className="trace-fact"><span>模型</span><strong title={chain?.model ?? ''}>{chain?.model || NA}</strong></div>
        <div className="trace-fact"><span>Token</span><strong>{tokens(chain)}</strong></div>
        <div className="trace-fact"><span>工具调用</span><strong>{steps.length ? toolCalls(steps) : NA}</strong></div>
        <div className="trace-fact"><span>SQL Hash</span><strong title={chain?.sql_hash ?? ''}>{shortHash(chain?.sql_hash)}</strong></div>
        <div className="trace-fact"><span>数据源</span><strong title={item.source_name ?? ''}>{item.source_name || NA}</strong></div>
      </div>

      <TraceNodes steps={steps} result={result} />
    </>
  )
}

/** 「1,020」而不是「953+67」：原型那一格是一个数。分不清进出的时候
 *  两者都没有就留占位，不拿 0 顶上。 */
function tokens(chain: TraceChain | null): string {
  if (!chain || (chain.tok_in == null && chain.tok_out == null)) return NA
  return ((chain.tok_in ?? 0) + (chain.tok_out ?? 0)).toLocaleString()
}

/** 原型写的是「8ad2…91cf」—— 首尾各四位。整串放进 title，要对账时能拷走。 */
function shortHash(hash: string | null | undefined): string {
  if (!hash) return NA
  return hash.length <= 12 ? hash : `${hash.slice(0, 4)}…${hash.slice(-4)}`
}

/** 链路条与 Span 明细。版式照原型，不另起说明段落 ——
 *  唯一的例外是空表里那一行状态，它替代的是原来那片无从解释的空白。 */
function TraceNodes({ steps, result }: {
  steps: ReplayStep[]
  result: TraceChainResult | { status: 'loading' }
}) {
  /* 一行都没有时，那一行说的是**为什么**没有。
   *
   * 四种空态原来渲染成同一张只有表头的空表，于是"接口把这条挡掉了"和
   * "这条调用确实没有节点"在界面上无法区分 —— 2026-09-07 的那次静默失效
   * （/api/trace 拿启动配置判可见性，运行时数据源上的记录一律 404）
   * 从界面上找不到任何线索，只能去 curl 才知道是 404。
   *
   * 措辞按后端的约定收着说：记录不存在与无权同为 404（区分本身就是信息泄露），
   * 前端不替它区分，只说"当前看不到"和两种可能，不断言是哪一种。 */
  const empty =
    result.status === 'loading' ? '读取中…'
    : result.status === 'unavailable' ? '这条链路当前不可见：记录不存在，或它命中的表不在你此刻的可见范围内。'
    : result.status === 'failed' ? `节点链读取失败：${result.message}`
    : '这条调用没有留下节点记录。'
  return (
    <>
      {steps.length > 0 && (
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
      )}

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
              {steps.length === 0 && (
                <tr className="span-empty"><td colSpan={6}>{empty}</td></tr>
              )}
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

/* ---------- 导出 OpenTelemetry ----------
 *
 * 原型这颗按钮只弹一句提示。这里按 OTLP/JSON 的 resourceSpans 结构把当前这条 trace
 * 真的写成文件下载 —— 不引依赖，浏览器 Blob 就够。取不到节点链时退化成一条根 span，
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

function exportOtel(item: AuditItem, chain: TraceChain | null) {
  const data = chain
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
