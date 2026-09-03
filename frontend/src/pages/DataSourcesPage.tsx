import { useEffect, useMemo, useState } from 'react'
import {
  deleteSource, fetchIntrospect, fetchSchema, fetchSelfCheck, fetchSources, scanSource,
  type Introspect, type Schema, type SelfCheck, type SourceCard, type SourceList,
} from '../api'
import { AddSourceModal, ScanTablesModal } from '../components/AddSourceModal'
import type { HealthState } from '../useHealth'

/** 数据源类型的短标。图标位 34px，放不下全名。 */
const TYPE_MARK: Record<string, string> = { duckdb: 'DK', postgresql: 'PG' }

/** 副标题里的引擎名。卡片副标题是「引擎 · host:port」，引擎位要给人看的写法。 */
const TYPE_NAME: Record<string, string> = { duckdb: 'DuckDB', postgresql: 'PostgreSQL' }

/** 一张运行时数据源卡的连接检查结果。延迟与最后检查两格没有其它来源。 */
type CardProbe = { latency: number | null; at: Date }

const TENANT_MODE: Record<string, string> = {
  column: '租户列',
  filter: '谓词间接归属',
  exempt: '显式豁免',
  none: '未开放',
}

export function DataSourcesPage({ health }: { health: HealthState }) {
  const [schema, setSchema] = useState<Schema | null>(null)
  const [introspect, setIntrospect] = useState<Introspect | null>(null)
  const [check, setCheck] = useState<SelfCheck | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState('')
  const [showConfig, setShowConfig] = useState(false)
  const [showAdd, setShowAdd] = useState(false)
  const [manage, setManage] = useState<SourceCard | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [sources, setSources] = useState<SourceList | null>(null)
  const [reload, setReload] = useState(0)
  const [probes, setProbes] = useState<Record<string, CardProbe>>({})
  const [testingId, setTestingId] = useState('')

  useEffect(() => {
    let alive = true
    Promise.all([fetchSchema(), fetchIntrospect()])
      .then(([s, i]) => { if (alive) { setSchema(s); setIntrospect(i) } })
      .catch(e => { if (alive) setError(`读取数据源信息失败：${String(e.message || e)}`) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    let alive = true
    fetchSources()
      .then(value => { if (alive) setSources(value) })
      .catch(e => { if (alive) setError(`读取数据源信息失败：${String(e.message || e)}`) })
    return () => { alive = false }
  }, [reload])

  const removeBuiltin = async () => {
    // 这一条删的是配置文件里的 datasource 段，后果和删一条运行时源完全不同：
    // 删完之后不带数据源的查询会被直接拒绝。确认文案必须把这句说出来。
    if (!window.confirm(
      '删除默认数据源？\n\n'
      + `会从 ${ready?.config ?? '配置文件'} 里移除 datasource 段。`
      + '此后不指定数据源的查询将被拒绝，需要在本页选择一个已添加的数据源。\n\n'
      + '配置文件里的其他段（护栏、租户策略、业务口径）不受影响。'
    )) return
    try {
      await deleteSource('builtin')
      setReload(n => n + 1)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  const removeSource = async (card: SourceCard) => {
    if (!window.confirm(`删除数据源「${card.name}」？白名单一并删除，历史审计记录不受影响。`)) return
    try {
      await deleteSource(card.id)
      setReload(n => n + 1)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  // 运行时数据源的「测试连接」。真去建连并读一次元数据 —— 延迟和最后检查
  // 两格没有别的来源，不点就只能是「—」/ NEVER，不拿 0 冒充很快。
  const testCard = async (card: SourceCard) => {
    setTestingId(card.id)
    try {
      const result = await scanSource(card.id)
      setProbes(current => ({ ...current, [card.id]: { latency: result.latency_ms, at: new Date() } }))
      setError(result.ok ? '' : (result.error
        ? `${result.error}${result.hint ? `｜${result.hint}` : ''}`
        : `数据源「${card.name}」连接自检未通过`))
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setTestingId('')
    }
  }

  const runCheck = async () => {
    setChecking(true)
    try {
      setCheck(await fetchSelfCheck())
      setCheckedAt(new Date())
      setShowConfig(true)
      setError('')
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setChecking(false)
    }
  }

  // 注释覆盖率按列算不按表算 —— 一张 40 列的表只写了 2 条注释，
  // 按表算是「已覆盖」，按列算才看得出它会拖准确率
  const coverage = useMemo(() => {
    const columns = (schema?.tables ?? []).flatMap(t => t.columns)
    if (!columns.length) return null
    return Math.round(columns.filter(c => c.desc).length / columns.length * 100)
  }, [schema])

  const ready = health.status === 'ready' ? health.health : null
  const ds = ready?.datasource

  // 内置卡的去留以 /api/sources 为准，不看 health —— health 只在页面加载时取一次，
  // 删完之后它还会说"有默认源"，卡片就会赖在那儿不走。
  const builtinCard = sources?.items.find(item => item.builtin) ?? null
  const showBuiltin = sources ? !!builtinCard : true

  return (
    <div className="page">
      {/* 通用 PageHeader 没有 eyebrow 位，这里按原型直接写出 .page-head */}
      <div className="page-head">
        <div>
          <div className="eyebrow">Phase 1 · Readonly Data</div>
          <h1>数据源管理</h1>
          <p>只连接测试库和生产只读镜像，凭证由服务端托管，不下发浏览器也不进入提示词。</p>
        </div>
        <button
          className="primary"
          disabled={!sources?.can_add}
          title={sources && !sources.can_add
            ? '本实例未开启运行时添加数据源：服务端会按填入的地址主动建连，而 askdb 不设账号体系，对外实例一律关闭'
            : undefined}
          onClick={() => setShowAdd(true)}
        >＋ 添加数据源</button>
      </div>

      {error && <div className="audit-error">{error}</div>}

      {/* 配置里的默认数据源最多一个，所以内置卡最多一张；它可以被删掉，
          删掉之后本页只剩运行时数据源 */}
      <div className="source-grid">
        {showBuiltin && (
        <article className="source-card">
          <div className="source-top">
            <i className="db-icon">
              {ds ? (TYPE_MARK[ds.type] ?? ds.type.slice(0, 2).toUpperCase()) : '··'}
            </i>
            {ds
              ? <span className={`card-status ${ds.ok ? '' : 'bad'}`}>
                  {ds.ok ? <><i className="online" /> 正常</> : '● 不可用'}
                </span>
              : <span className="card-status off">● 读取中</span>}
          </div>
          <h3>{builtinCard?.name || '默认数据源'}</h3>
          {/* 原型的副标题是「引擎 · host:port」。配置文件路径挪进 title —— 它是运维信息，
              占掉副标题会把「这条连的是哪个库」挤没 */}
          <p title={ready?.config ? `配置文件：${ready.config}` : undefined}>
            {ds ? (TYPE_NAME[ds.type] ?? ds.type) : '—'} · {builtinCard?.host || ds?.detail || '—'}
            {ds && !ds.ok && ds.hint && <><br />{ds.hint}</>}
          </p>
          <div className="mini-metrics">
            <div className="mini-metric">
              <span>延迟</span>
              <strong>{check?.latency_ms == null ? '—' : `${check.latency_ms}ms`}</strong>
            </div>
            <div className="mini-metric">
              <span>可见表</span>
              <strong>{introspect?.allowed_count ?? '—'}</strong>
            </div>
            <div className="mini-metric">
              <span>凭证</span>
              {/* 写死一个 VAULT 是假的。askdb 的真实答案只有两种：
                  口令来自某个环境变量，或者这个库根本不需要口令。 */}
              <strong>{ds ? (ds.credential ? `ENV · ${ds.credential}` : '无需口令') : '—'}</strong>
            </div>
            <div className="mini-metric">
              <span>最后检查</span>
              <strong>{checkedAt ? relative(checkedAt) : 'NEVER'}</strong>
            </div>
          </div>
          <div className="source-actions">
            <button className="secondary" onClick={runCheck} disabled={checking}>
              {checking ? '连接检查中…' : '测试连接'}
            </button>
            <button className="ghost" onClick={() => setShowConfig(v => !v)}>
              {showConfig ? '收起配置' : '配置'}
            </button>
            <button
              className="ghost"
              disabled={!builtinCard?.deletable}
              title={builtinCard?.deletable ? undefined
                : '删除默认数据源会改写配置文件：需要开启 datasources.allow_runtime_add，'
                  + '且至少已添加一个别的数据源接手'}
              onClick={removeBuiltin}
            >删除</button>
          </div>
        </article>
        )}

        {sources?.items.filter(item => !item.builtin).map(card => {
          // 一张表都没开放 = 原型里的「待配置」：连上了但模型什么也看不见，
          // 这时候该催的是去勾表，不是去测连接
          const pending = card.table_count === 0
          const probe = probes[card.id]
          return (
            <article className="source-card" key={card.id}>
              <div className="source-top">
                <i className="db-icon">{TYPE_MARK[card.type] ?? card.type.slice(0, 2).toUpperCase()}</i>
                <span className={`card-status ${pending ? 'off' : ''}`}>
                  {pending ? '● 待配置' : <><i className="online" /> 正常</>}
                </span>
              </div>
              <h3>{card.name}</h3>
              <p>{TYPE_NAME[card.type] ?? card.type} · {card.host || '—'} · {ENV_LABEL[card.env] ?? card.env}</p>
              <div className="mini-metrics">
                <div className="mini-metric">
                  <span>延迟</span>
                  <strong>{probe?.latency == null ? '—' : `${probe.latency}ms`}</strong>
                </div>
                <div className="mini-metric"><span>可见表</span><strong>{card.table_count}</strong></div>
                <div className="mini-metric">
                  <span>凭证</span>
                  <strong>{card.credential || '无需口令'}</strong>
                </div>
                <div className="mini-metric">
                  <span>最后检查</span>
                  <strong>{probe ? relative(probe.at) : 'NEVER'}</strong>
                </div>
              </div>
              <div className="source-actions">
                {pending
                  ? <button className="primary" onClick={() => setManage(card)}>完成配置</button>
                  : (
                    <>
                      <button className="secondary" disabled={testingId === card.id} onClick={() => testCard(card)}>
                        {testingId === card.id ? '连接检查中…' : '测试连接'}
                      </button>
                      <button className="ghost" onClick={() => setManage(card)}>配置</button>
                    </>
                  )}
                <button className="ghost" onClick={() => removeSource(card)}>删除</button>
              </div>
            </article>
          )
        })}
      </div>

      {showConfig && (
        <div className="source-detail">
          <section className="card notice-card">
            <h3>为什么这里只能删、不能改</h3>
            <p>
              askdb 不设账号体系，<b>数据库连接本身即权限边界</b>。表白名单、租户隔离、业务口径
              都是跟着这份连接配的 —— 页面若能改连接，等于任何打开页面的人都能把这套护栏整体换掉。
              所以这张卡只提供删除：删掉是<b>把默认源撤掉</b>，护栏范围只会收窄不会放宽；
              而改连接会让同一套护栏落到另一个库上。要换默认源，仍然是改配置文件后重启。
            </p>
            <p>
              删除同样受 <span className="mono">datasources.allow_runtime_add</span> 约束，
              且必须先有别的数据源接手 —— 一个源都不剩的实例查不了任何东西。
            </p>
            <pre className="drawer-code">python -m askdb.cli serve -c config/askdb.yaml</pre>
          </section>

          {check && (
            <section className="card">
              <h3>
                连接自检
                <span className={`card-status ${check.ok ? '' : 'bad'}`}>
                  {check.ok ? <><i className="online" /> 全部通过</> : '● 有未通过项'}
                </span>
              </h3>
              <div className="check-list">
                {check.checks.map(item => (
                  <div className="check-row" key={item.name}>
                    <span className={`check-dot ${item.ok ? '' : 'bad'}`}>{item.ok ? '✓' : '✕'}</span>
                    <b>{item.name}</b>
                    <span className="check-detail">{item.detail}</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {ready && (
            <section className="card">
              <h3>护栏阈值<span className="section-note">SQL 执行前强制应用，改这些要改配置文件</span></h3>
              <div className="mini-metrics metrics-4">
                <div className="mini-metric"><span>返回行上限 · R-13</span><strong>{ready.guard.max_rows}</strong></div>
                <div className="mini-metric"><span>扫描行上限 · R-11</span><strong>{ready.guard.max_scan_rows.toLocaleString()}</strong></div>
                <div className="mini-metric"><span>语句超时 · R-12</span><strong>{ready.guard.timeout_ms} ms</strong></div>
                <div className="mini-metric"><span>重试上限 · R-14</span><strong>{ready.guard.max_retry}</strong></div>
              </div>
            </section>
          )}

          <section className="card table-scroll">
            <h3>
              表白名单
              <span className="section-note">
                库内共 {introspect?.total ?? '—'} 张 · 已开放 {introspect?.allowed_count ?? '—'} 张 ·
                注释覆盖率 {coverage == null ? '—' : `${coverage}%`}
                {coverage != null && coverage < 60 && ' · 偏低，会拖累选表与生成 SQL 的准确率'}
              </span>
            </h3>
            {introspect && !introspect.ok && (
              <div className="audit-error">
                连不上数据源：{introspect.error}{introspect.hint ? ` · ${introspect.hint}` : ''}
              </div>
            )}
            <table className="audit-table">
              <thead>
                <tr>
                  <th>表</th><th>说明</th><th className="num">行数</th><th className="num">列数</th>
                  <th className="num">注释</th><th>租户隔离</th><th>状态</th>
                </tr>
              </thead>
              <tbody>
                {introspect?.tables.map(table => (
                  <TableRow
                    key={table.name}
                    table={table}
                    schema={schema}
                    expanded={expanded === table.name}
                    onToggle={() => setExpanded(current => current === table.name ? null : table.name)}
                  />
                ))}
                {introspect?.ok && introspect.tables.length === 0 && (
                  <tr><td colSpan={7} className="audit-empty">库里没有可见的表</td></tr>
                )}
                {!introspect && <tr><td colSpan={7} className="audit-empty">读取中…</td></tr>}
              </tbody>
            </table>
          </section>
        </div>
      )}

      {showAdd && sources && (
        <AddSourceModal
          meta={sources}
          onClose={() => setShowAdd(false)}
          onDone={() => { setShowAdd(false); setReload(n => n + 1) }}
        />
      )}
      {manage && (
        <ScanTablesModal
          id={manage.id}
          name={manage.name}
          onClose={() => setManage(null)}
          onDone={() => { setManage(null); setReload(n => n + 1) }}
        />
      )}
    </div>
  )
}

const ENV_LABEL: Record<string, string> = { test: '测试环境', prod_ro: '生产只读', builtin: '内置' }

function relative(at: Date): string {
  const seconds = Math.floor((Date.now() - at.getTime()) / 1000)
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

function TableRow({ table, schema, expanded, onToggle }: {
  table: Introspect['tables'][number]
  schema: Schema | null
  expanded: boolean
  onToggle: () => void
}) {
  const spec = schema?.tables.find(t => t.name === table.name)

  return (
    <>
      <tr className={`table-row ${table.allowed ? '' : 'muted'}`} onClick={table.allowed ? onToggle : undefined}>
        <td className="mono">
          {table.allowed && <span className="row-caret">{expanded ? '▾' : '▸'}</span>}
          {table.name}
        </td>
        <td className="audit-question" title={table.desc}>{table.desc || <span className="dim">未写说明</span>}</td>
        <td className="num">{table.rows.toLocaleString()}</td>
        <td className="num">{table.cols}</td>
        <td className="num">{table.allowed ? `${table.coverage}%` : '—'}</td>
        <td>
          {table.allowed
            ? <>{TENANT_MODE[table.tenant_mode]}{table.tenant_via && <span className="dim"> · {table.tenant_via}</span>}</>
            : <span className="dim">—</span>}
        </td>
        <td>
          {table.allowed
            ? <span className="status">已开放</span>
            : <span className="status wait">未开放</span>}
        </td>
      </tr>
      {expanded && spec && (
        <tr className="table-detail">
          <td colSpan={7}>
            {spec.aliases.length > 0 && <p className="drawer-note">别名：{spec.aliases.join('、')}</p>}
            <table className="drawer-table">
              <thead><tr><th>字段</th><th>类型</th><th>说明</th><th>取值</th></tr></thead>
              <tbody>
                {spec.columns.map(column => (
                  <tr key={column.name}>
                    <td className="mono">
                      {column.name}
                      {column.tenant && <span className="status">租户列</span>}
                    </td>
                    <td className="mono dim">{column.type}</td>
                    <td>{column.desc || <span className="dim">未写说明</span>}</td>
                    <td className="dim">{column.enum.join(' / ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  )
}
