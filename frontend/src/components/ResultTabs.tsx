import { useEffect, useState } from 'react'
import { resumeTask, type AskResult } from '../api'
import { STEP_NAMES } from '../traceSteps'
import type { ResultTab } from '../types'

/** 拦截规则的人话与处置建议。
 *  只给规则号（R-03）等于把排查成本原样丢给用户 —— 他不知道那是什么。 */
const RULES: Record<string, [string, string]> = {
  'R-01': ['只能执行一条语句', '把多条语句拆开，一次问一件事。'],
  'R-02': ['只允许查询，不允许改数据', 'askdb 是只读工具。要改数据请走正常的运维流程。'],
  'R-03': ['用到了没有开放的表', '到「数据源」把这张表加进白名单。没开放的表模型看不见也查不到。'],
  'R-04': ['用到了不存在的字段', '错误信息里已列出该表的真实字段名，照着改即可。'],
  'R-05': ['不允许 SELECT *', '显式写出需要的列，避免把不该看的字段也带出来。'],
  'R-06': ['不允许跨 schema / 跨库引用', '只查白名单内的表，不要带 schema 前缀。'],
  'R-07': ['用到了被禁用的函数', '这些函数能读文件或访问外部资源，不在只读查询的范围内。'],
  'R-08': ['出现了笛卡尔积', '补上 JOIN 条件，否则扫描量会失控。'],
  'R-10': ['无法确定这条查询属于哪个租户', '换个写法，别用会掩盖表来源的构造。'],
  'R-11': ['预计要扫描的数据量太大', '加上时间范围或更具体的筛选条件，把扫描量降下来。'],
  'R-17': ['本次任务的累计成本已达上限', '拆成更小的问题重新提问。'],
  QUOTA: ['今日调用配额已用完', '直查 SQL 不消耗 token，配额用尽后仍然可用。'],
  INTERRUPTED: ['执行在中断点停止', '下面可以从断点继续，已完成的节点不会重跑。'],
  EXEC: ['数据源出问题了', ''],
  LLM: ['模型调用没成功', ''],
  NO_SQL: ['这个问题用现有的表回答不了', '换个问法，或到「数据源」开放更多表。'],
}

export function ResultTabs({ result, active, onChange, onResumed }: {
  result: AskResult
  active: ResultTab
  onChange: (tab: ResultTab) => void
  onResumed: (result: AskResult) => void
}) {
  const interrupted = result.rejected_by === 'INTERRUPTED'
  const tabs: [ResultTab, string][] = [
    ['result', '查询结果'],
    ['sql', '原生 SQL'],
    ['chain', '执行链路'],
    ['checkpoint', interrupted ? '断点恢复' : '本次判定'],
  ]

  return (
    <div className="result-shell">
      <div className="result-tabs">
        {tabs.map(([id, label]) => (
          <button className={active === id ? 'active' : ''} key={id} onClick={() => onChange(id)}>
            {label}
            {id === 'checkpoint' && interrupted && <i className="tab-dot" />}
          </button>
        ))}
      </div>
      {active === 'result' && <ResultPane result={result} />}
      {active === 'sql' && <SqlPane result={result} />}
      {active === 'chain' && <ChainPane result={result} />}
      {active === 'checkpoint' && <CheckpointPane result={result} onResumed={onResumed} />}
    </div>
  )
}

function ResultPane({ result }: { result: AskResult }) {
  if (!result.ok) {
    const [title, fix] = RULES[result.rejected_by ?? ''] ?? ['这次查询没能完成', '']
    return (
      <div className="pane">
        <div className="notice bad">
          <div className="t">
            {result.rejected_by && <span className="rulecode">{result.rejected_by}</span>}
            {title}
          </div>
          {result.error && <div className="why">{result.error}</div>}
          {(result.hint || fix) && <div className="fix"><b>下一步：</b>{result.hint || fix}</div>}
        </div>
      </div>
    )
  }

  if (!result.row_count) {
    return (
      <div className="pane">
        <div className="notice info">
          <div className="t">◇ 结果为空</div>
          <div className="why">SQL 正常执行了，只是没有符合条件的行 —— 这不是错误。</div>
          <div className="fix">
            <b>可能的原因：</b>时间范围内确实没有数据；筛选条件太严；
            或当前租户（org_id = {result.org_id}）下没有这类记录。上面的 SQL 可以复制出来自己调。
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="pane">
      <div className="result-meta">
        {result.row_count.toLocaleString()} 行
        {result.truncated && <span className="warn-chip">已按行数上限截断 · R-13</span>}
        {/* 不标数据时间的结果，隔天再看会被当成当前状态 */}
        {result.as_of && <span> · 数据截至 {result.as_of}</span>}
        <span> · 结果附带上方 SQL，可自行核对</span>
      </div>
      <div className="table-scroll">
        <table>
          <thead><tr>{result.columns?.map(c => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>
            {result.rows?.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{cell === null ? <span className="dim">NULL</span> : String(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SqlPane({ result }: { result: AskResult }) {
  const [copied, setCopied] = useState(false)
  // 连同它算的是哪条 SQL 一起存，渲染时比对 —— 换了 SQL 但摘要还没算出来时
  // 宁可显示「—」，也不能把上一条的摘要挂在新 SQL 底下
  const [digest, setDigest] = useState<{ sql: string; hex: string } | null>(null)
  const sql = result.sql_final || result.sql_raw || ''

  useEffect(() => {
    // SHA-256 是给「拿去别处复核」用的锚点。crypto.subtle 只在安全上下文可用，
    // 取不到就如实留空，不要拿别的哈希冒充
    if (!sql || !globalThis.crypto?.subtle) return
    let alive = true
    globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(sql))
      .then(buf => {
        if (!alive) return
        const hex = [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('')
        setDigest({ sql, hex })
      })
      .catch(() => {})
    return () => { alive = false }
  }, [sql])

  const hash = digest?.sql === sql ? digest.hex : ''

  if (!sql) return <div className="pane"><p className="drawer-note">这次没有产出 SQL。</p></div>

  const executed = result.ok
  return (
    <div className="pane">
      <div className="sql-head">
        <b>
          {executed ? '最终 SQL（实际执行的就是这条）'
            : result.sql_final ? '改写后的 SQL（未执行）' : '模型产出（未执行）'}
        </b>
        <span className={`status ${executed ? '' : 'bad'}`}>
          {executed ? '护栏通过 · 只读执行'
            : result.rejected_by ? `已拦截 · ${result.rejected_by}` : '未执行'}
        </span>
        <button className="link-button" onClick={() => {
          navigator.clipboard?.writeText(sql)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1400)
        }}>{copied ? '已复制' : '复制'}</button>
      </div>
      <pre className="drawer-code">{sql}</pre>

      {!!result.rewrites?.length && (
        <div className="rewrites">
          <b>护栏改写</b>
          <ul>{result.rewrites.map(r => <li key={r}>{r}</li>)}</ul>
        </div>
      )}
      {!!result.sql_raw && result.sql_raw !== result.sql_final && (
        <details>
          <summary>模型原始 SQL（改写前）</summary>
          <pre className="drawer-code">{result.sql_raw}</pre>
        </details>
      )}
      <div className="sql-facts">
        <div><span>TRACE ID</span><code>{result.trace_id}</code></div>
        <div><span>SQL SHA-256</span><code>{hash ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : '—'}</code></div>
      </div>
    </div>
  )
}

function ChainPane({ result }: { result: AskResult }) {
  const steps = result.steps ?? []
  if (!steps.length) return <div className="pane"><p className="drawer-note">这条记录没有步骤明细。</p></div>

  const total = steps.reduce((sum, s) => sum + (s.ms || 0), 0) || 1
  return (
    <div className="pane">
      <div className="result-meta">
        {result.step_count ?? 1} 步 · {result.attempts ?? 1} 轮 · {result.elapsed_ms ?? 0} ms ·
        {' '}{result.tok_in ?? 0}+{result.tok_out ?? 0} tok · ¥{(result.cost_cny ?? 0).toFixed(4)}
      </div>
      <div className="chain-list">
        {steps.map((step, i) => (
          <div className={`chain-row ${step.status !== 'ok' ? 'bad' : ''}`} key={`${step.step}-${i}`}>
            <span className="chain-dot">{step.status === 'ok' ? '✓' : step.status === 'blocked' ? '✕' : '!'}</span>
            <span className="chain-main">
              <b>{STEP_NAMES[step.step] ?? step.step}</b>
              {step.note && <small>{step.note}</small>}
            </span>
            <span className="chain-bar"><i style={{ width: `${Math.max(2, (step.ms || 0) / total * 100)}%` }} /></span>
            <span className="chain-ms">
              {step.ms} ms{step.tok_in ? ` · ${step.tok_in}+${step.tok_out} tok` : ''}
            </span>
          </div>
        ))}
      </div>
      {!!result.tables_hit?.length && (
        <div className="hit-box">
          <b>命中</b>
          {result.tables_hit.map(t => <span className="tag" key={t}>{t}</span>)}
          {result.metrics_hit?.map(m => <span className="tag metric" key={m}>口径 {m}</span>)}
        </div>
      )}
    </div>
  )
}

/** 中断时是续跑面板；正常完成时展示这次判定的事实，不留空页签。 */
function CheckpointPane({ result, onResumed }: {
  result: AskResult
  onResumed: (result: AskResult) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const interrupted = result.rejected_by === 'INTERRUPTED'
  const thread = result.thread_id || result.trace_id

  const resume = async () => {
    setBusy(true); setError('')
    try {
      const value = await resumeTask(thread)
      if (value === null) {
        setError('这条任务已经跑完或不存在，无法续跑。')
        return
      }
      onResumed(value)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  if (!interrupted) {
    return (
      <div className="pane">
        <div className="policy-grid">
          <div><span>检查点线程</span><strong className="mono">{thread}</strong></div>
          <div><span>执行轮次</span><strong>{result.attempts ?? 1}</strong></div>
          <div><span>是否多步</span><strong>{result.multi_step ? '多步' : '单步'}</strong></div>
          <div><span>提前收敛</span><strong>{result.converged_early || '—'}</strong></div>
        </div>
        <p className="drawer-note">
          每次提问的检查点都会落盘，跨进程重启存活。本次正常完成，没有可续跑的断点。
          判定链路可在审计中心按 trace 复放。
        </p>
      </div>
    )
  }

  return (
    <div className="pane">
      <div className="notice bad">
        <div className="t"><span className="rulecode">INTERRUPTED</span>执行在中断点停止</div>
        {result.error && <div className="why">{result.error}</div>}
        <div className="fix">
          <b>下一步：</b>从断点继续。已完成的节点直接沿用，不会重新执行；
          预算计数一并恢复，续跑不会绕过成本上限。
        </div>
      </div>
      <div className="policy-grid">
        <div><span>检查点线程</span><strong className="mono">{thread}</strong></div>
        <div><span>已执行轮次</span><strong>{result.attempts ?? 1}</strong></div>
        <div><span>已花费</span><strong>¥{(result.cost_cny ?? 0).toFixed(4)}</strong></div>
        <div><span>已用 token</span><strong>{(result.tok_in ?? 0) + (result.tok_out ?? 0)}</strong></div>
      </div>
      {error && <div className="audit-error">{error}</div>}
      <div className="modal-actions">
        <button className="primary" disabled={busy} onClick={resume}>
          {busy ? '续跑中…' : '从断点继续'}
        </button>
      </div>
    </div>
  )
}
