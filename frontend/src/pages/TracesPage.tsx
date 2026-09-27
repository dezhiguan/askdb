import { PageHeader } from '../components/AppShell'
import { ModalShell } from '../components/Modals'
import { MultiAgentTrace } from '../components/MultiAgentTrace'
import { ResultDetail } from '../components/ResultDetail'
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchAudit, fetchAuditStats, fetchLiveQuality, fetchTraceChain, fetchResult, tracingLink,
  type AuditItem, type AuditStats, type LiveQuality, type ReplayStep, type TraceChain,
  type TraceChainResult, type TraceResult, type Me,
} from '../api'
import type { ModalName, View } from '../types'
import { writeGuard } from '../writeGuard'
import { resultChecks, scoreOf, scoreTitle } from '../trust'
import { rolesLabel } from '../roles'
import { KIND_NAMES, STAGE_NAMES, STATUS_HINT, STEP_NAMES, STEP_TYPE, stepFailed, stepSoft } from '../traceSteps'


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

/** Span 明细一次铺多少行。一条 agent 链路动辄四五十个 span，整张表一次铺完
 *  会把详情卡撑到几屏高 —— 右栏其余内容（结果弹窗入口、六格摘要）被推得
 *  再也扫不到。20 行略高于表体一屏，滚到底才续下一批。 */
const SPAN_PAGE = 20

/** 真正的工具调用次数 —— **模型自己选的那几次**。
 *
 *  2026-09-12 修正。此前这里数的是 TOOL ∪ DB，而那两类里只有 tool_call 是
 *  模型选的：schema_recall 是进门必跑的确定性召回，dry_run / execute 是
 *  execute_sql 这一个工具内部的阶段（agent 只落一条 tool_call），单独出现时
 *  来自直查 /api/sql —— 那条路连模型都不过。
 *
 *  症状很直白：老管道的一条 trace 里模型一次工具都没选，这一格却写着 3。
 *  判据改成"谁决定要不要调它"：tools.REGISTRY 里那几个由模型挑的才算。
 *  老管道因此恒为 0 —— 那是事实，不是缺数据。 */
const isToolStep = (s: ReplayStep) => STEP_TYPE[s.step] === 'TOOL'
const toolCalls = (steps: ReplayStep[]) => steps.filter(isToolStep).length
const spanTypeName = (step: ReplayStep) => {
  const type = STEP_TYPE[step.step] ?? 'SYS'
  return type === 'MODEL' ? 'LLM' : type === 'DB' ? 'QUERY' : type
}

/** 这条链路是不是按主路径跑完的，以及不是的话代价在哪。
 *
 *  顶部横幅与六格 KPI 都读它。**每个数字背后都有一条具体的 span**，
 *  取不到就是 0 条、不显示，不从别处推。
 *
 *  在这之前，页面只渲染链路的**终态**：被重试救回来的失败没有 span，
 *  切了备选也只显示最终那个模型名 —— 一条降级完成的链路和一条干净链路
 *  长得一模一样，而这两种链路给出的答案，可信程度差得远。 */
interface ChainHealth {
  failed: ReplayStep[]
  soft: ReplayStep[]
  clean: boolean
  /** 失败的尝试各自烧掉的时间与 token —— 返工的账，不该混在总数里看不见 */
  wastedMs: number
  wastedTok: number
  /** 真正出活的模型；replaced 是被它顶掉的那个（没换过就是空串） */
  answering: string
  replaced: string
  toolFailed: number
  /** 工具/数据库步骤里能力降级的与零行的，**分开数** —— 一次如实返回零行
   *  不是"降级"，混成一个词会让人以为系统出了故障。 */
  toolDegraded: number
  toolEmpty: number
}

function chainHealth(steps: ReplayStep[]): ChainHealth {
  const failed = steps.filter(s => stepFailed(s.status))
  const soft = steps.filter(s => stepSoft(s.status))
  /* 出活的那个模型：优先取 generate_sql —— 答案是那条 SQL 查出来的。
     **不能笼统取"最后一条模型 span"**：多步链路里判定与自检也各是一次调用，
     而备选只在失败时顶上一次，下一次会回到主模型。按最后一条取的话，
     "生成切了备选、自检又回到主模型"这种链路会把回退整个抹平，
     下面那个 replaced 跟着变空，⇄ 那行小字就不出现了。 */
  /* 只看真正过模型的节点 —— Schema 召回现在也带 model（嵌入模型），
     不排除的话，生成失败的链路会把「模型」那一格显示成 text-embedding-v4，
     而嵌入模型一句 SQL 都没生成过。 */
  const answered = steps.filter(s => s.model && !stepFailed(s.status)
                                     && STEP_TYPE[s.step] === 'MODEL')
  const gen = [...answered].reverse().find(s => s.step === 'generate_sql')
  const winner = gen ?? answered[answered.length - 1]
  const answering = winner?.model ?? ''
  /* 被顶掉的主模型只在**同一步**里找：别的步骤上的失败与这一步换没换模型无关。 */
  const replaced = failed.find(s => s.model && s.model !== answering
                                    && (!winner || s.step === winner.step))?.model ?? ''
  return {
    failed,
    soft,
    clean: failed.length === 0 && soft.length === 0,
    wastedMs: failed.reduce((a, s) => a + (s.ms || 0), 0),
    wastedTok: failed.reduce((a, s) => a + (s.tok_in ?? 0) + (s.tok_out ?? 0), 0),
    answering,
    replaced,
    toolFailed: steps.filter(s => isToolStep(s) && stepFailed(s.status)).length,
    toolDegraded: steps.filter(s => isToolStep(s)
      && (s.status === 'degraded' || s.status === 'fallback')).length,
    toolEmpty: steps.filter(s => isToolStep(s) && s.status === 'empty').length,
  }
}

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
  // 保留本地评审入口的响应式布局；展示数据与正常入口共用当前 Trace。
  const prototypePreview = new URLSearchParams(window.location.search).get('local-prototype') === '1'
  /** 导出与接入向导按登录态置灰，与其余六页同一口径 ——
   *  展示（流水、节点链）匿名可见，动作要登录。 */
  const guard = writeGuard(me ?? null, '这个操作')
  const [stats, setStats] = useState<AuditStats | null>(null)
  // 顶部五项按原型使用近 24 小时运行质量窗口；审计统计仍单独提供追踪集成状态。
  const [quality, setQuality] = useState<LiveQuality | null>(null)
  const [qualityError, setQualityError] = useState('')
  const [qualityUpdatedAt, setQualityUpdatedAt] = useState<Date | null>(null)
  const [qualityRefresh, setQualityRefresh] = useState(0)
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
  // 最终结果（/api/result）：与节点链分开取，登录态才有；旧记录/看不到为 null。
  const [finalResult, setFinalResult] = useState<{ key: string; data: TraceResult | null } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    fetchAuditStats()
      .then(s => { if (!alive) return; setStats(s) })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    let alive = true
    fetchLiveQuality(1).then(q => {
      if (!alive) return
      setQuality(q)
      setQualityError('')
      setQualityUpdatedAt(new Date())
    }).catch(e => {
      if (!alive) return
      // 失败不能沿用旧的 0（或旧的非零值），否则看起来仍是实时数据。
      setQuality(null)
      setQualityError(String(e.message || e))
    })
    return () => { alive = false }
  }, [qualityRefresh])

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === 'visible') setQualityRefresh(value => value + 1)
    }
    const timer = window.setInterval(refresh, 30_000)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [])

  /* 输入即请求会把每个字母都打成一次查询。300ms 防抖后再落到 query 上，
     筛选条件一变回到第一页 —— 停在第 5 页而结果只剩 3 条会看到一片空白。

     **回第一页跟着 setter 一起做，不靠一个 useEffect 去追。** 追的写法会先按
     旧页码请求一次、再按第 1 页请求一次：这一页是滚动追加，第一次请求的结果
     还会被拼进列表里，于是换条件之后列表顶上先闪一段旧结果。 */
  const [query, setQuery] = useState(focusTrace ?? '')
  useEffect(() => {
    const timer = setTimeout(() => { setQuery(keyword.trim()); setPage(1) }, 300)
    return () => clearTimeout(timer)
  }, [keyword])
  const refilter = <T,>(set: (value: T) => void) => (value: T) => {
    set(value)
    setPage(1)
  }
  const pickStatus = refilter(setStatus)
  const pickSource = refilter(setSource)

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
    fetchResult(selected).then(data => { if (alive) setFinalResult({ key: selected, data }) })
    return () => { alive = false }
  }, [selected])

  // key 不匹配 = 还在读这一条。四种状态必须一直分开传到底：
  // 「读取中」「看不到」「读取失败」「拿到了但没有步骤」在页面上长得一样时，
  // 一次接口层的静默失效就会被读成"这条调用本来就没有链路"（2026-09-07 撞过）。
  const currentResult: TraceChainResult | { status: 'loading' } =
    chain && chain.key === selected ? chain.result : { status: 'loading' }
  const currentChain = currentResult.status === 'ok' ? currentResult.data : null
  const currentItem = items?.find(i => i.trace_id === selected) ?? null

  /** 跳到另一条 trace（目前只有"命中缓存 → 首跑链路"这一处用）。
   *  与 focusTrace 同一个做法：把 id 落进左栏搜索词，列表收到那一条、右栏选中它，
   *  搜索框里明摆着 id，清掉就回到完整流水。直接 setSelected 不够 ——
   *  首跑那条未必在已加载的这几页里，右栏会因为找不到 item 而退回空态。 */
  const focusOn = useCallback((traceId: string) => {
    setKeyword(traceId)
    setSelected(traceId)
  }, [])

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

      <div className="trace-metric-meta" role="status" aria-live="polite">
        <span>
          {qualityError ? `当前环境指标读取失败（${qualityError}）` : quality
            ? `当前环境审计流水 · ${quality.service?.config || '当前配置'} · ${qualityUpdatedAt?.toLocaleTimeString('zh-CN') || '刚刚'} 更新${quality.runs === 0 ? ' · 近 24 小时没有已收尾调用' : ''}`
            : '正在读取当前环境审计流水…'}
        </span>
        <button type="button" onClick={() => setQualityRefresh(value => value + 1)}>
          刷新指标
        </button>
      </div>
      <StatTiles quality={quality} />

      <div className={`trace-layout${prototypePreview ? ' prototype-preview-layout' : ''}`}>
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
              <select value={status} aria-label="按状态筛选" onChange={event => pickStatus(event.target.value)}>
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
                onChange={event => pickSource(event.target.value === '__all__' ? undefined : event.target.value)}
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
                    {rolesLabel(item.role) || '未记录'} · {fmtTime(item.ts)} ·{' '}
                    {!item.ok ? (item.rejected_by === 'INTERRUPTED' ? '已中断' : '已拦截')
                      : item.cached ? '命中缓存' : secs(item.elapsed_ms)}
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
          <TraceDetail item={currentItem} chain={currentChain} result={currentResult}
                       finalResult={finalResult && finalResult.key === selected ? finalResult.data : null}
                       onFocusTrace={focusOn} />
        </div>
      </div>

    </div>
  )
}

function StatTiles({ quality }: { quality: LiveQuality | null }) {
  const pct = (v: number | null | undefined) => v == null ? NA : `${(v * 100).toFixed(1)}%`
  const successDelta = quality?.success_rate != null && quality.prev?.success_rate != null
    ? (quality.success_rate - quality.prev.success_rate) * 100 : null
  const tokenTotal = quality?.tok_total
  const formatTokens = (value: number | undefined) => value == null ? NA
    : value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M`
      : value >= 10_000 ? `${(value / 1_000).toFixed(1)}K` : value.toLocaleString()

  return (
    <div className="stats">
      <div className="stat stat-total">
        <span>最近 24 小时</span><strong>{quality?.runs.toLocaleString() ?? NA} 次调用</strong>
        <small>当前环境审计流水 · 全部类型</small>
      </div>
      <div className="stat">
        <span>多智能体调用</span><strong>{quality?.multi_agent_calls?.toLocaleString() ?? NA}</strong>
        <small>占比 {pct(quality?.multi_agent_rate)}</small>
      </div>
      <div className="stat">
        <span>成功率</span><strong>{pct(quality?.success_rate)}</strong>
        <small>{successDelta == null ? '较昨日 —' : `较昨日 ${successDelta >= 0 ? '+' : ''}${successDelta.toFixed(1)}%`}</small>
      </div>
      <div className="stat">
        <span>平均耗时</span><strong>{secs(quality?.avg_elapsed_ms)}</strong>
        <small>多智能体 {secs(quality?.multi_agent_avg_elapsed_ms)}</small>
      </div>
      <div className="stat">
        <span>Token 用量</span><strong>{formatTokens(tokenTotal)}</strong>
        <small>最近 24 小时</small>
      </div>
    </div>
  )
}

function TraceDetail({ item, chain, result, finalResult, onFocusTrace }: {
  item: AuditItem | null
  chain: TraceChain | null
  /** 从"命中缓存"那一行跳到首跑链路。见 TracesPage 里的 focusOn */
  onFocusTrace?: (traceId: string) => void
  /** 节点链这一次取的结果。chain 是它的 ok 分支，两个都要传：
   *  上面六格只需要值，下面的 Span 表还要说清"为什么没有值"。 */
  result: TraceChainResult | { status: 'loading' }
  /** 最终结果（/api/result）：答案 + 已脱敏结果行。null = 未登录/看不到/被拦/旧记录。 */
  finalResult?: TraceResult | null
}) {
  const [showResult, setShowResult] = useState(false)
  if (!item) return <p className="trace-empty">左侧选一条调用查看节点明细。</p>

  const steps = chain?.steps ?? []
  const outcome = item.ok
    ? 'SUCCESS'
    : item.rejected_by === 'INTERRUPTED' ? 'INTERRUPTED' : `BLOCKED · ${item.rejected_by}`
  /* 命中缓存要写在这一行里：下面六格的总耗时 0.00s、Token —— 都是真的，
     但只有知道"这次没跑模型"才读得懂，否则看起来像一次没记全的调用。 */
  const cached = Boolean(chain?.cached ?? item.cached)
  const health = chainHealth(steps)
  /* 实际出活的模型优先于链路级那个字段：切了备选时两者不是同一个。
     链路级字段留作兜底 —— 直查与老记录没有 span 级 model。 */
  const model = health.answering || chain?.model || ''
  /* 成本走流水本身兜底：/api/trace 还没回来、或这条链路被可见性收窄挡成 404
     时，chain 是 null，而列表那条记录上的 cost_cny 是同一条审计记录里的同一
     个数 —— 没理由在右栏留个占位符。 */
  const cost = chain?.cost_cny ?? item.cost_cny

  return (
    <>
      <div className="trace-detail-head">
        <div>
          <h3>{item.question || `（${KIND_NAMES[item.kind] ?? item.kind}）`}</h3>
          <p>
            {item.trace_id} · {outcome} ·{' '}
            {cached ? 'CACHED' : item.multi_step ? 'MULTI-STEP' : 'ONE-SHOT'}
            {health.failed.length > 0 && ` · RETRY ×${health.failed.length}`}
          </p>
        </div>
        <div className="trace-badges">
          {/* 最终结果：默认收起，点击弹出。只在拿到结果（登录+可见+非拦截）时出现。 */}
          {(finalResult && ((finalResult.rows_preview?.length ?? 0) > 0 || finalResult.answer)) && (
            <button type="button" className="result-open-btn" onClick={() => setShowResult(true)}>查看结果</button>
          )}
          {/* 原型这枚角标是写死的「可信度 96」。这里判真值，且与工作台右栏那枚环
              走同一份口径（trust.ts）—— 同一次查询在两页给出两个分，看的人第一件
              要做的事就变成了复核这两个数字谁对。判不了的时候留 NA，不编数。 */}
          <TrustBadge item={item} chain={chain} />
          {/* 分数**判不到**回退与降级：trust.ts 那六项来自审计记录的字段
              （截断 / 重试次数 / 脱敏 / 盲选 / 行数），主模型切备选、向量召回
              回落只留在 span 上，一项都不占。所以这里另挂一枚，说的是一件能
              从 span 直接读出来的事实，不去动那个分 —— 把降级折成扣几分，
              等于给通过率模型塞一个没有出处的权重。 */}
          {!health.clean && (
            <span className="status bad" title={degradeTitle(health)}>链路降级</span>
          )}
        </div>
      </div>

      <ChainBanner health={health} steps={steps} />

      {/* 字段与顺序严格照原型的六格，一格不多。数据来自 /api/trace（节点链）
          与流水本身 —— 不经回放，所以未登录、回放关闭时这六格照样是满的。
          每格底下那行小字只在**真有代价**时出现：没返工就不占位。 */}
      <div className="trace-facts">
        <div className="trace-fact">
          <span>总耗时</span><strong>{secs(item.elapsed_ms)}</strong>
          {health.wastedMs > 0 && <small className="bad">其中重试废弃 {secs(health.wastedMs)}</small>}
        </div>
        <div className="trace-fact">
          <span>模型</span>
          <strong className={health.replaced ? 'swap' : ''} title={model}>{model || NA}</strong>
          {health.replaced && (
            <small className="warn" title={`配置的主模型 ${health.replaced} 调用失败，本次由 ${model} 出活`}>
              ⇄ 回退自 <s>{health.replaced}</s>
            </small>
          )}
        </div>
        <div className="trace-fact">
          <span>Token</span><strong>{tokens(chain)}</strong>
          {health.wastedTok > 0 && <small className="bad">含废弃 {health.wastedTok.toLocaleString()}</small>}
        </div>
        <div className="trace-fact">
          <span>工具调用</span><strong>{steps.length ? toolCalls(steps) : NA}</strong>
          {(health.toolFailed > 0 || health.toolDegraded > 0 || health.toolEmpty > 0) && (
            <small className={health.toolFailed > 0 ? 'bad' : 'warn'}>
              {[health.toolFailed > 0 && `${health.toolFailed} 失败`,
                health.toolDegraded > 0 && `${health.toolDegraded} 降级`,
                health.toolEmpty > 0 && `${health.toolEmpty} 空结果`].filter(Boolean).join(' · ')}
            </small>
          )}
        </div>
        <div className="trace-fact"><span>成本</span><strong title={costTitle(cost)}>{money(cost)}</strong></div>
        <div className="trace-fact"><span>数据源</span><strong title={item.source_name ?? ''}>{item.source_name || NA}</strong></div>
      </div>

      {/* key 挂 trace_id：换一条链路要连展开态和铺开行数一起归零。不挂的话
          组件被复用，上一条展开到第 4 行、铺开 60 行的状态会套在新链路上。 */}
      <TraceNodes key={item.trace_id} traceId={item.trace_id} steps={steps} result={result}
                  cachedFrom={chain?.cached_from} onFocusTrace={onFocusTrace} />

      {showResult && finalResult && (
        <ModalShell onClose={() => setShowResult(false)}>
          {/* 结构与类名跟任务中心的结果弹窗是同一套（modal-sheet + result-*）：
              两页展示的本来就是同一条 /api/result，各写一套的结果是这里的头部
              没有任何样式、正文字号也开始分叉。 */}
          <div className="modal modal-sheet trace-result-modal" role="dialog" aria-modal="true" aria-labelledby="traceResultTitle">
            <div className="modal-head">
              <div>
                <div className="eyebrow">{item.trace_id.toUpperCase()} · {outcome}</div>
                <h3 id="traceResultTitle">最终结果</h3>
              </div>
              <button className="modal-close" type="button" onClick={() => setShowResult(false)} aria-label="关闭">×</button>
            </div>
            <div className="modal-body">
              {/* 正文与任务中心的结果弹窗共用一个组件：同一条 /api/result，
                  各写一套的那一版，字号与顺序已经开始分叉。 */}
              <ResultDetail
                /* 直查没有问题文本，「提问」那一格原来只有一句「（直查模式）」——
                   拿到一张八行的结果表，却没有任何东西说明它是查什么查出来的。
                   提交的那条 SQL 就是这次的提问，直接摆在同一个位置。 */
                question={finalResult.sql_raw ? undefined : (item.question || undefined)}
                questionSql={finalResult.sql_raw || undefined}
                /* 溯源里的「原生 SQL」给**真正执行的那一版**：注入租户谓词和
                   LIMIT 之后，跑的已经不是提交的那条。 */
                sql={finalResult.sql_final || undefined}
                answer={finalResult.answer || undefined}
                columns={finalResult.columns ?? undefined}
                rows={finalResult.rows_preview ?? undefined}
                cap={[
                  typeof finalResult.rows_returned === 'number' ? `共 ${finalResult.rows_returned} 行` : '',
                  (finalResult.rows_returned ?? 0) > (finalResult.rows_preview?.length ?? 0)
                    ? `仅前 ${finalResult.rows_preview?.length} 行` : '',
                  (finalResult.masked_columns?.length ?? 0) > 0
                    ? `已脱敏 ${finalResult.masked_columns?.join('、')}` : '',
                ].filter(Boolean).join(' · ')}
                facts={[
                  `数据源 · ${item.source_name || NA}`,
                  `耗时 · ${secs(item.elapsed_ms)}`,
                  ...(model ? [`模型 · ${model}`] : []),
                ]}
                traceNote="审计只保留执行事实；未脱敏字段不会在这里补齐。"
              />
              <div className="modal-actions">
                <button className="ghost" type="button" onClick={() => setShowResult(false)}>关闭</button>
              </div>
            </div>
          </div>
        </ModalShell>
      )}
    </>
  )
}

/** 「链路降级」角标的悬停说明 —— 只报角标不说因为什么，与写死一个标签没区别。 */
function degradeTitle(health: ChainHealth): string {
  const lines: string[] = []
  if (health.failed.length) lines.push(`失败 ${health.failed.length} 次，已由重试或备用路径接住`)
  if (health.replaced) lines.push(`模型由 ${health.replaced} 回退到 ${health.answering}`)
  if (health.soft.some(s => s.status === 'degraded')) lines.push('有步骤以低于主路径的能力完成')
  if (health.soft.some(s => s.status === 'empty')) lines.push('执行成功但返回零行')
  return ['这条链路没按主路径跑完：', ...lines.map(l => `· ${l}`)].join('\n')
}

/** 非纯净链路的顶部横幅 —— **干净链路不渲染**，日常不加噪音。
 *
 *  每一条都由一条具体的 span 生成，措辞只复述 span 上记着的东西：
 *  哪一步失败了、报的什么码、之后做了什么。不做归因、不给建议。 */
function ChainBanner({ health, steps }: { health: ChainHealth; steps: ReplayStep[] }) {
  if (health.clean) return null
  const name = (s: ReplayStep) => STEP_NAMES[s.step] ?? s.step
  const lines: React.ReactNode[] = health.failed.map((s, i) => (
    <li key={`f${i}`}>
      <b>{name(s)}</b>：{s.model ? `${s.model} ` : ''}{s.note || '调用失败'}
      {s.error_code ? `（${s.error_code}）` : ''}
      {s.disposition ? `，${s.disposition}` : ''}
      {s.error_message ? `　${s.error_message}` : ''}
    </li>
  ))
  if (health.replaced) {
    lines.push(
      <li key="swap">
        <b>最终产出</b>：由备选模型 {health.answering} 完成，非配置的主模型 {health.replaced}
        {health.wastedTok > 0 ? `；计费含废弃 ${health.wastedTok.toLocaleString()} tok` : ''}
      </li>,
    )
  }
  const empty = steps.find(s => s.status === 'empty')
  if (empty) {
    lines.push(
      <li key="empty">
        <b>{name(empty)}</b>：返回 0 行
        {health.failed.length > 0 || health.soft.length > 1
          ? '；本次链路上游存在失败或降级，不能判定为"确实没有数据"'
          : ''}
      </li>,
    )
  }
  if (lines.length === 0) return null
  return (
    <div className="chain-banner">
      <strong>本次链路未按主路径完成</strong>
      <ul>{lines}</ul>
    </div>
  )
}

/** 「1,020」而不是「953+67」：原型那一格是一个数。分不清进出的时候
 *  两者都没有就留占位，不拿 0 顶上。 */
function tokens(chain: TraceChain | null): string {
  if (!chain || (chain.tok_in == null && chain.tok_out == null)) return NA
  return ((chain.tok_in ?? 0) + (chain.tok_out ?? 0)).toLocaleString()
}

/** 这一次跑完花了多少钱。四位小数与任务中心、可信度栏那两处同一口径 ——
 *  同一条 trace 在三个地方必须显示同一个数，改这里就要三处一起改。
 *
 *  真 0 与"没记到"分开：命中缓存的那次调用成本确实是 ¥0.0000，而老记录里
 *  cost_cny 缺字段 —— 后者留占位，不拿 0 顶上，否则一条没记账的链路看起来
 *  像是白跑的。 */
function money(cost: number | null | undefined): string {
  if (cost == null) return NA
  return `¥${cost.toFixed(4)}`
}

/** 四位小数会把比 ¥0.0001 还小的一次调用显示成 ¥0.0000 —— 那不是免费。
 *  title 里给未截断的原值，要对账时能拷走。 */
function costTitle(cost: number | null | undefined): string {
  if (cost == null) return ''
  return `本次调用成本 ¥${cost}`
}

/** 输出摘要里的「命中 N 张表」。
 *
 *  N 是个数字，而看的人要判断的是**哪 N 张** —— 召回偏了与召回对了在那个数字
 *  上完全一样（实测：向量召回回落关键词后，任何问题都恒定命中同样的 8 张，
 *  摘要一眼看去毫无差别）。所以把 N 本身做成开关，点开列出这次真正给模型的表。
 *
 *  正则匹配不上就不硬拆文案，退回在摘要末尾挂一颗按钮：措辞将来改了，
 *  这颗按钮不该跟着一起失灵。 */
/** 「详情」列的开关：这一步记到的输入/输出全文，点开才铺。
 *
 *  全文入口单独占一列，不挂回「输入摘要 / 输出摘要」里。那两列是**一眼扫**
 *  的：token 计量与一句话结果，宽度固定、不换行，扫八行只需要两秒。把预览
 *  胶囊塞进去之后，两列变成了两颗长度不定的按钮 —— 表格被内容撑开、最右的
 *  状态列被挤出可视区，而且同一行上出现两个互不相干的展开入口。
 *
 *  两侧都没记到东西时给占位符而不是一颗禁用按钮：点了没反应的按钮，看的人
 *  第一反应是页面坏了，不是"这一步本来就没有输入输出"。
 */
/** 「输入摘要」一列在没有 token 计量时显示什么。
 *
 *  这一列原本只认两种内容：模型步的 prompt token 数，和缓存命中那一行通向
 *  首跑的入口。直查链路一个 token 都不烧 —— 护栏、干跑、执行三行的输入摘要
 *  于是全是占位符，页面上找不到这次执行的到底是哪条 SQL，而那是直查**唯一**
 *  的输入。（全文其实一直记着，但入口只有最右那颗「详情」，而摘要列空着的时候
 *  它读起来就是"这步本来就没有输入"。）
 *
 *  折成一行再显示：护栏改写后的 SQL 是多行带缩进的，原样铺进单元格会把这张表
 *  撑高一倍。列宽由 CSS 封死、超出省略号 —— 这张表是 auto 布局，任何一列变宽
 *  都是从最右的「状态」那里借的。全文仍点「详情」看。 */
function inputPeek(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function SpanIoToggle({ step, open, onToggle }: {
  step: ReplayStep
  open: boolean
  onToggle: () => void
}) {
  const chars = (step.input?.length ?? 0) + (step.output?.length ?? 0)
  if (!chars) return <>{NA}</>
  return (
    <button type="button" className="span-io-btn" aria-expanded={open}
            title={open ? '收起输入输出详情' : `展开输入输出详情（${chars.toLocaleString()} 字符）`}
            onClick={onToggle}>
      <i aria-hidden="true">{open ? '▴' : '▾'}</i>
    </button>
  )
}

function SpanNote({ step, open, onToggle }: {
  step: ReplayStep
  open: boolean
  onToggle: () => void
}) {
  const note = step.note ?? ''
  const tables = step.tables ?? []
  const tok = step.tok_out ? ` · ${step.tok_out.toLocaleString()} tok` : ''
  if (tables.length === 0) return <>{note || NA}{tok}</>

  const caret = <i aria-hidden="true">{open ? '▴' : '▾'}</i>
  const hit = /^(命中 )(\d+)( 张表)/.exec(note)
  const btn = (
    <button type="button" className="span-count" aria-expanded={open}
            title={open ? '收起命中的表' : '展开命中的表'} onClick={onToggle}>
      {hit ? hit[2] : tables.length}{caret}
    </button>
  )
  return hit
    ? <>{hit[1]}{btn}{hit[3]}{note.slice(hit[0].length)}{tok}</>
    : <>{note || NA} {btn}{tok}</>
}

function AgentSpanGroups({ groups, selected, onSelect }: {
  groups: AgentGroup[]; selected: string; onSelect: (id: string) => void
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set(groups.map(g => g.id)))
  const [openDetails, setOpenDetails] = useState<ReadonlySet<string>>(() => new Set())
  const toggle = (key: string) => setOpenDetails(prev => {
    const next = new Set(prev)
    if (!next.delete(key)) next.add(key)
    return next
  })
  const flipGroup = (id: string) => setExpanded(prev => {
    const next = new Set(prev)
    if (!next.delete(id)) next.add(id)
    return next
  })
  return <section className="agent-groups">
    <header><strong>Agent Run Span 分组</strong><span>{groups.length} 个智能体 · 按运行实例归属</span>
      <button type="button" onClick={() => setExpanded(new Set(groups.map(g => g.id)))}>全部展开</button>
      <button type="button" onClick={() => setExpanded(new Set())}>全部收起</button></header>
    {groups.map(group => {
      const failed = group.steps.some(s => stepFailed(s.status))
      const soft = group.steps.some(s => stepSoft(s.status))
      const state = failed ? 'failed' : soft ? 'soft' : 'ok'
      return <div className={`agent-group ${expanded.has(group.id) ? '' : 'closed'} ${selected === group.id ? 'selected' : ''}`} key={group.id}>
        <button className="agent-group-head" type="button" aria-expanded={expanded.has(group.id)} onClick={() => { flipGroup(group.id); onSelect(group.id) }}>
          <i>{expanded.has(group.id) ? '⌄' : '›'}</i><b>{group.label}</b><em>AGENT RUN · {group.id}</em>
          <span>{group.steps.reduce((n, s) => n + s.ms, 0)}ms</span><span>{group.steps.reduce((n, s) => n + (s.tok_in ?? 0) + (s.tok_out ?? 0), 0).toLocaleString()} tok</span>
          <strong className={state}>{failed ? '失败' : soft ? '降级' : '成功'}</strong>
        </button>
        {expanded.has(group.id) && <div className="agent-span-list">
          <div className="agent-span-columns"><span>类型</span><span>Span 名称</span><span>输入摘要</span><span>输出摘要</span><span>耗时</span><span>状态 / 详情</span></div>
          {group.steps.map((step, index) => {
            const key = `${group.id}:${index}`
            const open = openDetails.has(key)
            return <Fragment key={key}>
              <div className="agent-span-row">
                <span className={`span-type ${spanTypeName(step).toLowerCase()}`}>{spanTypeName(step)}</span>
                <b>{STEP_NAMES[step.step] ?? step.step}{step.tool ? <small>{step.tool}</small> : null}</b>
                <span title={step.input ?? ''}>{step.tok_in ? `${STEP_TYPE[step.step] === 'MODEL' ? 'prompt' : 'embed'} ${step.tok_in.toLocaleString()} tok` : step.input ? inputPeek(step.input) : NA}</span>
                <span title={step.note ?? ''}>{step.note || NA}{step.tok_out ? ` · ${step.tok_out.toLocaleString()} tok` : ''}</span>
                <time>{step.ms}ms</time>
                <strong className={stepFailed(step.status) ? 'failed' : stepSoft(step.status) ? 'soft' : ''}>{step.status.toUpperCase()}
                  {(step.input || step.output) && <button type="button" aria-expanded={open} onClick={() => toggle(key)}>{open ? '收起' : '详情'}</button>}
                </strong>
              </div>
              {open && <div className="agent-span-detail">
                {step.input && <section><b>输入</b><pre>{step.input}</pre></section>}
                {step.output && <section><b>输出</b><pre>{step.output}</pre></section>}
                {step.error_message && <section><b>错误</b><pre>{step.error_message}</pre></section>}
              </div>}
            </Fragment>
          })}
        </div>}
      </div>
    })}
  </section>
}

function AgentTimelineView({ groups, steps, traceId }: {
  groups: AgentGroup[]; steps: ReplayStep[]; traceId: string
}) {
  const complete = steps.length > 0 && steps.every(s => s.start_ms != null)
  const ordered = complete ? [...steps].sort((a, b) => a.start_ms! - b.start_ms!) : steps
  const maxMs = Math.max(1, ...steps.map(s => (s.start_ms ?? 0) + s.ms))
  const owner = (step: ReplayStep) => groups.find(g => g.id === step.agent_run_id
    || (!step.agent_run_id && g.steps.includes(step)))
  return <section className="agent-timeline-view">
    {!complete && <p className="timeline-legacy">这条历史 Trace 未记录 Span 开始时间，按服务端记录顺序排列；不推断并行时序。</p>}
    {complete && <div className="timeline-axis"><span>0ms</span><span>{Math.round(maxMs / 2)}ms</span><span>{maxMs}ms</span></div>}
    {ordered.map((step, index) => {
      const group = owner(step)
      const start = step.start_ms ?? 0
      return <div className="timeline-span" key={`${step.agent_run_id || step.step}:${index}`}>
        <span className="timeline-owner"><b>{group?.label ?? STEP_NAMES[step.step] ?? step.step}</b><small>{group?.id ?? `${traceId}:${step.step}`}</small></span>
        <span className="timeline-rail">{complete && <i className={stepFailed(step.status) ? 'failed' : stepSoft(step.status) ? 'soft' : ''} style={{ left: `${start / maxMs * 100}%`, width: `${Math.max(1, step.ms / maxMs * 100)}%` }} title={`${STEP_NAMES[step.step] ?? step.step} · ${step.ms}ms`} />}</span>
        <span className="timeline-name"><b>{STEP_NAMES[step.step] ?? step.step}</b><small>{complete ? `+${start}ms` : `记录 #${index + 1}`} · {step.ms}ms</small></span>
        <span className="timeline-kind"><i className={`span-type ${spanTypeName(step).toLowerCase()}`}>{spanTypeName(step)}</i></span>
        <strong className={stepFailed(step.status) ? 'failed' : stepSoft(step.status) ? 'soft' : ''}>{step.status.toUpperCase()}</strong>
      </div>
    })}
  </section>
}

/** 链路条与 Span 明细。版式照原型，不另起说明段落 ——
 *  唯一的例外是空表里那一行状态，它替代的是原来那片无从解释的空白。 */
type AgentGroup = { id: string; role: string; label: string; parent: string; steps: ReplayStep[] }

function agentGroups(steps: ReplayStep[], traceId: string): AgentGroup[] {
  const groups = new Map<string, AgentGroup>()
  for (const step of steps) {
    // Explicit IDs are written by new traces. For legacy multi-agent records, recover
    // worker identity from the historic `${traceId}:worker:n` stage convention.
    const legacyWorker = step.stage?.startsWith(`${traceId}:worker:`) ? step.stage : ''
    const role = step.agent_role || (legacyWorker ? 'query_worker' :
      ({ supervisor: 'supervisor', resolve_skills: 'supervisor', semantic: 'semantic',
        verifier: 'verifier', synthesizer: 'synthesizer' } as Record<string, string>)[step.step] || '')
    const id = step.agent_run_id || legacyWorker || (role ? `${traceId}:${role}` : '')
    if (!id || !role) continue
    const group = groups.get(id) ?? {
      id, role, label: role === 'query_worker'
        ? `Query Worker #${id.match(/worker:(\d+)/)?.[1] ?? '—'}`
        : ({ supervisor: 'Supervisor', semantic: 'Semantic Agent', verifier: 'Verifier',
             synthesizer: 'Synthesizer' } as Record<string, string>)[role] ?? role,
      parent: step.parent_agent_run_id || (role !== 'supervisor' ? `${traceId}:supervisor` : ''),
      steps: [],
    }
    group.steps.push(step)
    groups.set(id, group)
  }
  return [...groups.values()].sort((a, b) => {
    const order = ['supervisor', 'semantic', 'query_worker', 'verifier', 'synthesizer']
    const rank = (role: string) => order.includes(role) ? order.indexOf(role) : order.length
    return rank(a.role) - rank(b.role) || a.id.localeCompare(b.id, undefined, { numeric: true })
  })
}

/** 编排轨迹保持多张卡片。没有 Supervisor / 多个 Worker 时，整条链路就是一个查询智能体。 */
function displayGroups(steps: ReplayStep[], traceId: string): AgentGroup[] {
  const groups = agentGroups(steps, traceId)
  const workers = groups.filter(g => g.role === 'query_worker').length
  const orchestrated = groups.some(g =>
    g.role === 'supervisor' || g.role === 'semantic' || g.role === 'verifier' || g.role === 'synthesizer')
    || workers > 1
  if (orchestrated) return groups
  if (steps.length === 0) return []
  return [{
    id: `${traceId}:query`,
    role: 'query',
    label: 'Query Agent',
    parent: '',
    steps,
  }]
}

function TraceNodes({ traceId, steps, result, cachedFrom, onFocusTrace }: {
  traceId: string
  steps: ReplayStep[]
  result: TraceChainResult | { status: 'loading' }
  /** 命中缓存时，答案出自哪一次真跑。空/缺省表示不是缓存命中，或旧格式缓存没记 */
  cachedFrom?: string | null
  onFocusTrace?: (traceId: string) => void
}) {
  const groups = displayGroups(steps, traceId)
  const multiAgent = groups.some(g => g.role === 'supervisor' || g.role === 'query_worker')
  const [mode, setMode] = useState<'agents' | 'timeline' | 'spans'>('agents')
  const [selectedAgent, setSelectedAgent] = useState('')
  const selectedGroup = groups.find(g => g.id === selectedAgent) ?? groups[0]
  const shownSteps = multiAgent && mode === 'agents' && selectedGroup
    ? steps.filter(s => s.agent_run_id === selectedGroup.id
      || (!s.agent_run_id && (selectedGroup.role === 'query_worker'
        ? s.stage === selectedGroup.id
        : selectedGroup.role === 'supervisor'
          ? ['supervisor', 'resolve_skills'].includes(s.step)
          : s.step === selectedGroup.role)))
    : steps
  /* 展开的是哪几步。用 Set 而不是单个下标：多步问答里 schema_recall 会出现
     多次，展开第二次不该把第一次收起来。 */
  const [openRows, setOpenRows] = useState<ReadonlySet<number>>(() => new Set())
  const toggleRow = (i: number) => setOpenRows(prev => {
    const next = new Set(prev)
    if (!next.delete(i)) next.add(i)
    return next
  })
  /* 输入/输出全文的展开态**与命中表那个分开记**：同一行上两个互不相干的
     折叠，共用一个 Set 的话，点开表名会把提示词也拽出来。 */
  const [openIo, setOpenIo] = useState<ReadonlySet<number>>(() => new Set())
  const toggleIo = (i: number) => setOpenIo(prev => {
    const next = new Set(prev)
    if (!next.delete(i)) next.add(i)
    return next
  })

  /* 铺到第几行。滚到表底自动续下一批 —— 不是点一次加载一次：这张表是**顺着
     读**的，每 20 行截断一次会把"这一步之后发生了什么"切成人为的段落。
     哨兵同时是一颗可点的按钮：IntersectionObserver 在 root 上不生效时
     （容器还没量出高度、或浏览器不支持），它至少还点得动，不会卡成死表。 */
  const [shown, setShown] = useState(SPAN_PAGE)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sentinelRef = useRef<HTMLTableRowElement | null>(null)
  const rest = shownSteps.length - shown
  const showMore = useCallback(
    () => setShown(n => Math.min(n + SPAN_PAGE, shownSteps.length)), [shownSteps.length])
  useEffect(() => {
    if (rest <= 0) return
    const el = sentinelRef.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    /* root 给滚动容器本身：这张表滚的是自己那 520px，不是整页视口 ——
       用默认 root 的话，哨兵在容器里露出来的时候页面视口根本没动过。 */
    const io = new IntersectionObserver(
      es => { if (es.some(e => e.isIntersecting)) showMore() },
      { root: scrollRef.current, rootMargin: '80px' })
    io.observe(el)
    return () => io.disconnect()
  }, [rest, showMore])

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
  if (groups.length > 0) return <MultiAgentTrace traceId={traceId} groups={groups} steps={steps} />
  return (
    <>
      {!multiAgent && steps.length > 0 && (
        <div className="trace-flow">
          {steps.map((step, i) => (
            <Fragment key={`${step.step}-${i}`}>
              <div className={[
                'trace-node',
                (STEP_TYPE[step.step] ?? '').toLowerCase(),
                stepFailed(step.status) ? 'warn' : '',
                stepSoft(step.status) ? 'soft' : '',
              ].filter(Boolean).join(' ')}>
                {/* 尝试了几次就标几次。这颗角标是"这一步返过工"在链路条上
                    唯一的痕迹 —— 不标的话，一条被救回来的链路整条都是绿的。 */}
                {(step.attempts_total ?? 0) > 1 && (
                  <i className="retry-badge" title={`这一步共尝试 ${step.attempts_total} 次`}>
                    ×{step.attempts_total}
                  </i>
                )}
                <strong>{STEP_NAMES[step.step] ?? step.step}</strong>
                <small>{step.ms}ms</small>
              </div>
              {i < steps.length - 1 && <i className="trace-arrow">→</i>}
            </Fragment>
          ))}
        </div>
      )}

      <div className="span-table agent-span-views">
        <div className="span-table-title">
          <strong>{multiAgent && mode === 'agents' && selectedGroup
            ? 'Agent Run Span 明细' : mode === 'timeline' ? 'Span 时间线' : 'Span 明细'}</strong>
          {multiAgent && <div className="trace-view-tabs" role="tablist" aria-label="Span 展示方式">
            {([['agents', '按智能体'], ['timeline', '时间线'], ['spans', '全部 Span']] as const).map(([key, label]) =>
              <button key={key} type="button" role="tab" aria-selected={mode === key}
                className={mode === key ? 'active' : ''} onClick={() => setMode(key)}>{label}</button>)}
          </div>}
          <span>
            {multiAgent && mode === 'agents' ? `${shownSteps.length} 个 Span · ` : ''}
            {mode === 'agents' ? '按智能体归属' : mode === 'timeline' ? '按开始时间排序' : '按记录顺序'} · 失败与回退默认展开
            {/* 分页只在真分了页时说一句。没过一页还写「12/12」是噪声。 */}
            {shownSteps.length > SPAN_PAGE && ` · 已铺 ${Math.min(shown, shownSteps.length)}/${shownSteps.length}`}
          </span>
        </div>
        {multiAgent && mode === 'agents' && <AgentSpanGroups groups={groups}
          selected={selectedGroup?.id ?? ''} onSelect={setSelectedAgent} />}
        {multiAgent && mode === 'timeline' && <AgentTimelineView groups={groups} steps={steps} traceId={traceId} />}
        {(!multiAgent || mode === 'spans') && <div className="table-scroll span-scroll" ref={scrollRef}>
          <table>
            <thead>
              <tr><th>类型</th><th>Span</th>{multiAgent && <th>Agent Run</th>}<th>输入摘要</th><th>输出摘要</th>
                  <th className="span-io-col">详情</th><th>耗时</th><th>状态</th></tr>
            </thead>
            <tbody>
              {(shownSteps.length === 0 || steps.length === 0) && (
                <tr className="span-empty"><td colSpan={multiAgent ? 8 : 7}>{empty}</td></tr>
              )}
              {/* 下标照原数组算（slice 从 0 起，i 不偏）—— 展开态记的是下标，
                  切片一旦不是从头切，点开的就会是另一行。 */}
              {shownSteps.slice(0, shown).map((step, i) => (
                <Fragment key={`${step.step}-${i}`}>
                  <tr>
                    <td><span className={`span-type ${(STEP_TYPE[step.step] ?? 'sys').toLowerCase()}`}>{spanTypeName(step)}</span></td>
                    <td>
                      {STEP_NAMES[step.step] ?? step.step}
                      {/* decide 连着出现五六次，不分档就看不出哪次在挑工具、哪次
                          是看完结果再决定、哪次在返工。判定在后端（stage 字段）。 */}
                      {step.stage && STAGE_NAMES[step.stage] && (
                        <em className="span-stage">{STAGE_NAMES[step.stage]}</em>
                      )}
                      {/* 工具调用这一步调的具体工具名，换行成子行显示，样式与下面
                          「尝试/模型」子行一致，不另起颜色。 */}
                      {step.tool && <em className="span-attempt">{step.tool}</em>}
                      {/* 第几次尝试、谁出的活 —— 同一个节点名会连着出现两三行，
                          不写清楚就分不出哪行是失败的那次。 */}
                      {((step.attempts_total ?? 0) > 1 || step.model) && (
                        <em className="span-attempt">
                          {(step.attempts_total ?? 0) > 1 && `尝试 ${step.attempt}/${step.attempts_total}`}
                          {(step.attempts_total ?? 0) > 1 && step.model && ' · '}
                          {step.model}
                        </em>
                      )}
                    </td>
                    {multiAgent && <td className="span-agent-run">{groups.find(g => g.id === step.agent_run_id || (!step.agent_run_id && g.steps.includes(step)))?.label ?? NA}</td>}
                    {/* 原型这两列是「输入/输出摘要」。askdb 只记一条 note（该步的结果说明），
                        放在输出侧；输入侧只有 prompt token 数是真的，没有就留占位。 */}
                    {/* 缓存命中这一行的"输入"就是首跑那条记录 —— 整条链路只有这一个
                        节点，模型、工具、数据库一个都没跑，看的人要能一键走到真跑的那条。
                        cached_from 为空（旧格式缓存没记 trace_id）时退回占位符，不给死链。 */}
                    <td>
                      {step.step === 'cache' && cachedFrom
                        ? <button
                            className="span-origin"
                            title={`答案出自 ${cachedFrom} 那次执行，点击查看它的完整链路`}
                            onClick={() => onFocusTrace?.(cachedFrom)}
                          >首跑 {cachedFrom.slice(0, 6)} ↗</button>
                        /* Schema 召回那一步的 tok_in 是**嵌入的输入**，不是提示词
                           —— 自 2026-09-10 起它有真实用量了。照旧写成 "prompt"
                           会让人以为召回也在发提示词。 */
                        : step.tok_in
                          ? `${STEP_TYPE[step.step] === 'MODEL' ? 'prompt' : 'embed'} `
                            + `${step.tok_in.toLocaleString()} tok`
                            /* 命中前缀缓存的那部分单独标出来：它按标准输入价的
                               10% 结算，是"这一步到底花了多少钱"的关键一项，
                               混在 tok_in 里看不出来。没命中就不占位。 */
                            + (step.cached_in
                                ? `（缓存 ${step.cached_in.toLocaleString()}）` : '')
                        /* 两样都没有时退到输入全文的第一眼：直查那三行就靠它
                           才看得见执行的是哪条 SQL。 */
                        : step.input
                          ? <span className="span-in" title={step.input}>
                              {inputPeek(step.input)}
                            </span>
                          : NA}
                    </td>
                    <td className="span-note" title={step.note ?? ''}>
                      <SpanNote step={step} open={openRows.has(i)} onToggle={() => toggleRow(i)} />
                    </td>
                    <td className="span-io-col">
                      <SpanIoToggle step={step} open={openIo.has(i)} onToggle={() => toggleIo(i)} />
                    </td>
                    <td>{step.ms}ms</td>
                    <td className={stepFailed(step.status) ? 'bad' : stepSoft(step.status) ? 'warn' : 'good'}
                        title={STATUS_HINT[step.status] ?? ''}>
                      {step.status.toUpperCase()}
                    </td>
                  </tr>
                  {/* 失败的那次要能就地说清楚：报了什么码、原始消息是什么、
                      之后做了什么。默认展开 —— 这是这一行存在的全部理由，
                      再折一层就等于没记。 */}
                  {stepFailed(step.status) && (step.error_code || step.disposition) && (
                    <tr className="span-detail span-error">
                      <td colSpan={multiAgent ? 8 : 7}>
                        <dl>
                          {step.error_code && (
                            <div><dt>错误码</dt><dd><code>{step.error_code}</code></dd></div>
                          )}
                          {/* 原文只在回放那条路上有 —— /api/trace 不给（见 api.ts
                              ReplayStep.error_message）。没有就不留空行，
                              也不拿 note 顶上：note 是我们自己写的一句话，
                              把它标成"原始消息"是在骗人。 */}
                          {step.error_message && (
                            <div><dt>原始消息</dt><dd>{step.error_message}</dd></div>
                          )}
                          {step.disposition && <div><dt>处置</dt><dd>{step.disposition}</dd></div>}
                        </dl>
                      </td>
                    </tr>
                  )}
                  {openRows.has(i) && (step.tables ?? []).length > 0 && (
                    <tr className="span-detail">
                      <td colSpan={multiAgent ? 8 : 7}>
                        <ol className="span-tables">
                          {(step.tables ?? []).map(t => <li key={t}><code>{t}</code></li>)}
                        </ol>
                      </td>
                    </tr>
                  )}
                  {/* 输入/输出全文。摊成整行而不是塞回那两列 —— 提示词动辄
                      三四千字，挤在一列里既读不了，也会把表格撑到横向滚动。 */}
                  {openIo.has(i) && (step.input || step.output) && (
                    <tr className="span-detail span-io">
                      <td colSpan={multiAgent ? 8 : 7}>
                        <dl>
                          {step.input && (
                            <div>
                              <dt>输入</dt>
                              <dd><pre>{step.input}</pre></dd>
                            </div>
                          )}
                          {step.output && (
                            <div>
                              <dt>输出</dt>
                              <dd><pre>{step.output}</pre></dd>
                            </div>
                          )}
                        </dl>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {rest > 0 && (
                <tr className="span-more" ref={sentinelRef}>
                  <td colSpan={multiAgent ? 8 : 7}>
                    <button type="button" onClick={showMore}>
                      继续铺开（还有 {rest} 行）
                    </button>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>}
      </div>
    </>
  )
}

/** 执行追踪页那枚可信度角标。
 *
 *  三种判不了的情形一律留 NA 并在 title 里说清为什么 —— 编一个数比空着糟：
 *    · 这次被拦下（BLOCKED / INTERRUPTED）：根本没产出结果，无从谈可信
 *    · 命中缓存：答案是首跑那次的，分要看首跑那条
 *    · 老记录：痕迹字段是 2026-09-10 才落库的，之前的记录判据缺失，
 *      按缺省值判会一律满分 —— 那是把"没记"读成"没发生"
 */
function TrustBadge({ item, chain }: { item: AuditItem; chain: TraceChain | null }) {
  // 不打分时，**原因写在角标上**而不是只留一个 —。一个没有分数又不说
  // 为什么的角标读起来就是"这块坏了"，看的人会去翻链路找一个根本不存在的分。
  const na = (label: string, why: string) => (
    <span className="status wait" title={why}>{label}</span>
  )
  if (!item.ok) {
    return item.rejected_by === 'INTERRUPTED'
      ? na('未出结果 · 已中断', '这次执行中断，没有产出结果，无从判可信度')
      : na(`未出结果 · ${item.rejected_by ?? '被拦下'}`,
           `这次被 ${item.rejected_by ?? '护栏'} 拦下，SQL 没有在数据库上执行，`
           + '没有产出结果，无从判可信度')
  }
  if (!chain) return na(`可信度 ${NA}`, '节点链还没取到')
  // 命中缓存照样打分：痕迹沿用首跑（见 server._serve_cached_ask），而工作台
  // 拿着同一份结果也是这么打的。只有旧格式那些没沿用痕迹的缓存记录判不了。
  const cached = Boolean(item.cached ?? chain.cached)
  const traced = cached
    ? chain.recall_blind != null && chain.truncated != null
    : [chain.truncated, chain.scope_narrowed,
       chain.mask_degraded, chain.recall_blind].some(v => v != null)
  if (!traced) {
    return cached
      ? na('可信度 · 见首跑',
           '这条缓存记录没有沿用首跑的可信度痕迹（旧格式），分看首跑那条链路')
      : na('可信度 · 判据不全', '这条记录早于可信度痕迹落库，不给分')
  }

  const mode = item.kind === 'sql' ? 'sql' : 'ask'
  const checks = resultChecks({
    mode, rowCount: item.rows_returned ?? 0,
    truncated: chain.truncated, attempts: chain.attempts ?? item.attempts,
    maskDegraded: chain.mask_degraded, recallBlind: chain.recall_blind,
    scopeNarrowed: chain.scope_narrowed,
    hedgeTerms: chain.hedge_terms, derivedColumns: chain.derived_columns,
    anaphoric: chain.anaphoric,
  })
  const score = scoreOf(checks)
  const head = mode === 'sql' ? '本次执行可信度' : '本次结果可信度'
  return (
    <span className={`status ${score === 100 ? '' : 'wait'}`}
          title={scoreTitle(head, checks, mode)
                 + (cached ? '\n（答案来自应答缓存，痕迹沿用首跑那一次）' : '')}>
      可信度 {score ?? NA}
    </span>
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
      // OTLP 的 2 是 ERROR。hit 不是错，导出成 ERROR 会让观测后端把
      // 每一次缓存命中都算进错误率里
      status: { code: stepFailed(step.status) ? 2 : 1 },
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
