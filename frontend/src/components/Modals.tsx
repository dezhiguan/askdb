import { useEffect, useMemo, useRef, useState } from 'react'
import type { ModalName } from '../types'
import { ResultDetail } from './ResultDetail'

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
    <div className="modal modal-sheet task-create-modal" role="dialog" aria-modal="true" aria-labelledby="createTaskTitle">
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

/* ---------------- 筛选查询任务 ---------------- */

/** 列表的高级筛选条件。'all' 是"这一维不筛"的哨兵 —— 用空串当哨兵的话，
 *  "匿名发起人"（user 就是空串）就永远选不中。 */
export interface TaskFilters {
  source: string
  risk: string
  user: string
  since: string
}

export const EMPTY_TASK_FILTERS: TaskFilters = { source: 'all', risk: 'all', user: 'all', since: 'all' }

/* ---------------- 任务结果 / 任务原因 ---------------- */

export interface TaskResultView {
  /** 没有结果行可看时，溯源区那一句「为什么只剩它」。 */
  traceNote: string
  overview: [string, string][]
  rows: [string, string, string][]
  sql: string
  /** 本次查询的最终答案（agent 链路有；直查为空）。来自 /api/result。 */
  answer?: string
  /** 已脱敏结果表：列名 + 结果行（前若干行）。来自 /api/result。 */
  resultColumns?: string[]
  resultRows?: unknown[][]
  /** 结果表标注：共 N 行 / 仅前 N 行 / 已脱敏某列。 */
  resultNote?: string
}

export interface TaskReasonView {
  category: string
  node: string
  detail: string
  policy: string
  nextStep: string
  /** 这一档**当前这个人**能做的那一个动作。
   *
   *  它是按「状态 × 权限 × 归属」三者算出来的，不只看状态：同一条等待复核的
   *  任务，系统管理员看到的是「采信 / 打回」，发起人看到的是「等待复核」。
   *  算在一处（TasksPage.buildDetail），这里只负责显示 —— 判定散到弹窗里，
   *  就会出现按钮亮着、点下去 403。
   *
   *    clarify  补充条件后在同一条线程重跑（等待补充 / 可续跑，仅发起人）
   *    approve  放行或驳回这条申请（等待审批，仅系统管理员且非本人）
   *    redeem   凭已批准的票重跑（等待审批且已批准，仅发起人）
   *    review   采信或打回这个数字（等待复核，仅系统管理员且非本人）
   *    ops      标记执行期故障已处置（等待运维，仅运维/系统管理员）
   *    revise   换个问法，**仍在同一条线程上**（护栏拦下、复核打回、故障已恢复）
   *    none     此刻没有这个人能做的事 */
  action: 'clarify' | 'approve' | 'redeem' | 'review' | 'ops' | 'revise' | 'none'
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
  const r = detail.result
  return (
    <div className="modal modal-sheet task-detail-modal" role="dialog" aria-modal="true" aria-labelledby="taskResultTitle">
      <div className="modal-head">
        <div>
          <div className="eyebrow">{detail.id} · {detail.statusLabel.toUpperCase()}</div>
          <h3 id="taskResultTitle">{r ? '任务结果详情' : '任务状态详情'}</h3>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭任务结果">×</button>
      </div>
      <div className="modal-body">
        {loading && (
          <div className="task-detail-empty">
            <i>…</i><strong>正在读取执行记录</strong>
            <p>结果来自审计回放，读取完成前不会先显示任何数字。</p>
          </div>
        )}
        {!loading && (
          <ResultDetail
            question={detail.question}
            questionNote={detail.description}
            statusLabel={detail.statusLabel}
            wait={detail.wait}
            answer={r?.answer}
            columns={r?.resultColumns}
            rows={r?.resultRows}
            cap={r?.resultNote}
            facts={[`数据源 · ${detail.source}`, `执行时间 · ${detail.executedAt}`, `耗时 · ${detail.duration}`]}
            overview={r?.overview}
            auditRows={r?.rows}
            sql={r?.sql}
            traceNote={r?.traceNote}
            empty={r ? null : { title: detail.emptyTitle, text: detail.emptyText }}
          />
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
    <div className="modal modal-sheet task-detail-modal" role="dialog" aria-modal="true" aria-labelledby="taskReasonTitle">
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

/* ---------------- 补充信息（clarify 节点的出口） ---------------- */

/** 补充条件的长度上限，与服务端 ResumeRequest.clarification 的 max_length 对齐。
 *
 *  两处都要有：这里管的是"打字时就知道超了"，那边管的是"绕过界面也超不了"。
 *  数值写死在两边而不是由接口下发 —— 一个 500 不值得多一次往返，但**改的时候
 *  必须一起改**，所以两边注释互相点名。
 *
 *  为什么是 500 而不是几千：这是一句补充条件，不是第二个问题。放大了它就会被
 *  当成对话框用，而这套系统没有多轮上下文 —— 那条路的终点是模型假装"沿用上一轮
 *  口径"再编一个答案出来。 */
const CLARIFY_MAX = 500

/** 任务需要补充信息时的弹窗。
 *
 *  **2026-09-11 之前这一页是假的**，值得写下来免得有人照着它再做一个：四个
 *  选项组（时间范围/退款口径/统计维度/数据源）是从原型里抄来的写死样例，与
 *  用户手上那条任务毫无关系；更要命的是 onConfirm(preview) 传出去的内容在
 *  TasksPage 里被丢掉，resumeTask 只发 thread_id —— 人填的东西 100% 蒸发。
 *
 *  现在它是一个自由文本框，理由是**这里没有"可选项"这种东西**：agent 停下来
 *  的原因各不相同（指代不明、缺时间范围、口径有歧义、根本没产出 SQL），
 *  能穷举的选项集合不存在。给几个猜的选项，只会让人在里面挑一个最不错的，
 *  而他真正想说的那句话没地方写。
 *
 *  `hints` 是 agent 自己给出的那几句（error_hint / review_why / next_actor）——
 *  它们是这个框里唯一有信息量的引导，让人自己猜"要补什么"，这个框就没人填。 */
export function ClarificationModal({ taskId, question, hints = [], mode = 'clarify',
                                    busy, onClose, onConfirm }: {
  taskId: string
  question: string
  /** agent 停下来时给出的原因/建议。空数组时不渲染这一块，不编一句占位的话。 */
  hints?: string[]
  /** clarify = 原问题不变、补一个条件；revise = 直接改写问题本身。
   *
   *  **两种都在这个弹窗里做完，都接在原线程上。** 2026-09-12 之前 revise 是
   *  `onNavigate('query')` —— 跳到查询页重来，于是"换个问法"等于开一条新线程，
   *  原来那条永远挂在「已拦截 / 复核未通过」上。队列只进不出，
   *  与这次改造要消灭的形态一模一样。 */
  mode?: 'clarify' | 'revise'
  busy?: boolean
  onClose: () => void
  onConfirm: (payload: { clarification?: string; question?: string }) => void
}) {
  const revise = mode === 'revise'
  const [text, setText] = useState(revise ? question : '')
  const trimmed = text.trim()
  // 空补充 / 原样重发**不允许提交**：服务端拿不到新输入就不会重跑
  // （graph.resume 直接返回 None → 404），按钮却是亮的，点下去只会得到
  // 一句"任务不存在"。
  const ready = trimmed.length > 0 && trimmed.length <= CLARIFY_MAX
    && (!revise || trimmed !== question.trim())
  return (
    <div className="modal clarify-modal" role="dialog" aria-modal="true">
      <div className="modal-head">
        <div>
          <div className="eyebrow">{taskId} · {revise ? 'REVISE' : 'CLARIFY'}</div>
          <h3>{revise ? '换个问法继续' : '补充条件后继续'}</h3>
          <p>在同一条线程上重跑，不是新开一次提问 —— 审计里看得出这是第几次执行。</p>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭">×</button>
      </div>
      <div className="modal-body">
        <div className="clarify-alert">
          <div>
            <i>?</i>
            <span>
              <strong>{question}</strong>
              <small>{revise
                ? '这条已有结论，但换个问法仍是同一条线索。'
                : 'Agent 在这条上停住了，尚未产出可执行的 SQL。'}</small>
            </span>
          </div>
          <span className="status wait">{revise ? 'NEEDS REWRITE' : 'WAITING FOR INPUT'}</span>
        </div>
        {hints.length > 0 && (
          <ul className="clarify-hints">
            {hints.map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        )}
        <div className="clarify-form">
          <label className="clarify-field">
            <span>{revise ? '改写后的问题' : '补充条件'} <code>REQUIRED</code></span>
            <textarea
              rows={4}
              value={text}
              maxLength={CLARIFY_MAX}
              autoFocus
              placeholder={revise
                ? '换一个能在这个库的开放范围内回答的问法'
                : '例如：统计 2026 年 8 月，按数据源分组，只算解析完成的文档'}
              onChange={e => setText(e.target.value)}
            />
          </label>
        </div>
        <div className="clarify-spec">
          <span>{revise ? 'REVISED QUESTION' : 'RESUME SPEC · 将写入任务状态'}</span>
          <strong>{trimmed || '—'}</strong>
          <small>
            {trimmed.length} / {CLARIFY_MAX}
            {revise && trimmed && trimmed === question.trim() && ' · 与原问题相同，改一改再提交'}
          </small>
        </div>
        <div className="modal-actions">
          <button className="ghost" type="button" onClick={onClose}>稍后处理</button>
          <button className="primary" type="button" disabled={busy || !ready}
                  onClick={() => onConfirm(revise ? { question: trimmed }
                                                  : { clarification: trimmed })}>
            {busy ? '执行中…' : revise ? '换个问法重跑' : '补充并继续执行'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ---------------- 处置：复核 / 运维 ---------------- */

/** 复核与运维处置共用的弹窗。
 *
 *  **两件事共用一个组件，但语义不共用**：文案、按钮、结论取值全部由调用方给。
 *  合并的只是"一个判定 + 一句备注 + 两个出口"这个形状 —— 那确实是同一个形状，
 *  各写一遍必然在其中一处漏掉备注的长度上限或禁用态。
 *
 *  备注在**否定那一侧是必填**：打回一个数字、或判一条故障没救，发起人拿到的
 *  唯一解释就是这句话。不强制的话它就会空着，而队列里那条记录从此说不清
 *  为什么被否掉。 */
export function DispositionModal({
  eyebrow, title, subject, facts, affirmLabel, denyLabel, denyNeedsNote,
  busy, onClose, onDecide,
}: {
  eyebrow: string
  title: string
  subject: string
  /** 判定所依据的事实，逐条列出。空数组不渲染 —— 不编占位的话。 */
  facts?: string[]
  affirmLabel: string
  denyLabel: string
  /** 否定一侧是否强制填备注。复核打回、运维判 WONTFIX 都要。 */
  denyNeedsNote?: boolean
  busy?: boolean
  onClose: () => void
  onDecide: (affirm: boolean, note: string) => void
}) {
  const [note, setNote] = useState('')
  const trimmed = note.trim()
  const denyReady = !denyNeedsNote || trimmed.length > 0
  return (
    <div className="modal" role="dialog" aria-modal="true">
      <div className="modal-head">
        <div>
          <div className="eyebrow">{eyebrow}</div>
          <h3>{title}</h3>
          <p>{subject}</p>
        </div>
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭">×</button>
      </div>
      <div className="modal-body">
        {(facts ?? []).length > 0 && (
          <ul className="clarify-hints">
            {(facts ?? []).map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        )}
        <div className="clarify-form">
          <label className="clarify-field">
            <span>备注 <code>{denyNeedsNote ? 'REQUIRED ON DENY' : 'OPTIONAL'}</code></span>
            <textarea
              rows={3}
              value={note}
              maxLength={200}
              placeholder="写清判定依据 —— 这是发起人能拿到的唯一解释"
              onChange={e => setNote(e.target.value)}
            />
          </label>
        </div>
        <div className="modal-actions">
          <button className="ghost" type="button" onClick={onClose}>稍后处理</button>
          <button className="ghost" type="button" disabled={busy || !denyReady}
                  onClick={() => onDecide(false, trimmed)}>{denyLabel}</button>
          <button className="primary" type="button" disabled={busy}
                  onClick={() => onDecide(true, trimmed)}>{affirmLabel}</button>
        </div>
      </div>
    </div>
  )
}
