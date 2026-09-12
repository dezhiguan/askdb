import { useState } from 'react'

/* 结果详情正文 —— 任务中心与执行链路共用的那一份。
 *
 * 两页展示的本来就是同一条 /api/result，此前各写一套 JSX，结果是字号、
 * 表格圆角、有没有答案区都开始分叉。这里收成一个组件，两边只负责把数据
 * 递进来。
 *
 * 顺序是这一版改造的全部重点，按提问的相关度从近到远排：
 *
 *   提问 → 结果数据 → 答案说明 → 溯源（折叠）
 *
 * 改之前是倒过来的：一整段模型原文（含 markdown 表格、`**` 星号）铺在最上面，
 * 真正的结果行被压成一条横向滚动条塞在底部，执行事实与 SQL 又长又常驻。
 * 问的人要读完六行散文才看得到自己要的数字。
 */

/* ---------------- 答案正文：markdown 极简渲染 ----------------
 *
 * 模型出的答案是 markdown，此前整段按纯文本渲染，于是弹窗里直接出现
 * `| BANK_TRANSFER | 对公转账 | 3,391 |` 和 `**需要注意的两点**`。
 * 这里只认四种最常出现的记法，其余一律按原文走 —— 认不出的东西保持原样，
 * 比猜错了改写它安全。不引第三方依赖，也绝不走 dangerouslySetInnerHTML。 */

const TABLE_LINE = /^\s*\|.*\|\s*$/
const TABLE_RULE = /^\s*\|[\s:|-]+\|\s*$/

/** 拆一行 `| a | b |` 成单元格 */
function cells(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim())
}

/** `**粗体**` 与 `` `代码` `` —— 只做这两种行内记法 */
function inline(text: string, keyPrefix: string): React.ReactNode[] {
  const out: React.ReactNode[] = []
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    if (m[1] !== undefined) out.push(<strong key={`${keyPrefix}b${i}`}>{m[1]}</strong>)
    else out.push(<code key={`${keyPrefix}c${i}`}>{m[2]}</code>)
    last = m.index + m[0].length
    i += 1
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

type Block = { kind: 'text' | 'table'; node: React.ReactNode }

function parseAnswer(text: string): Block[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const out: Block[] = []
  let para: string[] = []
  let list: string[] = []

  const flushPara = () => {
    if (!para.length) return
    const body = para.join('\n')
    out.push({ kind: 'text', node: <p key={`p${out.length}`}>{inline(body, `p${out.length}`)}</p> })
    para = []
  }
  const flushList = () => {
    if (!list.length) return
    const items = list
    out.push({
      kind: 'text',
      node: (
        <ol key={`l${out.length}`} className="answer-list">
          {items.map((it, i) => <li key={i}>{inline(it, `l${out.length}i${i}`)}</li>)}
        </ol>
      ),
    })
    list = []
  }
  const flush = () => { flushPara(); flushList() }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]

    /* 表格：连续的 | 行，且第二行是 |---|---| 分隔行。两个条件缺一个就不当表格 ——
       正文里偶尔出现一个竖线不该把后面几段吃进去。 */
    if (TABLE_LINE.test(line) && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      flush()
      const head = cells(line)
      const body: string[][] = []
      let j = i + 2
      while (j < lines.length && TABLE_LINE.test(lines[j]) && !TABLE_RULE.test(lines[j])) {
        body.push(cells(lines[j]))
        j += 1
      }
      out.push({ kind: 'table', node: (
        <div key={`t${out.length}`} className="answer-table">
          <table>
            <thead><tr>{head.map((c, ci) => <th key={ci}>{inline(c, `th${ci}`)}</th>)}</tr></thead>
            <tbody>
              {body.map((row, ri) => (
                <tr key={ri}>{row.map((c, ci) => <td key={ci}>{inline(c, `td${ri}-${ci}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) })
      i = j - 1
      continue
    }

    /* 有序列表：`1. xxx` / `2. xxx`。模型的「需要注意的两点」就长这样。 */
    const li = line.match(/^\s*\d+[.、]\s+(.*)$/)
    if (li) { flushPara(); list.push(li[1]); continue }

    if (!line.trim()) { flush(); continue }
    flushList()
    para.push(line)
  }
  flush()
  return out
}

/* ---------------- 结果详情正文 ---------------- */

export interface ResultDetailProps {
  /** 这次问的是什么。放最前 —— 下面所有东西都是围着它排的。 */
  question?: string
  questionNote?: string
  statusLabel?: string
  wait?: boolean
  /** 模型给的答案原文（直查为空） */
  answer?: string
  /** 已脱敏结果表 */
  columns?: string[]
  rows?: unknown[][]
  /** 结果表标注：共 N 行 · 仅前 N 行 · 已脱敏某列 */
  cap?: string
  /** 溯源第一格：数据源 / 执行时间 / 耗时 / 模型 */
  facts?: string[]
  /** 溯源第二格：返回行数 / 耗时 / 扫描估算 */
  overview?: [string, string][]
  /** 溯源第三格：命中表 / 命中指标 / 护栏规则 / Token / 成本 */
  auditRows?: [string, string, string][]
  sql?: string
  /** 溯源区一句状态说明：只在**没有结果行可看**时出现（审计不保存结果行、
   *  回放关着）—— 这时溯源就是弹窗的全部内容，得说清为什么只剩它。 */
  traceNote?: string
  /** 没有任何结果时的空态（任务还没跑完 / 被拦下） */
  empty?: { title: string; text: string } | null
}

export function ResultDetail({
  question, questionNote, statusLabel, wait,
  answer, columns, rows, cap,
  facts, overview, auditRows, sql, traceNote, empty,
}: ResultDetailProps) {
  const [copyLabel, setCopyLabel] = useState('复制')
  const copy = async () => {
    try { await navigator.clipboard.writeText(sql ?? '') } catch { /* 剪贴板不可用就只改按钮文案 */ }
    setCopyLabel('已复制')
    window.setTimeout(() => setCopyLabel('复制'), 1200)
  }

  const [full, setFull] = useState(false)
  const hasRows = (rows?.length ?? 0) > 0
  const hasAnswer = Boolean(answer && answer.trim())

  /* 答案里的**文字一句都不折**：那两条「用 payments 算不出真实成功率」式的
     口径提醒就长在这段散文里，折起来等于把正确性警告藏了。
     默认折起的只有答案**重述结果的那张 markdown 表** —— 模型几乎总要把结果
     再抄一遍成表格，而那份数据上面「结果数据」已经完整显示过；两张表叠在一屏
     里正是这次要改掉的乱。上面没有结果表时（看不到结果行的那些记录），答案里
     这张就是唯一的一张，那就不折。
     按块折而不是按高度裁 —— 裁高度会把表切掉半截，看起来像渲染坏了。 */
  const blocks = hasAnswer ? parseAnswer(answer as string) : []
  const foldTables = hasRows && blocks.some(b => b.kind === 'table')
  const shown = !foldTables || full ? blocks : blocks.filter(b => b.kind !== 'table')

  const hasFacts = (facts?.length ?? 0) > 0
  const hasOverview = (overview?.length ?? 0) > 0
  const hasAudit = (auditRows?.length ?? 0) > 0
  const hasSql = Boolean(sql)
  const hasTrace = hasFacts || hasOverview || hasAudit || hasSql
  /* 上面什么都没有的时候（回放关着、结果行看不到），溯源就是这个弹窗的
     全部内容，再默认折起来就是一个空弹窗。 */
  const traceOpen = !hasRows && !hasAnswer

  return (
    <div className="result-detail">
      {question && (
        <div className="result-ask">
          <div>
            <span className="result-ask-label">提问</span>
            <h4>{question}</h4>
            {questionNote && <p>{questionNote}</p>}
          </div>
          {statusLabel && <span className={`status ${wait ? 'wait' : ''}`}>{statusLabel}</span>}
        </div>
      )}

      {/* 1 —— 结果数据。提问问的就是这张表，它必须在第一屏。 */}
      {hasRows && (
        <section className="result-sec">
          <div className="result-sec-head">
            <h5>结果数据</h5>
            {cap && <span>{cap}</span>}
          </div>
          <div className="result-rows-scroll">
            <table>
              <thead><tr>{(columns ?? []).map((c, ci) => <th key={ci}>{c}</th>)}</tr></thead>
              <tbody>
                {(rows ?? []).map((row, ri) => (
                  <tr key={ri}>{row.map((v, vi) => <td key={vi}>{v === null || v === undefined ? '—' : String(v)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* 2 —— 答案说明。结论与口径都在这段模型原文里，排在数据之后。 */}
      {hasAnswer && (
        <section className="result-sec">
          <div className="result-sec-head"><h5>答案说明</h5></div>
          <div className="result-answer">{shown.map(b => b.node)}</div>
          {foldTables && (
            <button type="button" className="result-more" onClick={() => setFull(v => !v)}>
              {full ? '收起答案中的表格' : '展开答案中的表格'}
            </button>
          )}
        </section>
      )}

      {empty && !hasRows && !hasAnswer && (
        <div className="task-detail-empty">
          <i>…</i>
          <strong>{empty.title}</strong>
          <p>{empty.text}</p>
        </div>
      )}

      {/* 3 —— 溯源。默认折起，需要核对的人才展开。 */}
      {hasTrace && (
        <section className="result-sec result-trace">
          <div className="result-sec-head">
            <h5>溯源</h5>
            {traceNote && !hasRows && <span>{traceNote}</span>}
          </div>

          {(hasFacts || hasOverview) && (
            <details open={traceOpen}>
              <summary>执行事实<span>数据源 · 耗时 · 扫描量</span></summary>
              <div className="result-fold">
                {hasFacts && (
                  <div className="result-facts">
                    {(facts ?? []).map((f, i) => <span key={i}>{f}</span>)}
                  </div>
                )}
                {hasOverview && (
                  <div className="result-metrics">
                    {(overview ?? []).map(([label, value]) => (
                      <div key={label}><span>{label}</span><strong>{value}</strong></div>
                    ))}
                  </div>
                )}
              </div>
            </details>
          )}

          {hasAudit && (
            <details>
              <summary>可核对信息<span>命中表 · 护栏 · 用量</span></summary>
              <div className="result-fold">
                <div className="result-audit">
                  <table>
                    <tbody>
                      {(auditRows ?? []).map(row => (
                        <tr key={row[0]}><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </details>
          )}

          {hasSql && (
            <details>
              <summary>原生 SQL<span>READ ONLY</span>
                <button type="button" className="result-copy"
                        onClick={e => { e.preventDefault(); copy() }}>{copyLabel}</button>
              </summary>
              <div className="result-fold"><pre className="result-sql">{sql}</pre></div>
            </details>
          )}
        </section>
      )}
    </div>
  )
}
