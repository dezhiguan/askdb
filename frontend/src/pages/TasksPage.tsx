import { PageHeader } from '../components/AppShell'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  askQuestion,
  fetchReplay,
  fetchSources,
  fetchTasks,
  resumeTask,
  type Replay,
  type Task,
  type TasksResult,
 type Me,
} from '../api'
import {
  ClarificationModal,
  CreateTaskModal,
  EMPTY_TASK_FILTERS,
  ModalShell,
  TaskFilterModal,
  TaskReasonModal,
  TaskResultModal,
  hasTaskFilter,
  type CreateTaskPayload,
  type TaskDetailView,
  type TaskFilters,
  type TaskSourceOption,
} from '../components/Modals'
import type { View } from '../types'
import { ruleTitle } from '../rules'
import { writeGuard } from '../writeGuard'

/* 结构、类名与文案对齐原型 trusted-data-agent-prototype.html 的 #view-tasks。
   原型里的任务是写死的样例，这里的每一行都来自 /api/tasks；
   原型有、后端还没有的字段（风险等级、运行中状态、缺少条件项数）一律占位 "—"，
   不拿模板值冒充真实判定。 */

function fmtClock(ts: string): string {
  /* 空值必须先挡掉。**new Date(null) 不是 NaN，是纪元 0** —— 只判 NaN 的话，
     没有 ts 的老审计记录会被格式化成「1970-01-01 08:00」，即凭空编出一个
     看起来合理的时间。宁可显示占位，也不要显示一个假的。 */
  if (!ts) return '—'
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return sameDay ? time : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`
}

function fmtFull(ts: string): string {
  // 同 fmtClock：new Date(null) 是纪元 0 而不是 NaN，空值必须先挡
  if (!ts) return '—'
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}S` : `${Math.round(ms)}MS`
}

/** 发起时间档。ts 解析不出来时**不放行** —— 选了"今天"却混进一条时间不明的记录，
 *  比少一条更糟：它会被当成今天发生的。 */
function withinSince(ts: string, since: string): boolean {
  if (since === 'all') return true
  const date = new Date(ts)
  if (!ts || Number.isNaN(date.getTime())) return false
  if (since === 'today') return date.toDateString() === new Date().toDateString()
  const days = since === '7d' ? 7 : since === '30d' ? 30 : 0
  if (!days) return true
  return Date.now() - date.getTime() <= days * 86400000
}

const STATUS_LABEL: Record<Task['status'], string> = {
  interrupted: '等待补充',
  rejected: '已拦截',
  done: '已完成',
}

/** 原型：completed / running 走绿色，其余一律 .status.wait */
const STATUS_WAIT: Record<Task['status'], boolean> = {
  interrupted: true,
  rejected: true,
  done: false,
}

const STATE_GLYPH: Record<Task['status'], string> = {
  interrupted: '?',
  rejected: '!',
  done: '✓',
}

/* 状态筛选下拉。原型给出七档，后端目前只落地其中三档（INPUT / DONE / BLOCK）——
   NEW / RUN / APPROVAL 没有对应数据，选中后列表就是空的。这里**不**把它们藏起来：
   下拉是状态口径的说明书，藏掉等于让人以为这三种状态不存在；空列表由空态文案说明。 */
type StatusFilter = 'all' | 'pending' | 'running' | 'interrupted' | 'approval' | 'done' | 'rejected'

const FILTER_ORDER: StatusFilter[] = ['all', 'pending', 'running', 'interrupted', 'approval', 'done', 'rejected']
const FILTER_LABEL: Record<StatusFilter, string> = {
  all: '全部状态',
  pending: '待执行',
  running: '运行中',
  interrupted: '等待补充',
  approval: '等待审批',
  done: '已完成',
  rejected: '已拦截',
}
const FILTER_CODE: Record<StatusFilter, string> = {
  all: 'ALL',
  pending: 'NEW',
  running: 'RUN',
  interrupted: 'INPUT',
  approval: 'APPROVAL',
  done: 'DONE',
  rejected: 'BLOCK',
}

type ModalState =
  | { kind: 'none' }
  | { kind: 'create' }
  | { kind: 'filter' }
  | { kind: 'result'; task: Task }
  | { kind: 'reason'; task: Task }
  | { kind: 'clarify'; task: Task }

export function TasksPage({ onNavigate, notify, me }: {
  onNavigate: (view: View) => void
  notify: (message: string) => void
  me: Me | null
}) {
  const guard = writeGuard(me, '创建任务')
  const [result, setResult] = useState<TasksResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [creating, setCreating] = useState(false)
  const [modal, setModal] = useState<ModalState>({ kind: 'none' })
  const [replay, setReplay] = useState<Replay | null>(null)
  const [replayLoading, setReplayLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [statusOpen, setStatusOpen] = useState(false)
  const [filters, setFilters] = useState<TaskFilters>(EMPTY_TASK_FILTERS)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [sources, setSources] = useState<TaskSourceOption[]>([])
  const statusRef = useRef<HTMLDivElement>(null)
  const statusMenuRef = useRef<HTMLDivElement>(null)
  /* 菜单锚点（视口坐标）。菜单**必须**画到 body 上：任务卡为了让行的圆角不冒出边框
     用了 overflow:hidden，菜单挂在卡片里就会被裁掉 —— 列表被筛空时卡片只有两三行高，
     七档里只看得见四档。 */
  const [statusAt, setStatusAt] = useState<{ top: number; right: number } | null>(null)

  const load = useCallback(() => {
    fetchTasks()
      .then(value => { setResult(value); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [])

  useEffect(load, [load])

  // 展开前先量按钮位置：菜单画在 body 上，只能自己贴回按钮下方
  const openStatusMenu = () => {
    const rect = statusRef.current?.getBoundingClientRect()
    if (rect) setStatusAt({ top: rect.bottom + 5, right: window.innerWidth - rect.right })
  }

  /* 下拉必须点外面能关、Esc 能关：只有按钮自身 toggle 的话，点走到别处它会一直悬在
     卡片上盖住第一行任务。菜单在 body 上，"外面"要同时排除按钮和菜单两棵子树，
     否则 mousedown 先把菜单卸掉，菜单项的 click 根本轮不到触发。
     滚动时直接关掉：锚点是视口坐标，跟着滚会飘到按钮以外的地方去。 */
  useEffect(() => {
    if (!statusOpen) return
    const inside = (node: Node) =>
      Boolean(statusRef.current?.contains(node)) || Boolean(statusMenuRef.current?.contains(node))
    const onDown = (event: MouseEvent) => { if (!inside(event.target as Node)) setStatusOpen(false) }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setStatusOpen(false) }
    const close = () => setStatusOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [statusOpen])

  useEffect(() => {
    fetchSources()
      .then(list => setSources(list.items.map(item => ({
        id: item.id,
        name: `${item.name} · ${item.type}`,
        role: `${(item.env || 'read').toUpperCase()}-RO`,
      }))))
      .catch(() => setSources([]))
  }, [])

  const items = useMemo(() => result?.items ?? [], [result])

  const stats = useMemo(() => {
    const today = new Date().toDateString()
    const interrupted = items.filter(task => task.status === 'interrupted').length
    const rejected = items.filter(task => task.status === 'rejected').length
    const done = items.filter(task => task.status === 'done')
    const doneToday = done.filter(task => new Date(task.ts).toDateString() === today).length
    const settled = done.length + rejected
    const rate = settled ? `成功率 ${((done.length / settled) * 100).toFixed(1)}%` : '暂无收尾记录'
    return { interrupted, rejected, doneToday, rate }
  }, [items])

  const matched = useMemo(() => items.filter(task => {
    if (statusFilter !== 'all' && task.status !== statusFilter) return false
    if (filters.source !== 'all' && (task.source ?? '') !== filters.source) return false
    if (filters.risk !== 'all' && (task.risk ?? '') !== filters.risk) return false
    if (filters.user !== 'all' && (task.user ?? '') !== filters.user) return false
    if (!withinSince(task.ts, filters.since)) return false
    return true
  }), [items, statusFilter, filters])

  /* 下拉的可选值只从**当前列表里真有的**值来：列出一个筛完是空的数据源，
     等于让人自己去撞哪个有数据。 */
  const sourceOptions = useMemo(() => {
    const seen = new Map<string, string>()
    items.forEach(task => {
      const value = task.source ?? ''
      if (!seen.has(value)) seen.set(value, task.source_name || task.source || '（未记录数据源）')
    })
    return Array.from(seen, ([value, label]) => ({ value, label }))
  }, [items])

  const userOptions = useMemo(() => {
    const seen = new Set<string>()
    items.forEach(task => seen.add(task.user ?? ''))
    return Array.from(seen, value => ({ value, label: value || '匿名' }))
  }, [items])

  /* 分页。/api/tasks 一次返回全部线程（统计卡要算全量），所以这里在**客户端**切页，
     与筛选、关键词同一条链路 —— 服务端分页会让上面那四个统计数字失真。
     切页本身也是必须的：这一页曾经一次渲染一千四百多行，DOM 高七万多像素。 */
  const pages = Math.max(Math.ceil(matched.length / pageSize), 1)
  const current = Math.min(page, pages)
  const visible = useMemo(
    () => matched.slice((current - 1) * pageSize, current * pageSize),
    [matched, current, pageSize],
  )

  // 筛选条件一变就回到第一页 —— 停在第 7 页而结果只剩 2 条，会看到一片空白
  useEffect(() => { setPage(1) }, [statusFilter, filters, pageSize])

  /* 结果与原因都来自审计回放：没有回放就说没有，不靠状态推断内容 */
  const openDetail = (task: Task, kind: 'result' | 'reason') => {
    setModal({ kind, task })
    setReplay(null)
    setReplayLoading(true)
    fetchReplay(task.trace_id)
      .then(value => setReplay(value.status === 'ok' ? value.data : null))
      .catch(() => setReplay(null))
      .finally(() => setReplayLoading(false))
  }

  const detail = useMemo<TaskDetailView | null>(() => {
    if (modal.kind !== 'result' && modal.kind !== 'reason' && modal.kind !== 'clarify') return null
    return buildDetail(modal.task, replay)
  }, [modal, replay])

  const resume = async (task: Task) => {
    setBusy(task.thread_id)
    try {
      const response = await resumeTask(task.thread_id)
      if (!response) {
        notify('这个任务已经跑完，或不属于当前账号')
      } else if (response.ok) {
        notify(`已从断点续跑完成 · 新 trace ${response.trace_id}`)
      } else {
        notify(`续跑仍未完成：${response.rejected_by ?? ''} ${response.error ?? ''}`.trim())
      }
      setModal({ kind: 'none' })
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const create = async (payload: CreateTaskPayload) => {
    setCreating(true)
    try {
      const response = await askQuestion(payload.goal, payload.sourceId, undefined, true)
      if (response.ok) {
        notify(`任务已创建并执行 · trace ${response.trace_id}`)
      } else {
        notify(`任务已创建但未执行完：${response.rejected_by ?? ''} ${response.error ?? ''}`.trim())
      }
      setModal({ kind: 'none' })
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setCreating(false)
    }
  }

  const reasonAction = () => {
    if (modal.kind !== 'reason' || !detail?.reason) return
    if (detail.reason.action === 'clarify') { setModal({ kind: 'clarify', task: modal.task }); return }
    if (detail.reason.action === 'revise') { setModal({ kind: 'none' }); onNavigate('query') }
  }

  const viewTrace = () => { setModal({ kind: 'none' }); onNavigate('traces') }

  return (
    <div className="page tasks-page">
      <PageHeader
        title="任务中心"
        description="需要确认、耗时较长或包含复杂分析步骤的查询会自动升级为任务。"
        action={
          <button className="primary" {...guard.props}
            onClick={() => setModal({ kind: 'create' })}>＋ 创建任务</button>
        }
      />

      <div className="stats">
        {/* 后端只记录线程的终态，没有「正在跑」这一维，不拿数字凑 */}
        <div className="stat stat-muted"><span>运行中</span><strong>—</strong><small>后端不跟踪运行中状态</small></div>
        <div className="stat"><span>待处理</span><strong>{stats.interrupted}</strong><small>{stats.interrupted} 补充信息 · 0 审批</small></div>
        <div className="stat"><span>今日完成</span><strong>{stats.doneToday}</strong><small>{stats.rate}</small></div>
        <div className="stat"><span>已拦截</span><strong>{stats.rejected}</strong><small>越权或写入意图</small></div>
      </div>

      {error && <div className="audit-error">读取任务失败：{error}</div>}

      {result && (
        <div className="card task-card" id="taskRowsCard">
          <div className="card-head">
            <div>
              <strong>查询任务</strong>
              <p>每个任务拥有独立状态、执行轨迹和审计记录。{result.user ? `账号 ${result.user}` : '匿名发起'} · 共 {items.length} 条。</p>
            </div>
            <div className="card-actions">
              <div className="status-select" ref={statusRef}>
                <button
                  className={`ghost ${statusFilter === 'all' ? '' : 'on'}`}
                  aria-haspopup="listbox"
                  aria-expanded={statusOpen}
                  onClick={() => { openStatusMenu(); setStatusOpen(open => !open) }}
                >
                  {FILTER_LABEL[statusFilter]}<i className="caret">⌄</i>
                </button>
                {statusOpen && statusAt && createPortal(
                  <div
                    className="task-status-menu"
                    role="listbox"
                    ref={statusMenuRef}
                    style={{ top: statusAt.top, right: statusAt.right }}
                  >
                    {FILTER_ORDER.map(value => (
                      <button
                        key={value}
                        role="option"
                        aria-selected={value === statusFilter}
                        className={value === statusFilter ? 'on' : ''}
                        onClick={() => { setStatusFilter(value); setStatusOpen(false) }}
                      >
                        <span>{FILTER_LABEL[value]}</span><b>{FILTER_CODE[value]}</b>
                      </button>
                    ))}
                  </div>,
                  document.body,
                )}
              </div>
              <button
                className={`ghost ${hasTaskFilter(filters) ? 'on' : ''}`}
                onClick={() => setModal({ kind: 'filter' })}
              >筛选</button>
            </div>
          </div>

          {/* 原型里本地新建的任务挂在这里；本实现的新建任务直接进真实任务流，
              容器保留以对齐结构（:empty 时不占位） */}
          <div className="created-task-list" />

          {visible.map(task => (
            <div className="task-row" key={task.thread_id} data-task-id={task.thread_id}>
              <i className={`task-state ${STATUS_WAIT[task.status] ? 'wait' : ''}`}>{STATE_GLYPH[task.status]}</i>
              <div className="task-main">
                <strong title={task.question ?? ''}>{task.question || '（无问题文本）'}</strong>
                <small>{task.thread_id} · {task.user} · {fmtClock(task.ts)} · 已执行 {task.attempts_on_thread} 次</small>
              </div>
              {task.status === 'interrupted' ? (
                <>
                  <div className="task-meta"><span>缺少条件</span><strong>—</strong></div>
                  <div className="task-meta"><span>当前节点</span><strong>INTERRUPT</strong></div>
                </>
              ) : task.status === 'rejected' ? (
                <>
                  <div className="task-meta"><span>风险</span><strong title={task.risk_why ?? ''}>{task.risk ?? '—'}</strong></div>
                  {/* 原来这一格写死 GUARD —— 接口给的是**具体规则号**，写死等于把
                      "撞了哪条护栏"这个唯一有用的信息抹掉了 */}
                  <div className="task-meta">
                    <span>原因</span>
                    <strong title={ruleTitle(task.rejected_by ?? '')}>{task.rejected_by || 'GUARD'}</strong>
                  </div>
                </>
              ) : (
                <>
                  <div className="task-meta"><span>风险</span><strong title={task.risk_why ?? ''}>{task.risk ?? '—'}</strong></div>
                  <div className="task-meta"><span>耗时</span><strong>{fmtDuration(task.elapsed_ms)}</strong></div>
                </>
              )}
              <div><span className={`status ${STATUS_WAIT[task.status] ? 'wait' : ''}`}>{STATUS_LABEL[task.status]}</span></div>
              {task.status === 'done' ? (
                <button className="ghost task-view-result" onClick={() => openDetail(task, 'result')}>查看结果</button>
              ) : (
                <button
                  className={task.resumable ? 'danger task-view-reason' : 'secondary task-view-reason'}
                  disabled={busy === task.thread_id}
                  onClick={() => openDetail(task, 'reason')}
                >
                  {busy === task.thread_id ? '续跑中…' : '查看原因'}
                </button>
              )}
            </div>
          ))}

          {matched.length === 0 && (
            <div className="task-empty">
              <strong>{items.length ? '当前筛选条件下没有任务。' : '这个账号名下还没有执行记录。'}</strong>
              <span>
                {items.length
                  ? '换个状态，或在筛选里重置数据源、发起人与时间再看；列表只包含当前账号发起的线程。'
                  : '任务由提问产生 —— 登录后到查询 Agent 问一次，或在这里创建任务，这里就会出现对应的线程。历史记录若是匿名发起的，不会归到任何账号名下。'}
              </span>
            </div>
          )}
        </div>
      )}

      {/* 分页条与审计中心同一套结构与类名，两页的操作手感必须一致 */}
      {matched.length > 0 && (
        <div className="audit-pager">
          <span>共 {matched.length} 条 · 第 {current} / {pages} 页</span>
          <span>
            <select value={pageSize} onChange={event => setPageSize(Number(event.target.value))}>
              {[10, 20, 50].map(size => <option key={size} value={size}>每页 {size} 条</option>)}
            </select>
            <button className="ghost" disabled={current <= 1} onClick={() => setPage(p => p - 1)}>‹ 上一页</button>
            <button className="ghost" disabled={current >= pages} onClick={() => setPage(p => p + 1)}>下一页 ›</button>
          </span>
        </div>
      )}

      {!result && !error && <section className="card notice-card"><p>读取中…</p></section>}

      {modal.kind === 'filter' && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <TaskFilterModal
            value={filters}
            sources={sourceOptions}
            users={userOptions}
            onClose={() => setModal({ kind: 'none' })}
            onApply={next => { setFilters(next); setModal({ kind: 'none' }) }}
          />
        </ModalShell>
      )}

      {modal.kind === 'create' && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <CreateTaskModal
            sources={sources}
            defaultSourceId={sources[0]?.id ?? ''}
            busy={creating}
            onClose={() => setModal({ kind: 'none' })}
            onSubmit={create}
          />
        </ModalShell>
      )}

      {modal.kind === 'result' && detail && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <TaskResultModal
            detail={detail}
            loading={replayLoading}
            onClose={() => setModal({ kind: 'none' })}
            onViewTrace={viewTrace}
          />
        </ModalShell>
      )}

      {modal.kind === 'reason' && detail && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <TaskReasonModal
            detail={detail}
            busy={busy === modal.task.thread_id}
            onClose={() => setModal({ kind: 'none' })}
            onViewTrace={viewTrace}
            onAction={reasonAction}
          />
        </ModalShell>
      )}

      {modal.kind === 'clarify' && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <ClarificationModal
            taskId={modal.task.thread_id}
            question={modal.task.question || '（无问题文本）'}
            busy={busy === modal.task.thread_id}
            onClose={() => setModal({ kind: 'none' })}
            onConfirm={() => resume(modal.task)}
          />
        </ModalShell>
      )}
    </div>
  )
}

/** 把 /api/tasks 的一行 + /api/replay 的回放拼成弹窗要的视图对象。 */
function buildDetail(task: Task, replay: Replay | null): TaskDetailView {
  const statusLabel = STATUS_LABEL[task.status]
  const wait = STATUS_WAIT[task.status]
  const duration = fmtDuration(task.elapsed_ms)
  const source = replay && replay.org_id !== null && replay.org_id !== undefined
    ? `org ${replay.org_id} · 只读`
    : '只读数据源'
  const list = (values: string[] | null | undefined) => (values && values.length ? values.join(', ') : '—')

  const sql = replay?.sql_final || replay?.sql_raw || ''
  const result = task.status === 'done' && replay && sql
    ? {
      conclusion: `本次查询返回 ${replay.rows_returned ?? '—'} 行结果`,
      note: '审计只保留执行事实与原生 SQL，不保存结果行；下表是这次执行可核对的信息。',
      overview: [
        ['返回行数', String(replay.rows_returned ?? '—')],
        ['耗时', fmtDuration(replay.elapsed_ms ?? task.elapsed_ms)],
        ['扫描估算', replay.explain_rows === null || replay.explain_rows === undefined ? '—' : String(replay.explain_rows)],
      ] as [string, string][],
      rows: [
        ['命中表', list(replay.tables_hit), '来自审计记录'],
        ['命中指标', list(replay.metrics_hit), '认证口径'],
        ['护栏规则', list(replay.rules_fired), '生成后触发'],
        ['Token 用量', `${replay.tok_in ?? 0} / ${replay.tok_out ?? 0}`, '入 / 出'],
        ['调用成本', replay.cost_cny === null || replay.cost_cny === undefined ? '—' : `¥${replay.cost_cny.toFixed(4)}`, '按模型计价'],
      ] as [string, string, string][],
      sql,
    }
    : null

  const reason = task.status === 'rejected'
    ? {
      category: `护栏拒绝 · ${replay?.rejected_by ?? 'GUARD'}`,
      node: replay?.rejected_by ?? '安全护栏',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '这次调用被护栏拦下，SQL 没有在数据库上执行。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: '改写问题或缩小取数范围后重新发起；被拦下的调用不会留下可续跑的断点。',
      action: 'revise' as const,
      actionLabel: '调整后重新提问',
    }
    : task.status === 'interrupted'
      ? {
        category: '信息不足 · INPUT REQUIRED',
        node: replay?.snapshots?.map(item => (item.next ?? []).join(' / ')).filter(Boolean).slice(-1)[0] || 'INTERRUPT',
        detail: '任务在生成 SQL 前暂停等待补充条件，现场已经写进检查点。',
        policy: `${task.kind} · ${task.role}`,
        nextStep: task.resumable
          ? '补充条件后从断点继续执行；checkpoint 之前已完成的节点不会重跑。'
          : '这条线程已经收尾，没有可续的断点。',
        action: task.resumable ? ('clarify' as const) : ('none' as const),
        actionLabel: task.resumable ? '补充信息并恢复' : '无可续的断点',
      }
      : null

  return {
    id: task.thread_id,
    statusLabel,
    wait,
    question: task.question || '（无问题文本）',
    description: `线程 ${task.thread_id} · 发起人 ${task.user} · 已执行 ${task.attempts_on_thread} 次`,
    source,
    executedAt: fmtFull(task.ts),
    duration,
    traceId: task.trace_id,
    result,
    emptyTitle: task.status === 'done' ? '当前状态暂无结果' : `任务状态为“${statusLabel}”，没有结果`,
    emptyText: task.status === 'done'
      ? '这条记录的审计回放不可读（回放接口未开放或记录已过期），因此不会展示推测或伪造的数据结果。'
      : `任务状态为“${statusLabel}”，系统不会展示推测或伪造的数据结果。`,
    reason,
  }
}
