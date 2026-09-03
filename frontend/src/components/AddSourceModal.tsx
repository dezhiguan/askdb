import { useEffect, useState } from 'react'
import {
  createSource, scanSource, setSourceTables, testSource,
  type Probe, type ScannedTable, type SourceInput, type SourceList,
} from '../api'

/** 添加只读数据源。两步：填连接 → 勾选开放的表。
 *
 *  第二步不是可选的收尾。扫描只解决「看得见」，开放与否是单独一步 ——
 *  白名单同时是安全边界与准确率边界，扫完直接全开等于把两条边界一起取消。
 */
export function AddSourceModal({ meta, onClose, onDone }: {
  meta: SourceList
  onClose: () => void
  onDone: () => void
}) {
  const [step, setStep] = useState<'form' | 'tables'>('form')
  const [name, setName] = useState('新的只读数据源')
  const [type, setType] = useState(meta.supported_types[0] ?? 'postgresql')
  const [env, setEnv] = useState('test')
  const [addr, setAddr] = useState('')
  const [dbname, setDbname] = useState('')
  const [user, setUser] = useState('')
  const [credMode, setCredMode] = useState<'env' | 'plain'>('env')
  const [passwordEnv, setPasswordEnv] = useState('')
  const [password, setPassword] = useState('')

  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [probe, setProbe] = useState<Probe | null>(null)
  const [sourceId, setSourceId] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const isFile = type === 'duckdb'

  const input = (): SourceInput => ({
    name,
    type,
    // askdb 的连接串是 key=value 形式，表单按人看得懂的字段拆着填，这里再拼回去
    dsn: isFile ? addr : buildDsn(addr, dbname, user),
    env,
    password_env: credMode === 'env' ? passwordEnv.trim() : '',
    password: credMode === 'plain' ? password : '',
  })

  const run = async (what: 'test' | 'save') => {
    setBusy(what); setError('')
    try {
      if (what === 'test') {
        const result = await testSource(input())
        setProbe(result)
        if (!result.ok) setError(result.error ? `${result.error}${result.hint ? `｜${result.hint}` : ''}` : '连接自检未通过')
      } else {
        const result = await createSource(input())
        setSourceId(result.source.id)
        setProbe(result)
        setPicked(new Set())
        setStep('tables')
      }
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const saveTables = async () => {
    setBusy('tables'); setError('')
    try {
      await setSourceTables(sourceId, [...picked])
      onDone()
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={e => { if (e.currentTarget === e.target) onClose() }}>
      <div className={`modal source-modal ${step === 'tables' ? 'scan-modal' : ''}`}>
        <header className="modal-head modal-dark">
          <div>
            <h3>{step === 'form' ? '添加只读数据源' : '选择开放的表'}</h3>
            <p>
              {step === 'form'
                ? '连接凭证将加密保存到服务端，不下发浏览器，也不会提供给模型。'
                : '没勾选的表，模型看不见也查不到 —— 白名单同时是安全边界与准确率边界。'}
            </p>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        </header>

        <div className="modal-body">
          {error && <div className="audit-error">{error}</div>}

          {step === 'form' ? (
            <>
              <div className="form-row">
                <label htmlFor="src-name">数据源名称</label>
                <input id="src-name" value={name} onChange={e => setName(e.target.value)} />
              </div>

              <div className="form-grid">
                <div className="form-row">
                  <label htmlFor="src-type">数据库类型</label>
                  <select id="src-type" value={type} onChange={e => setType(e.target.value)}>
                    {meta.supported_types.map(t => (
                      <option key={t} value={t}>{TYPE_LABEL[t] ?? t}</option>
                    ))}
                  </select>
                </div>
                <div className="form-row">
                  <label htmlFor="src-env">环境</label>
                  <select id="src-env" value={env} onChange={e => setEnv(e.target.value)}>
                    <option value="test">测试环境</option>
                    <option value="prod_ro">生产只读镜像</option>
                  </select>
                </div>
              </div>

              {isFile ? (
                <div className="form-row">
                  <label htmlFor="src-file">数据库文件路径</label>
                  <input id="src-file" value={addr} onChange={e => setAddr(e.target.value)}
                         placeholder="data/sample.duckdb" />
                </div>
              ) : (
                <>
                  <div className="form-row">
                    <label htmlFor="src-addr">数据库地址</label>
                    <input id="src-addr" value={addr} onChange={e => setAddr(e.target.value)}
                           placeholder="db.internal:5432" />
                  </div>
                  <div className="form-row">
                    <label htmlFor="src-db">数据库名</label>
                    <input id="src-db" value={dbname} onChange={e => setDbname(e.target.value)} placeholder="orders" />
                  </div>
                  <div className="form-grid">
                    <div className="form-row">
                      <label htmlFor="src-user">只读用户名</label>
                      <input id="src-user" value={user} onChange={e => setUser(e.target.value)} placeholder="askdb_ro" />
                    </div>
                    <div className="form-row">
                      <label className="cred-head" htmlFor="src-cred">
                        口令
                        <span className="cred-modes">
                          <button
                            type="button"
                            className={credMode === 'env' ? 'on' : ''}
                            onClick={() => setCredMode('env')}
                          >环境变量名</button>
                          <button
                            type="button"
                            className={credMode === 'plain' ? 'on' : ''}
                            disabled={!meta.can_store_password}
                            title={meta.can_store_password ? undefined
                              : '服务端未配置 ASKDB_SECRET_KEY，不能保存明文口令'}
                            onClick={() => setCredMode('plain')}
                          >直接填密码</button>
                        </span>
                      </label>
                      {credMode === 'env'
                        ? <input id="src-cred" value={passwordEnv} onChange={e => setPasswordEnv(e.target.value)}
                                 placeholder="ASKDB_ORDERS_PASSWORD" />
                        : <input id="src-cred" type="password" value={password}
                                 onChange={e => setPassword(e.target.value)}
                                 placeholder="保存时用主密钥加密" />}
                    </div>
                  </div>
                  <p className="cred-note">
                    {credMode === 'env'
                      ? '推荐。填的是环境变量名，口令一个字都不落盘 —— 服务端读该变量取值。'
                      : '口令用服务端主密钥加密后存到 var/sources/。备份、镜像里都会多一份要看管的东西。'}
                  </p>
                </>
              )}

              {probe && probe.checks.length > 0 && (
                <div className="check-list">
                  {probe.checks.map(item => (
                    <div className="check-row" key={item.name}>
                      <span className={`check-dot ${item.ok ? '' : 'bad'}`}>{item.ok ? '✓' : '✕'}</span>
                      <b>{item.name}</b>
                      <span className="check-detail">{item.detail}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="modal-actions">
                <button className="ghost" onClick={onClose}>取消</button>
                <button className="secondary" disabled={!!busy} onClick={() => run('test')}>
                  {busy === 'test' ? '连接检查中…' : '测试连接'}
                </button>
                <button className="primary" disabled={!!busy} onClick={() => run('save')}>
                  {busy === 'save' ? '扫描中…' : '保存并扫描元数据'}
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="drawer-note">
                扫描到 {probe?.tables.length ?? 0} 张表，<b>默认一张都不开放</b>。
                勾选的表会带着字段名与类型写入白名单 —— R-04（字段真实性）与
                R-05（展开 SELECT *）靠它判定。
              </p>
              <div className="pick-list">
                {probe?.tables.map(table => (
                  <TablePick
                    key={table.name}
                    table={table}
                    checked={picked.has(table.name)}
                    onToggle={() => setPicked(current => {
                      const next = new Set(current)
                      if (next.has(table.name)) next.delete(table.name)
                      else next.add(table.name)
                      return next
                    })}
                  />
                ))}
              </div>
              <p className="cred-note">
                该数据源按<b>单租户</b>处理，不做行级隔离 —— 一次结构扫描看不出哪一列
                代表租户，猜错的后果是越权。要做隔离仍然得写配置文件。
              </p>
              <div className="modal-actions">
                <span className="pick-count">已选 {picked.size} / {probe?.tables.length ?? 0}</span>
                <button className="ghost" onClick={onDone}>稍后再选</button>
                <button className="primary" disabled={!!busy} onClick={saveTables}>
                  {busy === 'tables' ? '保存中…' : '保存白名单'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

const TYPE_LABEL: Record<string, string> = { postgresql: 'PostgreSQL', duckdb: 'DuckDB' }

function TablePick({ table, checked, onToggle }: {
  table: ScannedTable
  checked: boolean
  onToggle: () => void
}) {
  return (
    <button className={`pick-row ${checked ? 'on' : ''}`} onClick={onToggle}>
      <span className="pick-box">{checked ? '✓' : ''}</span>
      <span className="pick-name mono">{table.name}</span>
      <span className="pick-meta">
        {table.rows.toLocaleString()} 行 · {table.cols} 列
        {table.tenant && <span className="status">疑似租户列</span>}
      </span>
    </button>
  )
}

function buildDsn(addr: string, dbname: string, user: string): string {
  const [host, port] = addr.split(':')
  const parts = [`host=${host.trim()}`]
  if (port) parts.push(`port=${port.trim()}`)
  if (dbname.trim()) parts.push(`dbname=${dbname.trim()}`)
  if (user.trim()) parts.push(`user=${user.trim()}`)
  return parts.join(' ')
}

/** 给已存在的数据源重新扫描并调整白名单。与新增第二步同一套语义。 */
export function ScanTablesModal({ id, name, onClose, onDone }: {
  id: string
  name: string
  onClose: () => void
  onDone: () => void
}) {
  const [probe, setProbe] = useState<Probe | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    scanSource(id)
      .then(result => {
        if (!alive) return
        setProbe(result)
        setPicked(new Set(result.tables.filter(t => t.allowed).map(t => t.name)))
      })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [id])

  const save = async () => {
    setBusy(true); setError('')
    try {
      await setSourceTables(id, [...picked])
      onDone()
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={e => { if (e.currentTarget === e.target) onClose() }}>
      <div className="modal source-modal scan-modal">
        <header className="modal-head modal-dark">
          <div>
            <h3>开放的表</h3>
            <p>{name} · 取消勾选会立即把该表移出可查范围。</p>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="modal-body">
          {error && <div className="audit-error">{error}</div>}
          {!probe && !error && <p className="drawer-note">扫描中…</p>}
          <div className="pick-list">
            {probe?.tables.map(table => (
              <TablePick
                key={table.name}
                table={table}
                checked={picked.has(table.name)}
                onToggle={() => setPicked(current => {
                  const next = new Set(current)
                  if (next.has(table.name)) next.delete(table.name)
                  else next.add(table.name)
                  return next
                })}
              />
            ))}
          </div>
          <div className="modal-actions">
            <span className="pick-count">已选 {picked.size} / {probe?.tables.length ?? 0}</span>
            <button className="ghost" onClick={onClose}>取消</button>
            <button className="primary" disabled={busy || !probe} onClick={save}>
              {busy ? '保存中…' : '保存白名单'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
