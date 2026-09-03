import { PageHeader } from '../components/AppShell'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  askQuestion,
  fetchReplay,
  fetchSources,
  fetchTasks,
  resumeTask,
  type Replay,
  type Task,
  type TasksResult,
} from '../api'
import {
  ClarificationModal,
  CreateTaskModal,
  ModalShell,
  TaskReasonModal,
  TaskResultModal,
  type CreateTaskPayload,
  type TaskDetailView,
  type TaskSourceOption,
} from '../components/Modals'
import type { View } from '../types'

/* 结构、类名与文案对齐原型 trusted-data-agent-prototype.html 的 #view-tasks。
   原型里的任务是写死的样例，这里的每一行都来自 /api/tasks；
   原型有、后端还没有的字段（风险等级、运行中状态、缺少条件项数）一律占位 "—"，
   不拿模板值冒充真实判定。 */

function fmtClock(ts: string): string {
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return sameDay ? time : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`
}

function fmtFull(ts: string): string {
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}S` : `${Math.round(ms)}MS`
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

type StatusFilter = 'all' | 'interrupted' | 'rejected' | 'done'

const FILTER_ORDER: StatusFilter[] = ['all', 'interrupted', 'rejected', 'done']
const FILTER_LABEL: Record<StatusFilter, string> = {
  all: '全部状态⌄',
  interrupted: '等待补充⌄',
  rejected: '已拦截⌄',
  done: '已完成⌄',
}

type ModalState =
  | { kind: 'none' }
  | { kind: 'create' }
  | { kind: 'result'; task: Task }
  | { kind: 'reason'; task: Task }
  | { kind: 'clarify'; task: Task }

export function TasksPage({ onNavigate, notify }: {
  onNavigate: (view: View) => void
  notify: (message: string) => void
}) {
  const [result, setResult] = useState<TasksResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [creating, setCreating] = useState(false)
  const [modal, setModal] = useState<ModalState>({ kind: 'none' })
  const [replay, setReplay] = useState<Replay | null>(null)
  const [replayLoading, setReplayLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [filterOpen, setFilterOpen] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [sources, setSources] = useState<TaskSourceOption[]>([])

  const load = useCallback(() => {
    fetchTasks()
      .then(value => { setResult(value); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [])

  useEffect(load, [load])

  useEffect(() => {
    fetchSources()
      .then(list => setSources(list.items.map(item => ({
        id: item.id,
        name: `${item.name} · ${item.type}`,
        role: `${(item.env || 'read').toUpperCase()}-RO`,
      }))))
      .catch(() => setSources([]))
  }, [])

  const items = useMemo(() => (result?.status === 'ok' ? result.items : []), [result])

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

  const visible = useMemo(() => {
    const key = keyword.trim().toLowerCase()
    return items.filter(task => {
      if (statusFilter !== 'all' && task.status !== statusFilter) return false
      if (!key) return true
      return `${task.question ?? ''} ${task.thread_id} ${task.trace_id}`.toLowerCase().includes(key)
    })
  }, [items, statusFilter, keyword])

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
      const response = await askQuestion(payload.goal, payload.sourceId)
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
          <button className="primary" onClick={() => setModal({ kind: 'create' })}>＋ 创建任务</button>
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

      {result?.status === 'need_login' && (
        <section className="card notice-card">
          <h3>任务列表需要登录</h3>
          <p>{result.detail}</p>
          <p>
            这不是懒得做匿名支持：中断的任务里带着发起人问过的问题原文，
            列出来就等于任何人都能看到、并续跑别人的任务。
            所以列表只对已登录用户开放，且<b>只列自己发起的</b>。
          </p>
        </section>
      )}

      {result?.status === 'ok' && (
        <div className="card task-card" id="taskRowsCard">
          <div className="card-head">
            <div>
              <strong>查询任务</strong>
              <p>每个任务拥有独立状态、执行轨迹和审计记录。账号 {result.user} · 共 {items.length} 条。</p>
            </div>
            <div className="card-actions">
              <button
                className={`ghost ${statusFilter === 'all' ? '' : 'on'}`}
                onClick={() => setStatusFilter(current => FILTER_ORDER[(FILTER_ORDER.indexOf(current) + 1) % FILTER_ORDER.length])}
              >
                {FILTER_LABEL[statusFilter]}
              </button>
              <button className={`ghost ${filterOpen ? 'on' : ''}`} onClick={() => { setFilterOpen(open => !open); if (filterOpen) setKeyword('') }}>筛选</button>
            </div>
          </div>

          {filterOpen && (
            <div className="task-filter">
              <input
                autoFocus
                placeholder="按问题原文、线程号或 trace 过滤…"
                value={keyword}
                onChange={event => setKeyword(event.target.value)}
              />
            </div>
          )}

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
                  <div className="task-meta"><span>风险</span><strong>—</strong></div>
                  <div className="task-meta"><span>原因</span><strong>GUARD</strong></div>
                </>
              ) : (
                <>
                  <div className="task-meta"><span>风险</span><strong>—</strong></div>
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

          {visible.length === 0 && (
            <div className="task-empty">
              <strong>{items.length ? '当前筛选条件下没有任务。' : '这个账号名下还没有执行记录。'}</strong>
              <span>
                {items.length
                  ? '换个状态或清空筛选词再看；列表只包含当前账号发起的线程。'
                  : '任务由提问产生 —— 登录后到查询 Agent 问一次，或在这里创建任务，这里就会出现对应的线程。历史记录若是匿名发起的，不会归到任何账号名下。'}
              </span>
            </div>
          )}
        </div>
      )}

      {result?.status === 'ok' && (
        <div className="tasks-foot">
          <button className="ghost" onClick={load}>刷新</button>
          <button className="ghost" onClick={() => onNavigate('traces')}>去执行追踪看节点明细</button>
        </div>
      )}

      {!result && !error && <section className="card notice-card"><p>读取中…</p></section>}

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
