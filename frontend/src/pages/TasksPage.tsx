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
  type TaskStats,
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

/** 还没读到数据时的统计占位。全 0 而不是隐藏那一排 —— 卡片先在位，
 *  数字随后到位，否则列表出现前整页会先跳一次。 */
const EMPTY_STATS: TaskStats = {
  running: 0, waiting_input: 0, waiting_approval: 0, waiting_review: 0,
  review_returned: 0, needs_operator: 0, interrupted: 0, rejected: 0,
  done: 0, done_today: 0, success_rate: null,
}

const STATUS_LABEL: Record<Task['status'], string> = {
  running: '运行中',
  waiting_review: '等待复核',
  review_returned: '复核未通过',
  interrupted: '可续跑',
  waiting_input: '等待补充',
  waiting_approval: '等待审批',
  needs_operator: '等待运维',
  rejected: '已拦截',
  done: '已完成',
}

/** 原型：completed / running 走绿色，其余一律 .status.wait */
const STATUS_WAIT: Record<Task['status'], boolean> = {
  running: false,
  waiting_review: true,
  review_returned: true,
  interrupted: true,
  waiting_input: true,
  waiting_approval: true,
  needs_operator: true,
  rejected: true,
  done: false,
}

const STATE_GLYPH: Record<Task['status'], string> = {
  running: '·',
  waiting_review: '?',
  review_returned: '!',
  interrupted: '?',
  waiting_input: '?',
  waiting_approval: '!',
  needs_operator: '!',
  rejected: '!',
  done: '✓',
}

/* 状态筛选下拉。七档**现在全部有对应数据**：2026-09-07 起后端按收尾码做确定性
   折算（audit.stage），并落发起记录，运行中与等待审批不再是空档。
   「待执行」是唯一还没有数据的一档 —— askdb 不排队，收到即执行，留在这里是
   为了让状态口径完整可读，选中后由空态文案说明。 */
/* 档位与后端的线程状态**一一对应**（askdb/audit.py 的那九个常量）。
   原来这里还有一档「待执行 / pending」，而没有任何线程会是这个状态 ——
   选中它列表必然是空的，看的人只会以为"这会儿真没有待执行的"。
   筛选搬到服务端之后这种档位更留不得：传过去就是一个非法取值。 */
type StatusFilter = 'all' | 'running' | 'interrupted' | 'waiting_input'
  | 'waiting_approval' | 'waiting_review' | 'review_returned' | 'needs_operator'
  | 'done' | 'rejected'

const FILTER_ORDER: StatusFilter[] = ['all', 'running', 'waiting_input',
  'waiting_approval', 'waiting_review', 'needs_operator', 'interrupted', 'done',
  'review_returned', 'rejected']
const FILTER_LABEL: Record<StatusFilter, string> = {
  all: '全部状态',
  running: '运行中',
  waiting_input: '等待补充',
  waiting_approval: '等待审批',
  waiting_review: '等待复核',
  needs_operator: '等待运维',
  interrupted: '可续跑',
  done: '已完成',
  review_returned: '复核未通过',
  rejected: '已拦截',
}
const FILTER_CODE: Record<StatusFilter, string> = {
  all: 'ALL',
  running: 'RUN',
  waiting_input: 'INPUT',
  waiting_approval: 'APPROVAL',
  waiting_review: 'REVIEW',
  needs_operator: 'OPS',
  interrupted: 'RESUME',
  done: 'DONE',
  review_returned: 'RETURNED',
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
  /* 页码。**换筛选条件的地方一并把它设回 1**，而不是靠一个 useEffect 去追 ——
     追的写法会先按旧页码请求一次、再按第 1 页请求一次，列表跳两下。
     停在第 7 页而筛完只剩 2 条，看到的会是一片空白。 */
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [sources, setSources] = useState<TaskSourceOption[]>([])
  const statusRef = useRef<HTMLDivElement>(null)
  const statusMenuRef = useRef<HTMLDivElement>(null)
  /* 菜单锚点（视口坐标）。菜单**必须**画到 body 上：任务卡为了让行的圆角不冒出边框
     用了 overflow:hidden，菜单挂在卡片里就会被裁掉 —— 列表被筛空时卡片只有两三行高，
     七档里只看得见四档。 */
  const [statusAt, setStatusAt] = useState<{ top: number; right: number } | null>(null)

  /* 筛选、切页都走服务端：/api/tasks 收筛选条件与页码，返回这一页 + 统计 +
     下拉可选值。参数一变就重新取，与"点了刷新"是同一条链路 —— 两条链路会漂。 */
  const load = useCallback(() => {
    fetchTasks({
      page, pageSize,
      status: statusFilter,
      source: filters.source,
      risk: filters.risk,
      user: filters.user,
      since: filters.since,
    })
      .then(value => { setResult(value); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [page, pageSize, statusFilter, filters])

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

  /* 这一页拿到的是**当前页**的行；统计卡、筛选下拉的可选值、两个总数都由
     服务端一并给出（audit.paginate_tasks）。原来这三样都在浏览器里从全量
     列表算，代价是每次打开要把一千四百多条线程发过来，而屏幕上只有十行。

     统计仍然算在**筛选之前**：这四个数讲的是系统当下的处境，跟着筛选变的话，
     筛完「已完成」再看「待处理」永远是 0。下拉可选值同理，跟着收窄就退不回去。 */
  const visible = useMemo(() => result?.items ?? [], [result])
  const stats = result?.stats ?? EMPTY_STATS
  const total = result?.total ?? 0
  const totalAll = result?.total_all ?? 0
  const sourceOptions = result?.sources ?? []
  const userOptions = result?.users ?? []
  // 成功率没有收尾样本时给 null，不是 0% —— 后者是在报一个没发生过的失败
  const rateLabel = stats.success_rate === null
    ? '暂无收尾记录'
    : `成功率 ${stats.success_rate.toFixed(1)}%`

  const pages = Math.max(Math.ceil(total / pageSize), 1)
  const current = Math.min(result?.page ?? page, pages)

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
    return buildDetail(modal.task, replay, result?.user ?? '')
  }, [modal, replay, result])

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
        {/* 2026-09-07 起后端在发起时先落一条记录，「运行中」才有真数字可给。
            它同时也是"跑一半进程没了"那一档 —— 那种线程原来整片从系统里消失。 */}
        <div className="stat"><span>运行中</span><strong>{stats.running}</strong><small>已发起未收尾</small></div>
        {/* 「待处理」= 还等着**某个人**动手的那些。三档分开写：等谁动手不一样，
            合成一个数字就等于让人自己去猜该找谁。审批那格原来写死 0。 */}
        <div className="stat">
          <span>待处理</span>
          <strong>{stats.waiting_input + stats.waiting_approval + stats.waiting_review
                   + stats.needs_operator + stats.interrupted}</strong>
          <small>
            {stats.waiting_input} 补充信息 · {stats.waiting_approval} 审批
            · {stats.waiting_review} 复核 · {stats.needs_operator} 运维
            · {stats.interrupted} 可续跑
          </small>
        </div>
        <div className="stat"><span>今日完成</span><strong>{stats.done_today}</strong><small>{rateLabel}</small></div>
        {/* 小字只说这一档真正是什么：护栏拦下的。"模型答不上来"已经分到
            「等待补充」，不再混进这个数字里 —— 原来 62 条 rejected 里 57 条
            是 NO_SQL，而这行小字写着"越权或写入意图"。 */}
        <div className="stat"><span>已拦截</span><strong>{stats.rejected}</strong><small>触碰安全边界</small></div>
      </div>

      {error && <div className="audit-error">读取任务失败：{error}</div>}

      {result && (
        <div className="card task-card" id="taskRowsCard">
          <div className="card-head">
            <div>
              <strong>查询任务</strong>
              <p>每个任务拥有独立状态、执行轨迹和审计记录。共 {totalAll} 条（全部发起人）
                  {result.user ? ` · 当前账号 ${result.user}，只有自己发起的线程能续跑` : ' · 未登录，可以浏览但不能续跑'}。</p>
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
                        onClick={() => { setStatusFilter(value); setPage(1); setStatusOpen(false) }}
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
                  <div className="task-meta"><span>现场</span><strong>检查点在</strong></div>
                  <div className="task-meta"><span>当前节点</span><strong>INTERRUPT</strong></div>
                </>
              ) : task.status === 'running' ? (
                <>
                  <div className="task-meta"><span>阶段</span><strong>执行中</strong></div>
                  <div className="task-meta"><span>已发起</span><strong>{fmtClock(task.ts)}</strong></div>
                </>
              ) : task.status === 'waiting_review' || task.status === 'review_returned' ? (
                <>
                  <div className="task-meta"><span>风险</span><strong title={task.risk_why ?? ''}>{task.risk ?? '—'}</strong></div>
                  {/* 存疑理由是复核人唯一要看的东西，直接摆在行上；
                      多条时给第一条，其余挂 title。 */}
                  <div className="task-meta">
                    <span>存疑</span>
                    <strong title={(task.review_why ?? []).join('；')}>
                      {(task.review_why ?? []).length > 1
                        ? `${task.review_why![0].slice(0, 6)}… +${task.review_why!.length - 1}`
                        : (task.review_why?.[0] ?? '—').slice(0, 10)}
                    </strong>
                  </div>
                </>
              ) : task.status === 'waiting_approval' || task.status === 'needs_operator'
                   || task.status === 'waiting_input' ? (
                <>
                  <div className="task-meta"><span>风险</span><strong title={task.risk_why ?? ''}>{task.risk ?? '—'}</strong></div>
                  <div className="task-meta">
                    <span>原因</span>
                    <strong title={ruleTitle(task.rejected_by ?? '')}>{task.rejected_by || '—'}</strong>
                  </div>
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
              <div><span className={`status ${STATUS_WAIT[task.status] ? 'wait' : ''}`}
                         title={task.next_actor ?? ''}>{STATUS_LABEL[task.status]}</span></div>
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
              <strong>{totalAll ? '当前筛选条件下没有任务。' : '还没有任何执行记录。'}</strong>
              <span>
                {totalAll
                  ? '换个状态，或在筛选里重置数据源、发起人与时间再看。'
                  : '任务由提问产生 —— 到查询 Agent 问一次，或在这里创建任务，这里就会出现对应的线程。这一页列全部发起人的线程，不只是当前账号的。'}
              </span>
            </div>
          )}
        </div>
      )}

      {/* 分页条与审计中心同一套结构与类名，两页的操作手感必须一致 */}
      {total > 0 && (
        <div className="audit-pager">
          <span>共 {total} 条 · 第 {current} / {pages} 页</span>
          <span>
            <select value={pageSize}
                    onChange={event => { setPageSize(Number(event.target.value)); setPage(1) }}>
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
            onApply={next => { setFilters(next); setPage(1); setModal({ kind: 'none' }) }}
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
function buildDetail(task: Task, replay: Replay | null, currentUser: string): TaskDetailView {
  /* 列得出来 ≠ 动得了。这一页 2026-09-06 起列全部发起人的线程，但续跑仍然
     只有主人能做（服务端 /api/resume 校验归属）。不在这里判一次的话，别人的
     中断线程会挂着一个「补充信息并恢复」的按钮，点下去必然 404 —— 那正是
     原来"只列自己的"想避免的「列得出来、续不了」，方向反过来而已。 */
  const mine = (task.owner || '') === currentUser
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

  /* 三档"还有下一步"的结局，原来都落在 rejected 这一支里，弹窗一律说
     「改写问题后重新发起」—— 对等审批的人是错的（该去找负责人），对库连不上
     的人更是错的（改写法一万遍也连不上）。nextStep 是这个弹窗唯一有用的一句话，
     不能对三种人说同一句。 */
  const reason = task.status === 'waiting_review'
    ? {
      category: '结果待复核 · REVIEW',
      node: '结果可信度',
      detail: (task.review_why ?? []).join('；')
        || '这次查询跑成了，但结果带着存疑痕迹。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: task.next_actor
        || '等系统管理员看一眼：采信这个数字，或打回并说明原因。',
      action: 'none' as const,
      actionLabel: '等待复核',
    }
    : task.status === 'review_returned'
    ? {
      category: '复核未通过 · RETURNED',
      node: '结果可信度',
      detail: (task.review_why ?? []).join('；')
        || '这条结果经复核判定为不可采信。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: task.next_actor
        || '这个数字不采信。按复核意见换个问法重新发起 —— 原始记录与链路仍可查。',
      action: 'revise' as const,
      actionLabel: '按复核意见重问',
    }
    : task.status === 'waiting_approval'
    ? {
      category: `等待审批 · ${task.rejected_by ?? 'R-11'}`,
      node: task.rejected_by ?? '高成本查询',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '这次查询超过成本阈值，已挂起等待放行；SQL 没有在数据库上执行。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: task.next_actor
        || '审批通过后凭票重跑；审批是一次性的，用过即作废。',
      action: 'none' as const,
      actionLabel: '等待负责人放行',
    }
    : task.status === 'needs_operator'
    ? {
      category: '执行期故障 · EXEC',
      node: '数据源',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '这次调用在执行阶段失败：数据源连不上，或执行期出错。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: task.next_actor
        || '这不是权限问题，改写法也过不去。等数据源恢复后原样重试即可。',
      action: 'revise' as const,
      actionLabel: '恢复后重试',
    }
    : task.status === 'waiting_input'
    ? {
      category: '信息不足 · NO_SQL',
      node: '语义理解',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '模型没能从这个问题里确定要查什么，没有产出 SQL。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: task.next_actor
        || '把问题说具体些（指明表名、时间范围或指标口径）后重新发起。',
      action: 'revise' as const,
      actionLabel: '补充后重新提问',
    }
    : task.status === 'rejected'
    ? {
      category: `护栏拒绝 · ${replay?.rejected_by ?? 'GUARD'}`,
      node: replay?.rejected_by ?? '安全护栏',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '这次调用被护栏拦下，SQL 没有在数据库上执行。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: task.next_actor
        || '这条触碰的是安全边界，改写法也过不去；换个能在开放范围内回答的问法。',
      action: 'revise' as const,
      actionLabel: '调整后重新提问',
    }
    : task.status === 'running'
    ? {
      category: '执行中 · RUNNING',
      node: '执行图',
      detail: '这条线程已经发起、还没有收尾记录：要么正在跑，要么跑到一半进程没了。',
      policy: `${task.kind} · ${task.role}`,
      nextStep: '稍后刷新；若长时间停在这里，到执行追踪看它停在哪个节点。',
      action: 'none' as const,
      actionLabel: '执行中',
    }
    : task.status === 'interrupted'
      ? {
        category: '信息不足 · INPUT REQUIRED',
        node: replay?.snapshots?.map(item => (item.next ?? []).join(' / ')).filter(Boolean).slice(-1)[0] || 'INTERRUPT',
        detail: '任务在生成 SQL 前暂停等待补充条件，现场已经写进检查点。',
        policy: `${task.kind} · ${task.role}`,
        nextStep: !task.resumable
          ? '这条线程已经收尾，没有可续的断点。'
          : mine
            ? '补充条件后从断点继续执行；checkpoint 之前已完成的节点不会重跑。'
            : `这条线程由${task.owner ? ` ${task.owner} ` : '匿名访客'}发起，只有发起人能从断点继续。`
              + '执行轨迹与审计记录仍然可以查看。',
        action: task.resumable && mine ? ('clarify' as const) : ('none' as const),
        actionLabel: !task.resumable
          ? '无可续的断点'
          : mine ? '补充信息并恢复' : '仅发起人可续跑',
      }
      : null

  return {
    id: task.thread_id,
    statusLabel,
    wait,
    question: task.question || '（无问题文本）',
    description: `线程 ${task.thread_id} · 发起人 ${task.owner || '匿名'}`
      + `${mine ? '（本人）' : ''} · 已执行 ${task.attempts_on_thread} 次`,
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
