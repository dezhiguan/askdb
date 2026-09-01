import { Fragment, useEffect, useState } from 'react'
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

/** SQL 的 SHA-256。连同它算的是哪条 SQL 一起存，渲染时比对 ——
 *  换了 SQL 但摘要还没算出来时宁可显示「—」，也不能把上一条的摘要挂在新 SQL 底下。
 *  crypto.subtle 只在安全上下文可用，取不到就如实留空，不拿别的哈希冒充。 */
function useSqlDigest(sql: string): string {
  const [digest, setDigest] = useState<{ sql: string; hex: string } | null>(null)

  useEffect(() => {
    if (!sql || !globalThis.crypto?.subtle) return
    let alive = true
    globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(sql))
      .then(buf => {
        if (!alive) return
        setDigest({ sql, hex: [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('') })
      })
      .catch(() => {})
    return () => { alive = false }
  }, [sql])

  return digest?.sql === sql ? digest.hex : ''
}

/** 结论卡。
 *
 *  原型这里是一句自然语言结论（"今天共有 18 笔支付失败订单，相比昨日同期
 *  下降 14.3%"）。askdb **不产出结论散文** —— 它返回行，附带 SQL 让人自验。
 *  那句对比更是设计稿的虚构：后端没有任何同比口径。
 *
 *  所以结论只说数据本身说得出的话：单值结果直接把那个值念出来（它就是答案），
 *  多行结果如实说返回了多少行。副标题用模型对自己所写 SQL 的说明，没有就不显示。
 */
function AnswerCard({ result, onGoTab }: {
  result: AskResult
  onGoTab: (tab: ResultTab) => void
}) {
  const sql = result.sql_final || result.sql_raw || ''
  const hash = useSqlDigest(sql)

  const single = result.row_count === 1 && result.columns?.length === 1 && result.rows?.[0]?.[0] != null
  const headline = single
    ? `${result.columns![0]}：${fmtValue(result.rows![0][0])}`
    : `查询返回 ${(result.row_count ?? 0).toLocaleString()} 行`

  return (
    <div className="answer-card">
      <div className="answer-top">
        <div>
          <h3>{headline}</h3>
          {result.reasoning && <p>{result.reasoning}</p>}
        </div>
        <div className="answer-actions">
          <button className="ghost" onClick={() => onGoTab('sql')}>查看原生 SQL</button>
          <button className="ghost" onClick={() => onGoTab('chain')}>查看执行链路</button>
        </div>
      </div>
      <div className="answer-facts">
        <div><span>查询 ID</span><strong>{result.trace_id}</strong></div>
        <div><span>SQL SHA-256</span><strong>{hash ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : '—'}</strong></div>
        <div><span>数据快照</span><strong>{result.as_of ? fmtStamp(result.as_of) : '—'}</strong></div>
        <div>
          <span>返回 / 扫描</span>
          <strong>
            {(result.row_count ?? 0).toLocaleString()} / {result.explain_rows == null
              ? '—' : result.explain_rows.toLocaleString()} ROWS
          </strong>
        </div>
      </div>
    </div>
  )
}

function fmtValue(v: string | number | boolean | null): string {
  return typeof v === 'number' ? v.toLocaleString() : String(v)
}

function fmtStamp(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function ResultTabs({ result, active, dialect, onChange, onResumed }: {
  result: AskResult
  active: ResultTab
  /** 当前数据源的方言，SQL 页签的 meta 行要如实标出来 */
  dialect: string
  onChange: (tab: ResultTab) => void
  onResumed: (result: AskResult) => void
}) {
  const interrupted = result.rejected_by === 'INTERRUPTED'
  const tabs: [ResultTab, string][] = [
    ['result', '查询结果'],
    ['sql', '原生 SQL'],
    ['chain', '执行链路'],
    ['checkpoint', '人工介入 / 断点恢复'],
  ]

  return (
    <div className="result-shell">
      {/* 被拦下时不出结论卡：那时候该看的是拦截原因，不是"返回 0 行" */}
      {result.ok && <AnswerCard result={result} onGoTab={onChange} />}
      <div className="result-tabs">
        {tabs.map(([id, label]) => (
          <button className={active === id ? 'active' : ''} key={id} onClick={() => onChange(id)}>
            {label}
            {id === 'checkpoint' && interrupted && <i className="tab-dot" />}
          </button>
        ))}
      </div>
      {active === 'result' && <ResultPane result={result} />}
      {active === 'sql' && <SqlPane result={result} dialect={dialect} />}
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
                  <td key={j} className={typeof cell === 'number' ? 'num-cell' : ''}>
                    {cell === null ? <span className="dim">NULL</span> : fmtValue(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const SQL_KEYWORDS = new Set([
  'select', 'from', 'where', 'group', 'by', 'order', 'having', 'limit', 'offset',
  'join', 'inner', 'left', 'right', 'full', 'outer', 'on', 'as', 'and', 'or', 'not',
  'in', 'is', 'null', 'case', 'when', 'then', 'else', 'end', 'with', 'union', 'all',
  'distinct', 'count', 'sum', 'avg', 'min', 'max', 'asc', 'desc', 'filter', 'over',
  'partition', 'cast', 'between', 'like', 'ilike', 'exists', 'coalesce',
])

/** 把 SQL 切成可着色的片段。
 *
 *  刻意不用 dangerouslySetInnerHTML —— SQL 里带着数据库对象名，
 *  拼进 innerHTML 就是把转义责任交给自己，而 React 默认转义本来就是对的。
 *  切片渲染成元素，多几行代码换掉一整类注入面。
 */
function tokenizeSql(sql: string) {
  const out: { text: string; cls: string }[] = []
  // 注释 / 字符串 / 词，三类分别成段，其余原样
  const re = /(--[^\n]*|'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)/g
  let last = 0
  for (let m = re.exec(sql); m; m = re.exec(sql)) {
    if (m.index > last) out.push({ text: sql.slice(last, m.index), cls: '' })
    const t = m[0]
    const cls = t.startsWith('--') ? 'comment'
      : t.startsWith("'") ? 'str'
      : SQL_KEYWORDS.has(t.toLowerCase()) ? 'kw' : ''
    out.push({ text: t, cls })
    last = m.index + t.length
  }
  if (last < sql.length) out.push({ text: sql.slice(last), cls: '' })
  return out
}

function SqlPane({ result, dialect }: { result: AskResult; dialect: string }) {
  const [copied, setCopied] = useState(false)
  const sql = result.sql_final || result.sql_raw || ''
  const hash = useSqlDigest(sql)

  if (!sql) return <div className="pane"><p className="drawer-note">这次没有产出 SQL。</p></div>

  const rewrites = result.rewrites ?? []
  return (
    <div className="pane">
      <div className="sql-toolbar">
        <span>
          方言：{dialect || '—'} · SHA-256: {hash ? `${hash.slice(0, 4)}…${hash.slice(-4)}` : '—'} ·{' '}
          {/* 原型这行写死「未经格式改写」。askdb 的最终 SQL 恰恰是被护栏改写过的
              （注入租户谓词、补 LIMIT、展开 SELECT *），照抄就是撒谎 */}
          {rewrites.length ? `已按护栏改写 ${rewrites.length} 处` : '未经改写'}
        </span>
        <button onClick={() => {
          navigator.clipboard?.writeText(sql)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1200)
        }}>{copied ? '已复制' : '复制原生 SQL'}</button>
      </div>
      <pre className="sql-code">
        {tokenizeSql(sql).map((tok, i) =>
          tok.cls ? <span className={tok.cls} key={i}>{tok.text}</span> : tok.text)}
      </pre>

      <div className="sql-status">
        <span className={`status ${result.ok ? '' : 'bad'}`}>
          {result.ok ? '护栏通过 · 只读执行'
            : result.rejected_by ? `已拦截 · ${result.rejected_by}` : '未执行'}
        </span>
        <span className="drawer-note">
          {result.ok ? '实际执行的就是这条。复制出去核对，哈希应当一致。'
            : '这条没有执行。'}
        </span>
      </div>

      {rewrites.length > 0 && (
        <div className="rewrites">
          <b>护栏改写</b>
          <ul>{rewrites.map(r => <li key={r}>{r}</li>)}</ul>
        </div>
      )}
      {!!result.sql_raw && result.sql_raw !== result.sql_final && (
        <details>
          <summary>模型原始 SQL（改写前）</summary>
          <pre className="sql-code plain">{result.sql_raw}</pre>
        </details>
      )}
    </div>
  )
}

function ChainPane({ result }: { result: AskResult }) {
  const steps = result.steps ?? []
  if (!steps.length) return <div className="pane"><p className="drawer-note">这条记录没有步骤明细。</p></div>

  const total = steps.reduce((sum, s) => sum + (s.ms || 0), 0) || 1
  return (
    <div className="pane">
      {/* 原型顶部那条横向节点链。它给的是"这次经过了哪几段、各多久"的全貌，
          下面的明细列表给的是每段具体做了什么 —— 两者都要，缺一个都得靠猜 */}
      <div className="mini-trace">
        {steps.map((step, i) => (
          <Fragment key={`${step.step}-${i}`}>
            {i > 0 && <i className="mini-trace-arrow">→</i>}
            <div className={`mini-trace-node ${step.status !== 'ok' ? 'bad' : ''}`}>
              <strong>{STEP_NAMES[step.step] ?? step.step}</strong>
              <small>{step.ms}ms</small>
            </div>
          </Fragment>
        ))}
      </div>
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
