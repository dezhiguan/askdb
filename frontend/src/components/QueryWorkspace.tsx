import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { askQuestion, fetchSchema, runSql, type AskResult, type Me, type Schema } from '../api'
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
  const [menuOpen, setMenuOpen] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    let alive = true
    fetchSchema().then(s => { if (alive) setSchema(s) }).catch(() => {})
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [menuOpen])

  const ready = health.status === 'ready' ? health.health : null
  const canAsk = !!ready?.datasource.ok && !!ready?.llm.ok
  const canSql = !!ready?.datasource.ok
  const mode: Mode = modeChoice ?? (canAsk ? 'ask' : 'sql')

  // 内置源的名字取 health 里的真实库名，而不是配置文件路径 ——
  // 工作台上要回答的是"我在查哪个库"
  const options: SourceOption[] = [
    {
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
    },
    ...sourceCards.filter(c => !c.builtin).map(c => ({
      id: c.id,
      code: MARK[c.type] ?? c.type.slice(0, 2).toUpperCase(),
      name: c.name,
      meta: `${c.type} · ${c.host || '—'} · 开放 ${c.table_count} 张表`,
      tables: c.table_count,
      dialect: DIALECT[c.type] ?? c.type,
    })),
  ]
  const current = options.find(o => o.id === sourceId) ?? options[0]
  const usable = (mode === 'ask' ? canAsk : canSql) && current.tables > 0

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
    const bucket = { key: sourceKey, name: current.name }
    setRunning(true); setError('')
    recent.upsert(text, 'running', bucket)
    try {
      const value = mode === 'ask' ? await askQuestion(text, sourceId) : await runSql(text, sourceId)
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
function Welcome({ mode, schema, usable, sourceName, recent, onFill, onDelete, onClear }: {
  mode: Mode
  schema: Schema | null
  usable: boolean
  sourceName: string
  recent: RecentQuery[]
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
    const out = schema.metrics.slice(0, 2).map(m => ({
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
          <button className="recent-clear" type="button" disabled={!recent.length} onClick={onClear}>
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


interface SourceOption {
  id: string
  code: string
  name: string
  meta: string
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
