import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { askQuestion, fetchSchema, runSql, type AskResult, type Me, type Schema } from '../api'
import { writeGuard, type WriteGuard } from '../writeGuard'
import type { ResultTab, View } from '../types'
import type { HealthState } from '../useHealth'
import type { SourcesState } from '../useSources'
import { ResultTabs } from './ResultTabs'
import { TrustSidebar } from './TrustSidebar'

/** askdb 真实存在的两种模式。
 *
 *  原型写的是「快捷查询 / 仅生成 SQL」，但后端没有「生成但不执行」这条路：
 *  /api/ask 走模型并执行，/api/sql 是用户自己给 SQL、跳过模型只跑
 *  护栏 → 干跑 → 执行。照抄原型的措辞会让人以为有个不会碰数据库的预览模式。
 */
type Mode = 'ask' | 'sql'

export function QueryWorkspace({ health, sources, onNavigate, notify, me }: {
  health: HealthState
  /** 数据源选择与顶栏共用同一份状态 —— 各存一份必然漂移，
   *  而漂移的表现是"正在查 A 库、顶栏说你在 B 库" */
  sources: SourcesState
  onNavigate: (view: View) => void
  /** 全局 toast —— 原型在切源、回填历史、删历史时都会提示一句 */
  notify?: (message: string) => void
  /** 右栏「身份」一格用 */
  me?: Me | null
}) {
  // 用户没显式选过时按能力推导：模型没接就落到直查 ——
  // 让人对着一个永远点不动的按钮发呆没有意义。
  const [modeChoice, setModeChoice] = useState<Mode | null>(null)
  const [question, setQuestion] = useState('')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<AskResult | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<ResultTab>('result')
  const [schema, setSchema] = useState<Schema | null>(null)
  const { items: sourceCards, sourceId, setSourceId } = sources
  const guard = writeGuard(me ?? null, '清空历史记录')
  const [menuOpen, setMenuOpen] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [menuOpen])

  const ready = health.status === 'ready' ? health.health : null
  const canAsk = !!ready?.datasource.ok && !!ready?.llm.ok
  const canSql = !!ready?.datasource.ok
  // health 还没回来时**不要替用户改模式**。原来这里 canAsk 为 false 就落到
  // 'sql'，而 health 未就绪时 canAsk 恰好也是 false —— 于是页面刚打开的那一两秒
  // 处于"看着是自然语言提问、实际是直查模式"的状态，此时按 Enter 什么都不会发生
  // （Enter 只在 ask 模式下提交），也不给任何反馈。实测复现过一次：输入框被清空、
  // 一条请求都没发出。ready 为 null 时按主用途留在 'ask'，等 health 回来再定。
  const mode: Mode = modeChoice ?? (!ready ? 'ask' : canAsk ? 'ask' : 'sql')

  // 内置源的名字取 health 里的真实库名，而不是配置文件路径 ——
  // 工作台上要回答的是"我在查哪个库"。
  //
  // **没配默认源时这一项整个不出现**：configured=false 时 health 的 detail 是
  // 「未配置默认数据源」，照旧渲染就成了一张以那句话为名的假数据源卡，还能选中，
  // 选中后每次查询都会撞后端的「本实例未配置默认数据源」。不存在的东西不该在
  // 选择器里占一行。ready 为 null（还在读 health）时先留着占位，不闪。
  const hasBuiltin = !ready || ready.datasource.configured
  const options: SourceOption[] = [
    ...(hasBuiltin ? [{
      id: '',
      code: ready ? MARK[ready.datasource.type] ?? 'DB' : '··',
      name: ready?.datasource.detail ?? '读取中…',
      meta: ready
        ? `${ready.datasource.type} · 只读`
          + (ready.tenant.enabled ? ` · ${ready.tenant.column}=${ready.tenant.org_id}` : '')
          + (schema ? ` · 开放 ${schema.tables.length} 张表` : '')
        : '',
      tables: schema?.tables.length ?? 1,
      dialect: DIALECT[ready?.datasource.type ?? ''] ?? ready?.datasource.type ?? '',
    }] : []),
    ...sourceCards.filter(c => !c.builtin).map(c => ({
      id: c.id,
      code: MARK[c.type] ?? c.type.slice(0, 2).toUpperCase(),
      name: c.name,
      meta: `${c.type} · ${c.host || '—'} · 开放 ${c.table_count} 张表`,
      tables: c.table_count,
      dialect: DIALECT[c.type] ?? c.type,
      // 右栏「数据库角色」按它报 PROD-RO / TEST-RO。此前没带过来，
      // 于是不管连的是生产只读还是测试库，那一格都写着通用的 READ-ONLY
      env: c.env,
    })),
  ]
  // 既没有内置源、运行时也一个都没加时，options 是空的 —— 兜住，别让 options[0]
  // 是 undefined 一路 undefined.tables 崩掉整页
  const current = options.find(o => o.id === sourceId) ?? options[0] ?? EMPTY_SOURCE
  const usable = (mode === 'ask' ? canAsk : canSql) && current.tables > 0

  // schema 跟着数据源走：推荐问题、示例 SQL 全从这份 schema 生成，
  // 切了源却不重取，页面就会把另一个库的表名推给用户（点了必然拒答）。
  // 依赖 current.id 而不是 sourceId，理由同 run() 里那段：选中项被移除时
  // 二者会不一致，而**发查询用的是 current.id** —— 示例必须与它同源。
  // 切换过程中先清空，宁可空一瞬，也不要显示上一个源的表名。
  useEffect(() => {
    let alive = true
    setSchema(null)
    fetchSchema(current.id).then(s => { if (alive) setSchema(s) }).catch(() => {})
    return () => { alive = false }
  }, [current.id])

  // 最近查询按数据源分桶。空串（内置源）不能直接当键 —— 落盘后与
  // "没有数据源"分不开，统一映射成 builtin。
  const sourceKey = current.id || 'builtin'
  const recent = useRecentQueries()
  const visibleRecent = useMemo(
    () => recent.items.filter(item => item.sourceKey === sourceKey),
    [recent.items, sourceKey],
  )

  const fill = (text: string) => {
    setQuestion(text)
    inputRef.current?.focus()
  }

  const pickSource = (id: string) => {
    if (id === sourceId) { setMenuOpen(false); return }
    setSourceId(id)
    setMenuOpen(false)
    // 旧结果出自另一个库，留着就是张冠李戴
    setResult(null)
    setError('')
    // 原型切源会提示一句 —— 结果被清掉了，不出声用户会以为没生效
    notify?.(`已切换到 ${sourceCards.find(s => s.id === id)?.name ?? '所选数据源'}，上一次结果已清空`)
  }

  const run = async () => {
    const text = question.trim()
    if (!text) { inputRef.current?.focus(); return }
    // Enter 与「发送」按钮必须同一套判定。按钮上挂着 disabled，Enter 却直接
    // 进链路 —— 两条入口对同一个状态给出不同结果，用户看到的就是"有时能发、
    // 有时不能，还不说为什么"。
    if (running) return
    if (!usable) {
      setError(ready
        ? '当前数据源不可执行查询：先到「数据源」确认连接与开放表。'
        : '正在确认数据源与模型状态，稍等一下再发起。')
      return
    }
    const bucket = { key: sourceKey, name: current.name }
    setRunning(true); setError('')
    recent.upsert(text, 'running', bucket)
    try {
      // 用 current.id 而不是 sourceId：选中项被移除（内置源撤掉、运行时源删掉）时
      // current 会回落到第一项，此时 sourceId 还是旧值 —— 照它发就是界面显示 A、实际查 B
      const value = mode === 'ask' ? await askQuestion(text, current.id) : await runSql(text, current.id)
      setResult(value)
      // 被拦下时先看拦截原因，而不是一张空结果表
      setTab(value.ok ? 'result' : value.rejected_by === 'INTERRUPTED' ? 'checkpoint' : 'sql')
      recent.upsert(
        text,
        value.ok ? 'completed' : value.rejected_by === 'INTERRUPTED' ? 'needs-input' : 'interrupted',
        bucket,
      )
    } catch (e) {
      setError(String((e as Error).message || e))
      recent.upsert(text, 'interrupted', bucket)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="workspace-grid">
      <div className="query-stage">
        <div className="stage-head">
          <SourceSelector
            open={menuOpen}
            onToggle={() => setMenuOpen(v => !v)}
            onPick={pickSource}
            current={current}
            options={options}
          />
          <div className="mode-tabs">
            <button className={`mode-tab ${mode === 'ask' ? 'active' : ''}`} disabled={!canAsk}
                    title={canAsk ? undefined : '未配置模型密钥，自然语言提问不可用'}
                    onClick={() => setModeChoice('ask')}>自然语言提问</button>
            <button className={`mode-tab ${mode === 'sql' ? 'active' : ''}`} disabled={!canSql}
                    onClick={() => setModeChoice('sql')}>直查 SQL</button>
          </div>
        </div>

        <div className="composer">
          <div className="composer-box">
            <textarea
              ref={inputRef}
              className={mode === 'sql' ? 'mono' : ''}
              value={question}
              onChange={e => setQuestion(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey && mode === 'ask') { e.preventDefault(); run() }
              }}
              placeholder={mode === 'ask' ? '例如：各知识库分别有多少文档'
                : 'SELECT ... —— 直查不经模型，只跑护栏、干跑与只读执行'}
            />
            <button className="send" onClick={run} disabled={running || !usable || !question.trim()}>
              {running ? '…' : '↗'}
            </button>
          </div>
          <div className="composer-foot">
            <span>{mode === 'ask' ? 'Enter 查询 · Shift + Enter 换行' : '直查不消耗 token，配额用尽后仍可用'}</span>
            <span>✓ 历史查询不会自动进入本次上下文</span>
          </div>
        </div>

        {error && <div className="audit-error stage-error">{error}</div>}

        {result
          ? <ResultTabs result={result} active={tab} dialect={current.dialect}
                        onChange={setTab} onResumed={setResult}
                        onOpenTrace={() => onNavigate('traces')} />
          : <Welcome
              mode={mode}
              schema={schema}
              usable={usable}
              sourceName={current.name}
              recent={visibleRecent}
              guard={guard}
              onFill={fill}
              onDelete={recent.remove}
              onClear={() => recent.clearSource(sourceKey)}
            />}
      </div>

      <TrustSidebar health={health} source={current} result={result} me={me} onResultTab={setTab} onNavigate={onNavigate} />
    </div>
  )
}

const MARK: Record<string, string> = { duckdb: 'DK', postgresql: 'PG' }
const DIALECT: Record<string, string> = { duckdb: 'DuckDB', postgresql: 'PostgreSQL' }

/* ---------------- 最近查询（照原型：localStorage，按数据源分桶，上限 10） ---------------- */

type RecentStatus = 'running' | 'completed' | 'needs-input' | 'interrupted'

interface RecentQuery {
  id: string
  question: string
  /** 数据源标识。内置源固定为 builtin —— 空串落盘后与"没有数据源"分不开 */
  sourceKey: string
  sourceName: string
  timestamp: number
  status: RecentStatus
}

const RECENT_QUERY_STORAGE_KEY = 'askdb.recentQueries.v1'
const RECENT_QUERY_LIMIT = 10
const RECENT_QUERY_STATUSES = new Set<RecentStatus>(['running', 'completed', 'needs-input', 'interrupted'])
const RECENT_QUERY_STATUS_LABELS: Record<RecentStatus, string> = {
  running: '执行中',
  completed: '已完成',
  'needs-input': '待补充',
  interrupted: '已中断',
}

function createRecentQueryId(): string {
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 9)
}

/** 落盘内容一律当成不可信输入重新校验。
 *  上一次会话里停在 running 的记录不可能还在跑（页面已经关了），
 *  读回来时降级成 interrupted，否则列表上会永远挂着一条假的"执行中"。 */
function loadRecentQueries(): RecentQuery[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENT_QUERY_STORAGE_KEY) || '[]')
    const rawItems: unknown[] = Array.isArray(parsed)
      ? parsed
      : (parsed && typeof parsed === 'object' && Array.isArray((parsed as { items?: unknown[] }).items)
        ? (parsed as { items: unknown[] }).items
        : [])

    const cleaned = rawItems
      .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
      .map(item => {
        const question = typeof item.question === 'string' ? item.question.trim().slice(0, 1000) : ''
        const sourceKey = typeof item.sourceKey === 'string' ? item.sourceKey.trim().slice(0, 64) : ''
        const sourceName = typeof item.sourceName === 'string' ? item.sourceName.trim().slice(0, 120) : ''
        const timestamp = Number(item.timestamp)
        const stored = RECENT_QUERY_STATUSES.has(item.status as RecentStatus)
          ? item.status as RecentStatus : 'completed'
        return {
          id: typeof item.id === 'string' && item.id ? item.id.slice(0, 80) : createRecentQueryId(),
          question,
          sourceKey,
          sourceName: sourceName || sourceKey,
          timestamp: Number.isFinite(timestamp) && timestamp > 0 ? timestamp : Date.now(),
          status: stored === 'running' ? 'interrupted' as RecentStatus : stored,
        }
      })
      .filter(item => item.question && item.sourceKey)
      .sort((a, b) => b.timestamp - a.timestamp)

    const seen = new Set<string>()
    return cleaned.filter(item => {
      const key = item.sourceKey + '\u0000' + item.question
      if (seen.has(key)) return false
      seen.add(key)
      return true
    }).slice(0, RECENT_QUERY_LIMIT)
  } catch {
    return []
  }
}

function formatRecentQueryTime(timestamp: number): string {
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date(timestamp))
  } catch {
    return '刚刚'
  }
}

function useRecentQueries() {
  const [items, setItems] = useState<RecentQuery[]>(() => loadRecentQueries())

  // 落盘统一收在这里：写 localStorage 是副作用，塞进 setState 的
  // updater 里会在 StrictMode 下被重放执行两次。
  useEffect(() => {
    try {
      localStorage.setItem(RECENT_QUERY_STORAGE_KEY, JSON.stringify({ version: 1, items }))
    } catch { /* 浏览器禁写（无痕 / 配额满）时只丢持久化，不丢本页列表 */ }
  }, [items])

  const upsert = useCallback((question: string, status: RecentStatus, source: { key: string; name: string }) => {
    const normalized = question.trim()
    if (!normalized) return
    setItems(prev => {
      const next = [
        {
          id: createRecentQueryId(),
          question: normalized.slice(0, 1000),
          sourceKey: source.key,
          sourceName: source.name,
          timestamp: Date.now(),
          status,
        },
        ...prev.filter(item => !(item.sourceKey === source.key && item.question === normalized)),
      ].slice(0, RECENT_QUERY_LIMIT)
      return next
    })
  }, [])

  const remove = useCallback((id: string) => {
    setItems(prev => {
      return prev.filter(item => item.id !== id)
    })
  }, [])

  const clearSource = useCallback((sourceKey: string) => {
    setItems(prev => {
      return prev.filter(item => item.sourceKey !== sourceKey)
    })
  }, [])

  return { items, upsert, remove, clearSource }
}

/** 示例问题按**当前库的白名单和口径**生成，不写死。
 *  写死的示例换个数据源就全是查不出结果的废话，还会让人以为库里有这些表。 */
function Welcome({ mode, schema, usable, sourceName, recent, guard, onFill, onDelete, onClear }: {
  mode: Mode
  schema: Schema | null
  usable: boolean
  sourceName: string
  recent: RecentQuery[]
  guard: WriteGuard
  onFill: (text: string) => void
  onDelete: (id: string) => void
  onClear: () => void
}) {
  const samples = useMemo(() => {
    if (!schema) return []
    if (mode === 'sql') {
      return schema.tables.slice(0, 4).map(t => ({
        title: t.name,
        text: `SELECT ${t.columns.slice(0, 3).map(c => c.name).join(', ')} FROM ${t.name}`,
        desc: t.desc || '直查这张表的前几列',
      }))
    }
    // 只拿当前角色**问得出数**的口径当示例 —— 口径列表本身不再按角色收窄
    // （业务口径中心要能整本读），但示例点了就得能跑
    const out = schema.metrics.filter(m => m.queryable).slice(0, 2).map(m => ({
      title: m.name,
      text: `${m.name}是多少`,
      desc: m.scope.length ? `已确认口径 · 涉及 ${m.scope.join('、')}` : '使用已确认的业务口径',
    }))
    for (const t of schema.tables) {
      if (out.length >= 4) break
      out.push({ title: t.desc || t.name, text: `${t.aliases[0] || t.name}一共有多少条`, desc: `表 ${t.name}` })
    }
    return out
  }, [schema, mode])

  return (
    <div className="welcome">
      <div className="welcome-intro">
        <div className="welcome-mark">↯</div>
        <div>
          <h2>{usable ? '今天想从数据里确认什么？' : '当前不可执行查询'}</h2>
          <p>
            {usable
              ? '系统会召回相关表、生成只读 SQL，并在护栏与成本检查通过后执行。结果附带 SQL，可自行核对。'
              : '先到「数据源」确认连接与白名单。'}
          </p>
        </div>
      </div>

      <section className="recent-queries" aria-labelledby="recentQueriesTitle">
        <div className="welcome-section-head">
          <div className="welcome-section-title">
            <strong id="recentQueriesTitle">最近查询</strong>
            <span>{sourceName}</span>
            <span>{recent.length} / {RECENT_QUERY_LIMIT}</span>
          </div>
          <button className="recent-clear" type="button"
            disabled={!recent.length || !guard.can}
            title={guard.props.title}
            onClick={onClear}>
            清空当前数据源
          </button>
        </div>
        <div className="recent-query-list" aria-live="polite">
          {recent.length === 0
            ? <div className="recent-query-empty">当前数据源还没有查询记录。完成一次查询后，会在这里安全保存并支持回填。</div>
            : recent.map(item => (
              <article className="recent-query-card" key={item.id}>
                <button
                  type="button"
                  className="recent-query-fill"
                  aria-label={`回填查询：${item.question}`}
                  onClick={() => onFill(item.question)}
                >
                  <span className="recent-query-text">{item.question}</span>
                  <span className="recent-query-meta">
                    <span>{item.sourceName}</span>
                    <span>{formatRecentQueryTime(item.timestamp)}</span>
                    <span className={`recent-query-status status-${item.status}`}>
                      {RECENT_QUERY_STATUS_LABELS[item.status]}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  className="recent-query-delete"
                  aria-label={`删除查询：${item.question}`}
                  onClick={() => onDelete(item.id)}
                >×</button>
              </article>
            ))}
        </div>
      </section>

      {samples.length > 0 && (
        <section className="recommendations" aria-labelledby="recommendedQueriesTitle">
          <div className="welcome-section-head">
            <div className="welcome-section-title">
              <strong id="recommendedQueriesTitle">推荐问题</strong>
              <span>点击后回填，可继续编辑</span>
            </div>
          </div>
          <div className="suggestions">
            {samples.map(s => (
              <button className="suggestion" key={s.title + s.text} onClick={() => onFill(s.text)}>
                <strong>{s.title}</strong><small>{s.desc}</small>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}


/** 一个数据源都没有时的兜底项。tables=0 因此发起按钮是禁用的，
 *  文案直接说下一步该干什么，不给一个点不动又不解释的按钮。 */
const EMPTY_SOURCE: SourceOption = {
  id: '', code: '··', name: '没有可用数据源',
  meta: '到「数据源」页添加一个只读数据源', tables: 0, dialect: '',
}

interface SourceOption {
  id: string
  code: string
  name: string
  meta: string
  /** 环境归属（prod_ro / test）。内置源没有，右栏据此退回通用写法 */
  env?: string
  /** 开放表数。0 张的源查不出任何东西 —— 与其让人查完撞 R-03，不如直接禁选 */
  tables: number
  /** SQL 方言。切了源方言就变了，SQL 页签要如实标 */
  dialect: string
}

function SourceSelector({ open, onToggle, onPick, current, options }: {
  open: boolean
  onToggle: () => void
  onPick: (id: string) => void
  current: SourceOption
  options: SourceOption[]
}) {
  const usable = options.filter(o => o.tables > 0).length
  return (
    <div className={`source-selector ${open ? 'open' : ''}`} onClick={e => e.stopPropagation()}>
      <button className="source-trigger" onClick={onToggle}>
        <span className="source-db">{current.code}</span>
        <span className="source-trigger-copy"><strong>{current.name}</strong><small>{current.meta}</small></span>
        <span className="source-caret">⌄</span>
      </button>
      <div className="source-menu">
        <div className="source-menu-label">
          <span>选择本次查询的数据源</span>
          <span>{usable} / {options.length} 可查</span>
        </div>
        {options.map(option => (
          <button
            key={option.id || 'builtin'}
            className={`source-option ${option.tables > 0 ? '' : 'disabled'} ${option.id === current.id ? 'active' : ''}`}
            title={option.tables > 0 ? undefined : '该数据源还没有开放任何表，到「数据源」页勾选后才能查'}
            onClick={() => option.tables > 0 && onPick(option.id)}
          >
            <span className="source-db">{option.code}</span>
            <span><strong>{option.name}</strong><small>{option.meta}</small></span>
            <span className="source-option-status">{option.tables > 0 ? '● 可查' : '未开放表'}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
