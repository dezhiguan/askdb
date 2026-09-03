import { useEffect, useMemo, useRef, useState } from 'react'
import type { ModalName } from '../types'

/* 弹窗结构、文案与类名对齐原型 trusted-data-agent-prototype.html：
   createTaskModal / taskResultModal / taskReasonModal / clarificationModal。 */

export function ModalLayer({ active, onClose, notify }: {
  active: ModalName
  onClose: () => void
  notify: (message: string) => void
}) {
  if (!active) return null
  const finish = (message: string) => { onClose(); notify(message) }
  return (
    <div className="modal-backdrop" onMouseDown={event => { if (event.currentTarget === event.target) onClose() }}>
      {active === 'clarification' && (
        <ClarificationModal
          taskId="TASK-0831"
          question="查询退款金额"
          onClose={onClose}
          onConfirm={() => finish('补充信息已写入任务状态 · 已从 INTERRUPT 节点恢复')}
        />
      )}
      {active === 'langfuse' && <FormModal title="接入 Langfuse" description="配置可观测平台；敏感内容默认不上报。" onClose={onClose}>
        <label>Langfuse Host<input defaultValue="https://langfuse.company.internal" /></label>
        <div className="form-grid"><label>Public Key<input defaultValue="pk-lf-••••••" /></label><label>Secret Key / Vault<input type="password" defaultValue="vault://observability/langfuse" /></label></div>
        <div className="rule-row"><span>01</span><div><strong>上报 Trace 元数据</strong><small>耗时、Token、模型、状态和工具名称。</small></div><button className="toggle on"><i /></button></div>
        <div className="modal-actions"><button className="ghost" onClick={onClose}>取消</button><button className="secondary" onClick={() => notify('Langfuse 连接成功 · Trace Schema 兼容')}>测试连接</button><button className="primary" onClick={() => finish('Langfuse 集成已保存 · 隐私上报策略已生效')}>保存集成</button></div>
      </FormModal>}
    </div>
  )
}

function FormModal({ title, description, onClose, children }: { title: string; description: string; onClose: () => void; children: React.ReactNode }) {
  return <div className="modal"><header><div><h3>{title}</h3><p>{description}</p></div><button onClick={onClose}>×</button></header><div className="modal-body">{children}</div></div>
}

/** 弹窗外壳：点遮罩空白处关闭，与原型的 data-close-modal 行为一致。 */
export function ModalShell({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="modal-backdrop" onMouseDown={event => { if (event.currentTarget === event.target) onClose() }}>
      {children}
    </div>
  )
}

/* ---------------- 创建任务 ---------------- */

export interface TaskSourceOption {
  id: string
  name: string
  /** 数据库权限口径，展示在执行预览里 */
  role: string
}

export type TaskExecution = 'execute' | 'sql-only'
export type TaskRisk = 'standard' | 'always-confirm' | 'strict'

export interface CreateTaskPayload {
  name: string
  goal: string
  sourceId: string
  sourceName: string
  execution: TaskExecution
  risk: TaskRisk
  notes: string
}

const RISK_LABELS: Record<TaskRisk, string> = {
  standard: '超阈值确认',
  'always-confirm': '始终确认',
  strict: '严格拦截',
}

export function CreateTaskModal({ sources, defaultSourceId, busy, onClose, onSubmit }: {
  sources: TaskSourceOption[]
  defaultSourceId: string
  busy: boolean
  onClose: () => void
  onSubmit: (payload: CreateTaskPayload) => void
}) {
  const [name, setName] = useState('')
  const [goal, setGoal] = useState('')
  /* 数据源列表是异步到的：先记用户选过什么，没选过就跟随默认值 —— 用派生值而不是
     在 effect 里回写 state，避免加载完成时多跑一轮渲染 */
  const [pickedSourceId, setPickedSourceId] = useState('')
  const [risk, setRisk] = useState<TaskRisk>('standard')
  const [execution, setExecution] = useState<TaskExecution>('execute')
  const [notes, setNotes] = useState('')
  const [invalid, setInvalid] = useState<{ name?: boolean; goal?: boolean }>({})
  const nameRef = useRef<HTMLInputElement>(null)
  const goalRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => { nameRef.current?.focus() }, [])

  const sourceId = pickedSourceId || defaultSourceId
  const source = sources.find(item => item.id === sourceId)

  const flow = useMemo(() => {
    const nodes = ['身份鉴权', 'Schema 检索', 'SQL 只读护栏']
    if (risk === 'always-confirm') nodes.push('人工确认')
    if (execution === 'sql-only') nodes.push('生成 SQL', '等待执行')
    else nodes.push('只读查询', '结果解释')
    return nodes
  }, [risk, execution])

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const next = { name: !name.trim(), goal: !goal.trim() }
    setInvalid(next)
    if (next.name) { nameRef.current?.focus(); return }
    if (next.goal) { goalRef.current?.focus(); return }
    onSubmit({
      name: name.trim().slice(0, 80),
      goal: goal.trim().slice(0, 1000),
      sourceId,
      sourceName: source?.name ?? '',
      execution,
      risk,
      notes: notes.trim().slice(0, 500),
    })
  }

  return (
    <div className="modal task-create-modal" role="dialog" aria-modal="true" aria-labelledby="createTaskTitle">
      <div className="modal-head">
        <div>
          <div className="eyebrow">NEW GOVERNED QUERY TASK</div>
          <h3 id="createTaskTitle">创建问数任务</h3>
          <p>创建一次新的独立查询任务，不会续接任何已有任务或人工介入状态。</p>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭创建任务">×</button>
      </div>
      <div className="modal-body">
        <form onSubmit={submit} noValidate>
          <div className="task-create-layout">
            <div className="task-create-form">
              <div className="form-row">
                <label htmlFor="taskName">任务名称<span className="field-required">REQUIRED</span></label>
                <input
                  id="taskName"
                  ref={nameRef}
                  className={invalid.name ? 'invalid' : ''}
                  aria-invalid={invalid.name || undefined}
                  maxLength={80}
                  autoComplete="off"
                  placeholder="例如：每日支付异常复盘"
                  value={name}
                  onChange={event => { setName(event.target.value); if (event.target.value.trim()) setInvalid(current => ({ ...current, name: false })) }}
                />
                <span className={`field-error ${invalid.name ? 'show' : ''}`}>请输入任务名称</span>
              </div>
              <div className="form-row">
                <label htmlFor="taskGoal">任务目标 / 自然语言查询<span className="field-required">REQUIRED</span></label>
                <textarea
                  id="taskGoal"
                  ref={goalRef}
                  className={invalid.goal ? 'invalid' : ''}
                  aria-invalid={invalid.goal || undefined}
                  maxLength={1000}
                  placeholder="描述需要确认的数据、时间范围和统计口径…"
                  value={goal}
                  onChange={event => { setGoal(event.target.value); if (event.target.value.trim()) setInvalid(current => ({ ...current, goal: false })) }}
                />
                <span className={`field-error ${invalid.goal ? 'show' : ''}`}>请输入任务目标或自然语言查询</span>
              </div>
              <div className="form-grid">
                <div className="form-row">
                  <label htmlFor="taskSource">数据源</label>
                  <select id="taskSource" value={sourceId} onChange={event => setPickedSourceId(event.target.value)}>
                    {sources.length === 0 && <option value="">默认只读数据源</option>}
                    {sources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
                  </select>
                </div>
                <div className="form-row">
                  <label htmlFor="taskRiskPolicy">风险策略 / 人工确认</label>
                  <select id="taskRiskPolicy" value={risk} onChange={event => setRisk(event.target.value as TaskRisk)}>
                    <option value="standard">标准策略 · 超阈值时人工确认</option>
                    <option value="always-confirm">执行前始终需要人工确认</option>
                    <option value="strict">严格策略 · 敏感字段与高成本拦截</option>
                  </select>
                </div>
              </div>
              <div className="form-row">
                <label>执行方式</label>
                <div className="task-choice-grid">
                  <label className="task-choice">
                    <input type="radio" name="taskExecution" value="execute" checked={execution === 'execute'} onChange={() => setExecution('execute')} />
                    <span className="task-choice-copy"><strong>立即执行</strong><small>安全检查通过后，使用只读连接运行查询。</small></span>
                  </label>
                  {/* 后端没有「只生成不执行」的通道，选项按原型保留位置但不可选 —— 不做点了没反应的开关 */}
                  <label className="task-choice">
                    <input type="radio" name="taskExecution" value="sql-only" disabled checked={execution === 'sql-only'} onChange={() => setExecution('sql-only')} />
                    <span className="task-choice-copy"><strong>仅生成 SQL</strong><small>生成并校验 SQL，不连接数据库执行。</small><em>后端暂未开放</em></span>
                  </label>
                </div>
              </div>
              <div className="form-row">
                <label htmlFor="taskNotes">备注 <span style={{ color: 'var(--muted)' }}>OPTIONAL</span></label>
                <textarea
                  id="taskNotes"
                  maxLength={500}
                  style={{ minHeight: 52 }}
                  placeholder="补充背景、期望输出或协作说明…"
                  value={notes}
                  onChange={event => setNotes(event.target.value)}
                />
              </div>
            </div>
            <aside className="task-run-summary" aria-live="polite">
              <div className="task-summary-head">
                <span>EXECUTION PREVIEW</span>
                <strong>本次任务如何运行</strong>
                <small>任务使用独立上下文；执行前重新校验身份、权限与 Schema。</small>
              </div>
              <div className="task-summary-facts">
                <div className="task-summary-fact"><span>数据源</span><strong>{source?.name ?? '默认只读数据源'}</strong></div>
                <div className="task-summary-fact"><span>数据库权限</span><strong>{source ? `${source.role} · MASKED` : '只读 · MASKED'}</strong></div>
                <div className="task-summary-fact"><span>执行方式</span><strong>{execution === 'sql-only' ? '仅生成 SQL' : '立即执行'}</strong></div>
                <div className="task-summary-fact"><span>人工策略</span><strong>{RISK_LABELS[risk]}</strong></div>
              </div>
              <div className="task-summary-section">
                <strong>预计执行节点</strong>
                <div className="task-summary-flow">
                  {flow.map(node => <span className="task-summary-node" key={node}>{node}</span>)}
                </div>
                <p className="task-summary-note">所有 SQL 均经过 AST 只读检查、成本限制与字段脱敏；新任务不会复用人工介入任务的 checkpoint。</p>
              </div>
            </aside>
          </div>
          <div className="modal-actions">
            <button className="ghost" type="button" onClick={onClose}>取消</button>
            <button className="primary" type="submit" disabled={busy}>{busy ? '创建中…' : '创建任务'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ---------------- 任务结果 / 任务原因 ---------------- */

export interface TaskResultView {
  conclusion: string
  note: string
  overview: [string, string][]
  rows: [string, string, string][]
  sql: string
}

export interface TaskReasonView {
  category: string
  node: string
  detail: string
  policy: string
  nextStep: string
  action: 'clarify' | 'revise' | 'none'
  actionLabel: string
}

export interface TaskDetailView {
  id: string
  statusLabel: string
  /** true 走 .status.wait 琥珀色（原型：非 completed/running 一律 wait） */
  wait: boolean
  question: string
  description: string
  source: string
  executedAt: string
  duration: string
  traceId: string
  result: TaskResultView | null
  reason: TaskReasonView | null
  /** 没有结果时的空态文案，照原型按状态拼 */
  emptyTitle: string
  emptyText: string
}

export function TaskResultModal({ detail, loading, onClose, onViewTrace }: {
  detail: TaskDetailView
  loading: boolean
  onClose: () => void
  onViewTrace: () => void
}) {
  const [copyLabel, setCopyLabel] = useState('复制 SQL')
  const copy = async () => {
    try { await navigator.clipboard.writeText(detail.result?.sql ?? '') } catch { /* 剪贴板不可用就只改按钮文案 */ }
    setCopyLabel('已复制')
    window.setTimeout(() => setCopyLabel('复制 SQL'), 1200)
  }
  return (
    <div className="modal task-detail-modal" role="dialog" aria-modal="true" aria-labelledby="taskResultTitle">
      <div className="modal-head">
        <div>
          <div className="eyebrow">{detail.id} · {detail.statusLabel.toUpperCase()}</div>
          <h3 id="taskResultTitle">{detail.result ? '任务结果详情' : '任务状态详情'}</h3>
          <p>结果只保留摘要、原生 SQL 与可验证执行信息。</p>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭任务结果">×</button>
      </div>
      <div className="modal-body">
        <div className="task-detail-intro">
          <div><h4>{detail.question}</h4><p>{detail.description}</p></div>
          <span className={`status ${detail.wait ? 'wait' : ''}`}>{detail.statusLabel}</span>
        </div>
        <div className="task-detail-meta">
          <span>数据源 · {detail.source}</span>
          <span>执行时间 · {detail.executedAt}</span>
          <span>耗时 · {detail.duration}</span>
        </div>
        {loading && <div className="task-detail-empty"><i>…</i><strong>正在读取执行记录</strong><p>结果来自审计回放，读取完成前不会先显示任何数字。</p></div>}
        {!loading && detail.result && (
          <div>
            <div className="task-conclusion">
              <span>KEY FINDING</span>
              <strong>{detail.result.conclusion}</strong>
              <p>{detail.result.note}</p>
            </div>
            <div className="task-result-overview">
              {detail.result.overview.map(([label, value]) => (
                <div className="task-result-metric" key={label}><span>{label}</span><strong>{value}</strong></div>
              ))}
            </div>
            <div className="task-result-table">
              <table>
                <thead><tr><th>指标 / 维度</th><th>结果</th><th>说明</th></tr></thead>
                <tbody>
                  {detail.result.rows.map(row => (
                    <tr key={row[0]}><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="task-sql-block">
              <div className="task-sql-head"><span>原生 SQL · READ ONLY</span><button type="button" onClick={copy}>{copyLabel}</button></div>
              <pre>{detail.result.sql}</pre>
            </div>
          </div>
        )}
        {!loading && !detail.result && (
          <div className="task-detail-empty">
            <i>…</i>
            <strong>{detail.emptyTitle}</strong>
            <p>{detail.emptyText}</p>
          </div>
        )}
        <div className="modal-actions">
          <button className="ghost" type="button" onClick={onClose}>关闭</button>
          <button className="secondary" type="button" disabled={!detail.traceId} onClick={onViewTrace}>查看执行轨迹</button>
        </div>
      </div>
    </div>
  )
}

export function TaskReasonModal({ detail, busy, onClose, onViewTrace, onAction }: {
  detail: TaskDetailView
  busy: boolean
  onClose: () => void
  onViewTrace: () => void
  onAction: () => void
}) {
  const reason = detail.reason
  return (
    <div className="modal task-detail-modal" role="dialog" aria-modal="true" aria-labelledby="taskReasonTitle">
      <div className="modal-head">
        <div>
          <div className="eyebrow">{detail.id} · {detail.statusLabel.toUpperCase()}</div>
          <h3 id="taskReasonTitle">任务未继续执行</h3>
          <p>这里说明暂停或拦截原因；恢复动作会沿用已有任务状态，不会创建新对话。</p>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭任务原因">×</button>
      </div>
      <div className="modal-body">
        <div className="task-detail-intro">
          <div><h4>{detail.question}</h4><p>{detail.description}</p></div>
          <span className={`status ${detail.wait ? 'wait' : ''}`}>{detail.statusLabel}</span>
        </div>
        <div className="task-reason-card">
          <div className="task-reason-row"><span>原因分类</span><strong>{reason?.category ?? '—'}</strong></div>
          <div className="task-reason-row"><span>触发节点</span><strong>{reason?.node ?? '—'}</strong></div>
          <div className="task-reason-row"><span>具体说明</span><strong>{reason?.detail ?? '—'}</strong></div>
          <div className="task-reason-row"><span>数据源 / 策略</span><strong>{reason?.policy ?? '—'}</strong></div>
        </div>
        <div className="task-next-step"><span>NEXT SAFE ACTION</span><strong>{reason?.nextStep ?? '—'}</strong></div>
        <div className="modal-actions">
          <button className="ghost" type="button" onClick={onClose}>稍后处理</button>
          <button className="secondary" type="button" disabled={!detail.traceId} onClick={onViewTrace}>查看轨迹</button>
          <button className="primary" type="button" disabled={busy || !reason || reason.action === 'none'} onClick={onAction}>
            {busy ? '处理中…' : reason?.actionLabel ?? '处理并继续'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ---------------- 补充信息（LangGraph INTERRUPT） ---------------- */

const CLARIFY_FIELDS = [
  {
    key: 'period' as const,
    label: '时间范围',
    tag: 'REQUIRED',
    cols: 'three',
    options: [
      { value: '今天', note: '00:00 至今' },
      { value: '昨天', note: '完整自然日' },
      { value: '最近 7 天', note: '包含今天' },
    ],
  },
  {
    key: 'metric' as const,
    label: '退款口径',
    tag: 'AFFECTS RESULT',
    cols: 'two',
    options: [
      { value: '成功退款金额', note: '认证口径 v3.2' },
      { value: '退款申请金额', note: '包含处理中申请' },
    ],
  },
  {
    key: 'group' as const,
    label: '统计维度',
    tag: 'REQUIRED',
    cols: 'three',
    options: [
      { value: '仅汇总', note: '返回一个总数' },
      { value: '按支付渠道', note: '支付宝 / 微信等' },
      { value: '按退款原因', note: '查看原因分布' },
    ],
  },
  {
    key: 'source' as const,
    label: '数据源',
    tag: 'AUTHORIZED',
    cols: 'two',
    options: [
      { value: '财务只读库', note: '推荐 · 认证退款口径' },
      { value: '订单中心只读镜像', note: '仅订单侧退款状态' },
    ],
  },
]

export function ClarificationModal({ taskId, question, busy, onClose, onConfirm }: {
  taskId: string
  question: string
  busy?: boolean
  onClose: () => void
  onConfirm: (spec: string) => void
}) {
  const [values, setValues] = useState({
    period: '最近 7 天',
    metric: '成功退款金额',
    group: '按支付渠道',
    source: '财务只读库',
  })
  const preview = useMemo(
    () => [values.period, values.metric, values.group, values.source].join(' · '),
    [values],
  )
  return (
    <div className="modal clarify-modal" role="dialog" aria-modal="true">
      <div className="modal-head">
        <div>
          <div className="eyebrow">{taskId} · LANGGRAPH INTERRUPT</div>
          <h3>任务需要补充信息</h3>
          <p>这不是对话。补充内容将写入当前任务状态，然后从暂停节点继续执行。</p>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭补充信息">×</button>
      </div>
      <div className="modal-body">
        <div className="clarify-alert">
          <div>
            <i>?</i>
            <span>
              <strong>问题“{question}”存在关键歧义</strong>
              <small>Agent 已暂停，尚未生成或执行任何 SQL。</small>
            </span>
          </div>
          <span className="status wait">WAITING FOR INPUT</span>
        </div>
        <div className="clarify-progress">
          <div className="clarify-step done">01 理解问题</div>
          <div className="clarify-step done">02 检查完整性</div>
          <div className="clarify-step active">03 等待补充</div>
          <div className="clarify-step">04 生成 SQL</div>
          <div className="clarify-step">05 安全执行</div>
        </div>
        <div className="clarify-form">
          {CLARIFY_FIELDS.map(field => (
            <div className="clarify-field" key={field.key}>
              <span>{field.label} <code>{field.tag}</code></span>
              <div className={`clarify-options ${field.cols}`}>
                {field.options.map(option => (
                  <label className="clarify-choice" key={option.value}>
                    <input
                      type="radio"
                      name={`clarify-${field.key}`}
                      checked={values[field.key] === option.value}
                      onChange={() => setValues(current => ({ ...current, [field.key]: option.value } as typeof current))}
                    />
                    <div><strong>{option.value}</strong><small>{option.note}</small></div>
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="clarify-spec">
          <span>RESUME SPEC · 结构化任务参数</span>
          <strong>{preview}</strong>
          <small>补充参数会进入任务状态，不保存为对话历史，也不会影响其他任务。</small>
        </div>
        <div className="modal-actions">
          <button className="ghost" type="button" onClick={onClose}>稍后处理</button>
          <button className="primary" type="button" disabled={busy} onClick={() => onConfirm(preview)}>
            {busy ? '恢复中…' : '确认并继续执行'}
          </button>
        </div>
      </div>
    </div>
  )
}
