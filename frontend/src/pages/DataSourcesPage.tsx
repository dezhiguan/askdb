import { useEffect, useMemo, useState } from 'react'
import {
  fetchIntrospect, fetchSchema, fetchSelfCheck,
  type Introspect, type Schema, type SelfCheck,
} from '../api'
import { PageHeader } from '../components/AppShell'
import type { HealthState } from '../useHealth'

/** 数据源类型的短标。图标位 34px，放不下全名。 */
const TYPE_MARK: Record<string, string> = { duckdb: 'DK', postgresql: 'PG' }

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
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    Promise.all([fetchSchema(), fetchIntrospect()])
      .then(([s, i]) => { if (alive) { setSchema(s); setIntrospect(i) } })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

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

  return (
    <div className="page">
      <PageHeader
        eyebrow="Phase 1 · Readonly Data"
        title="数据源管理"
        description="只连测试库与生产只读镜像，连接由配置文件指定。表白名单同时是安全边界与准确率边界。"
        action={<button className="primary" onClick={() => setShowAdd(true)}>＋ 添加数据源</button>}
      />

      {error && <div className="audit-error">读取数据源信息失败：{error}</div>}

      {/* 一个进程一份配置一个数据源，所以网格里只会有一张卡 */}
      <div className="source-grid">
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
          <h3>{ds ? ds.detail : '—'}</h3>
          <p>
            {ds ? ds.type : '—'} · {ready?.config ?? '—'}
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
              {checking ? '检查中…' : '测试连接'}
            </button>
            <button className="ghost" onClick={() => setShowConfig(v => !v)}>
              {showConfig ? '收起配置' : '配置'}
            </button>
          </div>
        </article>
      </div>

      {showConfig && (
        <div className="source-detail">
          <section className="card notice-card">
            <h3>为什么这里不能改连接</h3>
            <p>
              askdb 不设账号体系，<b>数据库连接本身即权限边界</b>。表白名单、租户隔离、业务口径
              都是跟着这份连接配的 —— 页面若能改连接，等于任何打开页面的人都能把这套护栏整体换掉。
              换数据源请换配置文件后重启。
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

      {showAdd && <AddSourceModal onClose={() => setShowAdd(false)} />}
    </div>
  )
}

/** 「添加数据源」不是一个表单。
 *
 *  在 askdb 里新增数据源 = 写一份配置 + 重启进程，页面无权参与 ——
 *  所以这里给的是真正要做的事，而不是一个收数据库口令、点了假装成功的表单。 */
function AddSourceModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onMouseDown={event => { if (event.currentTarget === event.target) onClose() }}>
      <div className="modal">
        <header>
          <div>
            <div className="eyebrow">Config-driven</div>
            <h3>添加数据源</h3>
            <p>连接由配置文件指定，页面不参与 —— 下面是实际要做的三步。</p>
          </div>
          <button onClick={onClose} aria-label="关闭">×</button>
        </header>

        <div className="modal-body">
          <ol className="add-steps">
          <li>
            <b>写一份配置</b>
            <span>复制 <code>config/askdb.yaml</code>，改 <code>datasource</code> 段。口令只写环境变量名，不写值。</span>
            <pre className="drawer-code">{`datasource:
  type: postgresql
  dsn: "host=… dbname=… user=…"
  password_env: ASKDB_DB_PASSWORD
  upstream: "10.0.0.7:5432"   # 经隧道时写真实库地址`}</pre>
          </li>
          <li>
            <b>配一份表白名单与业务口径</b>
            <span>
              同名的 <code>*-tables.yaml</code> 与 <code>*-metrics.yaml</code>。
              没进白名单的表，模型看不见也查不到 —— 白名单同时是安全边界与准确率边界。
            </span>
          </li>
          <li>
            <b>用这份配置起一个进程</b>
            <span>一个进程一份配置一个数据源。换数据源 = 换配置重启，不是在页面上切。</span>
            <pre className="drawer-code">python -m askdb.cli serve -c config/你的配置.yaml</pre>
          </li>
          </ol>

        <div className="modal-actions">
            <button className="primary" onClick={onClose}>知道了</button>
          </div>
        </div>
      </div>
    </div>
  )
}

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
