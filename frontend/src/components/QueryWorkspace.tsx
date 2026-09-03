import { useEffect, useMemo, useState } from 'react'
import { askQuestion, fetchSchema, runSql, type AskResult, type Schema } from '../api'
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

export function QueryWorkspace({ health, sources, onNavigate }: {
  health: HealthState
  /** 数据源选择与顶栏共用同一份状态 —— 各存一份必然漂移，
   *  而漂移的表现是"正在查 A 库、顶栏说你在 B 库" */
  sources: SourcesState
  onNavigate: (view: View) => void
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

  const pickSource = (id: string) => {
    if (id === sourceId) { setMenuOpen(false); return }
    setSourceId(id)
    setMenuOpen(false)
    // 旧结果出自另一个库，留着就是张冠李戴
    setResult(null)
    setError('')
  }

  const run = async () => {
    const text = question.trim()
    if (!text) return
    setRunning(true); setError('')
    try {
      const value = mode === 'ask' ? await askQuestion(text, sourceId) : await runSql(text, sourceId)
      setResult(value)
      // 被拦下时先看拦截原因，而不是一张空结果表
      setTab(value.ok ? 'result' : value.rejected_by === 'INTERRUPTED' ? 'checkpoint' : 'sql')
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="workspace-grid">
      <section className="query-stage">
        <div className="stage-head">
          <SourceSelector
            open={menuOpen}
            onToggle={() => setMenuOpen(v => !v)}
            onPick={pickSource}
            current={current}
            options={options}
          />
          <div className="mode-tabs">
            <button className={mode === 'ask' ? 'active' : ''} disabled={!canAsk}
                    title={canAsk ? undefined : '未配置模型密钥，自然语言提问不可用'}
                    onClick={() => setModeChoice('ask')}>自然语言提问</button>
            <button className={mode === 'sql' ? 'active' : ''} disabled={!canSql}
                    onClick={() => setModeChoice('sql')}>直查 SQL</button>
          </div>
        </div>

        <div className="composer">
          <div>
            <textarea
              className={mode === 'sql' ? 'mono' : ''}
              value={question}
              onChange={e => setQuestion(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey && mode === 'ask') { e.preventDefault(); run() }
              }}
              placeholder={mode === 'ask' ? '例如：各知识库分别有多少文档'
                : 'SELECT ... —— 直查不经模型，只跑护栏、干跑与只读执行'}
            />
            <button onClick={run} disabled={running || !usable || !question.trim()}>
              {running ? '…' : '↗'}
            </button>
          </div>
          <small>
            <span>{mode === 'ask' ? 'Enter 查询 · Shift + Enter 换行' : '直查不消耗 token，配额用尽后仍可用'}</span>
            <span>✓ 历史查询不会自动进入本次上下文</span>
          </small>
        </div>

        {error && <div className="audit-error stage-error">{error}</div>}

        {result
          ? <ResultTabs result={result} active={tab} dialect={current.dialect}
                        onChange={setTab} onResumed={setResult}
                        onOpenTrace={() => onNavigate('traces')} />
          : <Welcome mode={mode} schema={schema} usable={usable} onPick={setQuestion} />}
      </section>

      <TrustSidebar health={health} source={current} result={result} onResultTab={setTab} onNavigate={onNavigate} />
    </div>
  )
}

const MARK: Record<string, string> = { duckdb: 'DK', postgresql: 'PG' }
const DIALECT: Record<string, string> = { duckdb: 'DuckDB', postgresql: 'PostgreSQL' }

/** 示例问题按**当前库的白名单和口径**生成，不写死。
 *  写死的示例换个数据源就全是查不出结果的废话，还会让人以为库里有这些表。 */
function Welcome({ mode, schema, usable, onPick }: {
  mode: Mode
  schema: Schema | null
  usable: boolean
  onPick: (text: string) => void
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
      <div className="welcome-mark">↯</div>
      <h2>{usable ? '今天想从数据里确认什么？' : '当前不可执行查询'}</h2>
      <p>
        {usable
          ? '系统会召回相关表、生成只读 SQL，并在护栏与成本检查通过后执行。结果附带 SQL，可自行核对。'
          : '先到「数据源」确认连接与白名单。'}
      </p>
      {samples.length > 0 && (
        <div className="suggestions">
          {samples.map(s => (
            <button key={s.title + s.text} onClick={() => onPick(s.text)}>
              <strong>{s.title}</strong><small>{s.desc}</small>
            </button>
          ))}
        </div>
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
        <span className="source-code">{current.code}</span>
        <span><strong>{current.name}</strong><small>{current.meta}</small></span>
        <b>⌄</b>
      </button>
      {open && (
        <div className="source-menu">
          <div className="source-menu-label">
            <span>选择本次查询的数据源</span>
            <span>{usable} / {options.length} 可查</span>
          </div>
          {options.map(option => (
            <button
              key={option.id || 'builtin'}
              className={`${option.tables > 0 ? '' : 'disabled'} ${option.id === current.id ? 'active' : ''}`}
              title={option.tables > 0 ? undefined : '该数据源还没有开放任何表，到「数据源」页勾选后才能查'}
              onClick={() => option.tables > 0 && onPick(option.id)}
            >
              <span className="source-code">{option.code}</span>
              <span><strong>{option.name}</strong><small>{option.meta}</small></span>
              <em>{option.tables > 0 ? '● 可查' : '未开放表'}</em>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
