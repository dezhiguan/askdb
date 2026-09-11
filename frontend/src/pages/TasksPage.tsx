import { PageHeader } from '../components/AppShell'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { FilterBar, FilterChips, FilterSearch, type FilterChip } from '../components/FilterBar'
import {
  askQuestion,
  decideReview,
  fetchReplay,
  fetchResult,
  fetchSources,
  fetchTasks,
  resolveOps,
  resumeTask,
  type Replay,
  type TraceResult,
  type Task,
  type TaskStats,
  type TasksResult,
 type Me,
} from '../api'
import {
  ClarificationModal,
  CreateTaskModal,
  DispositionModal,
  EMPTY_TASK_FILTERS,
  ModalShell,
  TaskReasonModal,
  TaskResultModal,
  type CreateTaskPayload,
  type TaskDetailView,
  type TaskFilters,
  type TaskSourceOption,
} from '../components/Modals'
import type { View } from '../types'
import { personName } from '../person'
import { ruleTitle } from '../rules'
import { writeGuard } from '../writeGuard'
import { rolesLabel } from '../roles'

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
 *  数字随后到位，否则列表出现前整页会先跳一次。
 *
 *  **这几个 0 不能直接渲染**：卡片在位靠的是 DASH 占位，不是把 0 当真数字给
 *  出去。见页面里的 loading。 */
const EMPTY_STATS: TaskStats = {
  running: 0, waiting_input: 0, waiting_approval: 0, waiting_review: 0,
  review_returned: 0, needs_operator: 0, interrupted: 0, rejected: 0,
  done: 0, done_today: 0, success_rate: null,
}

/** 数字未知时的占位。与执行追踪页同一个字符，两页并排看不出差异。 */
const DASH = '—'

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

/** 风险档与发起时间档的显示文案。取值定义在后端（audit.RISK_LEVELS /
 *  SINCE_CHOICES），这里只是文案 —— 加档要两边一起加，否则传过去就是 400。 */
const RISK_LABEL: Record<string, string> = {
  all: '全部风险', HIGH: 'HIGH', MEDIUM: 'MEDIUM', LOW: 'LOW',
}
const SINCE_LABEL: Record<string, string> = {
  all: '全部时间', today: '今天', '7d': '近 7 天', '30d': '近 30 天',
}

type ModalState =
  | { kind: 'none' }
  | { kind: 'create' }
  | { kind: 'result'; task: Task }
  | { kind: 'reason'; task: Task }
  | { kind: 'clarify'; task: Task }
  /* 复核与运维处置共用 DispositionModal，但**分成两个 kind**：
     两者的结论写进不同的存储、打不同的接口，合成一个再靠状态去分支，
     就会在某个分支上把复核的判定发到运维接口上。 */
  | { kind: 'review'; task: Task }
  | { kind: 'ops'; task: Task }

export function TasksPage({ onNavigate, notify, me }: {
  /** 第二个参数是**带去目标页的内容**。查询页拿它做预填 —— 终态那几档的
   *  「换个问法」原来只是跳转，人得自己回来抄一遍原问题。 */
  onNavigate: (view: View, focus?: string) => void
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
  // 最终结果（/api/result）：答案 + 已脱敏结果行。与 replay 分开取，登录态才有。
  const [taskResult, setTaskResult] = useState<TraceResult | null>(null)
  const [resultLoading, setResultLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [filters, setFilters] = useState<TaskFilters>(EMPTY_TASK_FILTERS)
  /* 关键词是**提交后**的值，不是输入框里正在打的字：输入框自己防抖
     （FilterSearch），每敲一个字就发一次请求的话，翻页与统计都会跟着抖。 */
  const [keyword, setKeyword] = useState('')
  /* 页码。**换筛选条件的地方一并把它设回 1**，而不是靠一个 useEffect 去追 ——
     追的写法会先按旧页码请求一次、再按第 1 页请求一次，列表跳两下。
     停在第 7 页而筛完只剩 2 条，看到的会是一片空白。 */
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [sources, setSources] = useState<TaskSourceOption[]>([])

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
      q: keyword,
    })
      .then(value => { setResult(value); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [page, pageSize, statusFilter, filters, keyword])

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

  /* 这一页拿到的是**当前页**的行；统计卡、筛选下拉的可选值、两个总数都由
     服务端一并给出（audit.paginate_tasks）。原来这三样都在浏览器里从全量
     列表算，代价是每次打开要把一千四百多条线程发过来，而屏幕上只有十行。

     统计仍然算在**筛选之前**：这四个数讲的是系统当下的处境，跟着筛选变的话，
     筛完「已完成」再看「待处理」永远是 0。下拉可选值同理，跟着收窄就退不回去。 */
  const visible = useMemo(() => result?.items ?? [], [result])
  const stats = result?.stats ?? EMPTY_STATS
  /* 首帧 result 还没回来，stats 退化成 EMPTY_STATS（全 0）。把那几个 0 当成
     真数字渲染，看的人读到的是"任务中心是空的"，而其实只是还没加载完 ——
     "还不知道"和"确实是 0"在页面上必须分开。列表区本来就有「读取中…」，
     上面这四格是当时漏掉的另一半。 */
  const loading = !result && !error
  const total = result?.total ?? 0
  const totalAll = result?.total_all ?? 0
  const sourceOptions = result?.sources ?? []
  const userOptions = result?.users ?? []
  const labelForUser = (value: string, label: string) =>
    personName(label === value ? '' : label, value, me) || label || value
  // 成功率没有收尾样本时给 null，不是 0% —— 后者是在报一个没发生过的失败
  const rateLabel = loading ? '读取中…'
    : stats.success_rate === null ? '暂无收尾记录'
    : `成功率 ${stats.success_rate.toFixed(1)}%`

  const pages = Math.max(Math.ceil(total / pageSize), 1)
  const current = Math.min(result?.page ?? page, pages)

  /* 已选条件。每个都能单独摘掉 —— 一次只错一个条件时，不该逼人整条重来。 */
  const patch = (next: Partial<TaskFilters>) => setFilters(current => ({ ...current, ...next }))
  const chips: FilterChip[] = [
    keyword ? { label: '关键词', value: keyword, onClear: () => setKeyword('') } : null,
    statusFilter !== 'all'
      ? { label: '状态', value: FILTER_LABEL[statusFilter], onClear: () => setStatusFilter('all') }
      : null,
    filters.risk !== 'all'
      ? { label: '风险', value: filters.risk, onClear: () => patch({ risk: 'all' }) } : null,
    filters.source !== 'all'
      ? {
        label: '数据源',
        value: sourceOptions.find(item => item.value === filters.source)?.label ?? filters.source,
        onClear: () => patch({ source: 'all' }),
      } : null,
    filters.user !== 'all'
      ? {
        label: '发起人',
        value: labelForUser(
          filters.user,
          userOptions.find(item => item.value === filters.user)?.label ?? filters.user,
        ),
        onClear: () => patch({ user: 'all' }),
      } : null,
    filters.since !== 'all'
      ? { label: '时间', value: SINCE_LABEL[filters.since] ?? filters.since, onClear: () => patch({ since: 'all' }) }
      : null,
  ].filter(Boolean) as FilterChip[]
  const resetFilters = () => {
    setStatusFilter('all')
    setFilters(EMPTY_TASK_FILTERS)
    setKeyword('')
  }

  // 筛选条件一变就回到第一页 —— 停在第 7 页而结果只剩 2 条，会看到一片空白
  useEffect(() => { setPage(1) }, [statusFilter, filters, keyword, pageSize])

  /* 结果与原因都来自审计回放：没有回放就说没有，不靠状态推断内容 */
  const openDetail = (task: Task, kind: 'result' | 'reason') => {
    setModal({ kind, task })
    setReplay(null)
    setTaskResult(null)
    setReplayLoading(true)
    fetchReplay(task.trace_id)
      .then(value => setReplay(value.status === 'ok' ? value.data : null))
      .catch(() => setReplay(null))
      .finally(() => setReplayLoading(false))
    /* 最终结果不受 replay_api 开关约束（走 /api/result）——回放关闭时也能拿到。
       它自己的加载态必须单独记：两个请求各跑各的，回放先回来（关着开关时几乎是
       立刻）就把 loading 放掉的话，结果还在路上的那几百毫秒会先渲染出一句
       「当前状态暂无结果 · 审计回放不可读」，紧接着又被结果顶掉 —— 一次成功的
       查询在自己的结果弹窗里先被宣告没有结果，比转圈久一点糟得多。 */
    setResultLoading(true)
    fetchResult(task.trace_id)
      .then(setTaskResult)
      .catch(() => setTaskResult(null))
      .finally(() => setResultLoading(false))
  }

  const detail = useMemo<TaskDetailView | null>(() => {
    /* 白名单改黑名单：处置弹窗（review / ops）也要 detail，漏一个 kind
       就是一个打不开的弹窗，而新增 kind 时没人会记得回来补这一行。 */
    if (modal.kind === 'none' || modal.kind === 'create') return null
    return buildDetail(modal.task, replay, result?.user ?? '', taskResult, me)
  }, [modal, replay, result, taskResult, me])

  /** 补充条件后在同一条线程上继续。
   *
   *  两种情形走同一个接口，由服务端按"现场在不在检查点里"分流：
   *    · 真中断（进程被杀）→ 从断点继续，已完成的节点不重跑
   *    · 等待补充（NO_SQL）→ 带着补充条件重跑整条链路
   *  前端不判这个 —— 判据在 graph.resume，前端再写一份必然漂。 */
  const resume = async (task: Task, clarification = '') => {
    setBusy(task.thread_id)
    try {
      const response = await resumeTask(task.thread_id, clarification)
      if (!response) {
        notify('这个任务已经跑完，或不属于当前账号')
      } else if (response.ok) {
        notify(`已继续执行完成 · 新 trace ${response.trace_id}`)
      } else {
        notify(`仍未跑通：${response.rejected_by ?? ''} ${response.error ?? ''}`.trim())
      }
      setModal({ kind: 'none' })
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  /** 凭已批准的审批票原样重跑。
   *
   *  **必须用问题原文**：票绑在原文的指纹上（approvals.fingerprint），
   *  改一个字就 403。所以这里不给编辑入口 —— 要改问法就是另一次提问，
   *  也该另外走一次审批。 */
  const redeem = async (task: Task) => {
    setBusy(task.thread_id)
    try {
      const response = await askQuestion(
        task.question || '', task.source || '', undefined, false, task.trace_id)
      if (response.ok) {
        notify(`已凭票重跑 · 新 trace ${response.trace_id}`)
      } else {
        notify(`重跑未通过：${response.rejected_by ?? ''} ${response.error ?? ''}`.trim())
      }
      setModal({ kind: 'none' })
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const submitReview = async (task: Task, accepted: boolean, note: string) => {
    setBusy(task.thread_id)
    try {
      await decideReview(task.trace_id, accepted, note)
      notify(accepted ? '已采信这条结果' : '已打回，发起人会看到你的意见')
      setModal({ kind: 'none' })
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const submitOps = async (task: Task, resolved: boolean, note: string) => {
    setBusy(task.thread_id)
    try {
      await resolveOps(task.trace_id, resolved ? 'RESOLVED' : 'WONTFIX', note)
      notify(resolved ? '已标记为故障已排除，发起人可重试' : '已标记为无法恢复')
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
    const task = modal.task
    switch (detail.reason.action) {
      case 'clarify': setModal({ kind: 'clarify', task }); return
      case 'review': setModal({ kind: 'review', task }); return
      case 'ops': setModal({ kind: 'ops', task }); return
      case 'redeem': void redeem(task); return
      case 'revise':
        setModal({ kind: 'none' })
        /* **带上问题原文**。这里原来只是 onNavigate('query')，查询页是空白的
           —— 点「调整后重新提问」的人得自己回来抄一遍原问题。终态那几档
           （护栏拦下、复核打回、运维已恢复）走的都是这一条。 */
        onNavigate('query', task.question || '')
        return
      default: return
    }
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
        <div className="stat"><span>运行中</span><strong>{loading ? DASH : stats.running}</strong><small>已发起未收尾</small></div>
        {/* 「待处理」= 还等着**某个人**动手的那些。三档分开写：等谁动手不一样，
            合成一个数字就等于让人自己去猜该找谁。审批那格原来写死 0。 */}
        <div className="stat">
          <span>待处理</span>
          <strong>{loading ? DASH
                   : stats.waiting_input + stats.waiting_approval + stats.waiting_review
                     + stats.needs_operator + stats.interrupted}</strong>
          <small>
            {loading ? '读取中…' : <>
              {stats.waiting_input} 补充信息 · {stats.waiting_approval} 审批
              · {stats.waiting_review} 复核 · {stats.needs_operator} 运维
              · {stats.interrupted} 可续跑
            </>}
          </small>
        </div>
        <div className="stat"><span>今日完成</span><strong>{loading ? DASH : stats.done_today}</strong><small>{rateLabel}</small></div>
        {/* 小字只说这一档真正是什么：护栏拦下的。"模型答不上来"已经分到
            「等待补充」，不再混进这个数字里 —— 原来 62 条 rejected 里 57 条
            是 NO_SQL，而这行小字写着"越权或写入意图"。 */}
        <div className="stat"><span>已拦截</span><strong>{loading ? DASH : stats.rejected}</strong><small>触碰安全边界</small></div>
      </div>

      {error && <div className="audit-error">读取任务失败：{error}</div>}

      {result && (
        <div className="card task-card" id="taskRowsCard">
          <div className="card-head">
            <div>
              <strong>查询任务</strong>
              <p>每个任务拥有独立状态、执行轨迹和审计记录
                  {result.user ? ` · 当前账号 ${result.user}，只有自己发起的线程能续跑` : ' · 未登录，可以浏览但不能续跑'}。</p>
            </div>
          </div>

          {/* 筛选条常驻页面（原来是「全部状态」自绘下拉 + 一个「筛选」弹窗）。
              弹窗的问题不在多点一下：条件藏在里面，页面上只剩一个高亮的按钮，
              看列表的人无从知道自己正按什么在看。六个条件都在服务端筛
              （/api/tasks），选中即生效。 */}
          <FilterBar>
            <FilterSearch
              value={keyword}
              onCommit={setKeyword}
              placeholder="搜索问题 / 线程 ID / trace…"
            />
            <select
              className={statusFilter === 'all' ? '' : 'on'}
              aria-label="按状态筛选"
              value={statusFilter}
              onChange={event => setStatusFilter(event.target.value as StatusFilter)}
            >
              {FILTER_ORDER.map(value => (
                <option key={value} value={value}>
                  {FILTER_LABEL[value]}{value === 'all' ? '' : ` · ${FILTER_CODE[value]}`}
                </option>
              ))}
            </select>
            <select
              className={filters.risk === 'all' ? '' : 'on'}
              aria-label="按风险等级筛选"
              value={filters.risk}
              onChange={event => patch({ risk: event.target.value })}
            >
              {['all', 'HIGH', 'MEDIUM', 'LOW'].map(value => (
                <option key={value} value={value}>{RISK_LABEL[value]}</option>
              ))}
            </select>
            <select
              className={filters.source === 'all' ? '' : 'on'}
              aria-label="按数据源筛选"
              value={filters.source}
              onChange={event => patch({ source: event.target.value })}
            >
              <option value="all">全部数据源</option>
              {sourceOptions.map(item => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
            <select
              className={filters.user === 'all' ? '' : 'on'}
              aria-label="按发起人筛选"
              value={filters.user}
              onChange={event => patch({ user: event.target.value })}
            >
              <option value="all">全部发起人</option>
              {userOptions.map(item => (
                <option key={item.value} value={item.value}>
                  {labelForUser(item.value, item.label)}
                </option>
              ))}
            </select>
            <select
              className={filters.since === 'all' ? '' : 'on'}
              aria-label="按发起时间筛选"
              value={filters.since}
              onChange={event => patch({ since: event.target.value })}
            >
              {['all', 'today', '7d', '30d'].map(value => (
                <option key={value} value={value}>{SINCE_LABEL[value]}</option>
              ))}
            </select>
            <button className="ghost" disabled={!chips.length} onClick={resetFilters}>重置</button>
          </FilterBar>
          <FilterChips chips={chips} matched={total} total={totalAll} />

          {/* 原型里本地新建的任务挂在这里；本实现的新建任务直接进真实任务流，
              容器保留以对齐结构（:empty 时不占位） */}
          <div className="created-task-list" />

          {/* 表头与行包进 .task-list：列宽只定一份，子项 subgrid 对齐。
              筛空时不渲染 —— 空态卡片上挂一条孤零零的表头没有意义。 */}
          {visible.length > 0 && (
            <div className="task-list">
              <div className="task-head" role="row">
                <span />
                <span>任务 / 线程</span>
                <span>数据源</span>
                <span>风险</span>
                <span>关键信息</span>
                <span>状态</span>
                <span className="right">操作</span>
              </div>

              {visible.map(task => (
                <div className="task-row" key={task.thread_id} data-task-id={task.thread_id}>
                  <i className={`task-state ${STATUS_WAIT[task.status] ? 'wait' : ''}`}>{STATE_GLYPH[task.status]}</i>
                  <div className="task-main">
                    <strong title={task.question ?? ''}>{task.question || '（无问题文本）'}</strong>
                    {/* 发起人显示姓名，与审计中心 / 成员名册「姓名」同一口径；
                        名册里查不到、未登录（姓名不下发）时退回账号，title 留账号 */}
                    <small title={task.user ? `发起人账号 ${task.user}` : undefined}>
                      {task.thread_id} · {personName(task.user_name, task.user, me)} · {fmtClock(task.ts)}
                      {' · 已执行 '}{task.attempts_on_thread} 次
                    </small>
                  </div>
                  {/* 数据源恒为一列：这一页把所有数据源的线程列在一起，「这条跑在哪个库」
                      原来只有点开弹窗才看得到，而筛选条上就摆着「全部数据源」——
                      能筛却看不见。取列表自带的 source_name（审计 summary 字段），
                      没记名字时退回源 id，两个都没有才是占位。 */}
                  <div className="task-meta task-source">
                    <strong title={task.source_name || task.source || ''}>
                      {task.source_name || task.source || '—'}
                    </strong>
                  </div>
                  {/* 第四列**恒为风险**：后端对每一条线程都算了档（audit._risk，
                      没有收尾码时兜底 LOW），所以这一列能被表头钉住。原来
                      running / interrupted 两档在这里放的是"阶段"和"现场：检查点在"
                      —— 一个表头之下三种含义，那样的表头是在骗人。 */}
                  <div className="task-meta"><strong title={task.risk_why ?? ''}>{task.risk ?? '—'}</strong></div>
                  {/* 第五列是唯一随状态变的一列，所以只有它保留行内小标签 */}
                  {keyInfo(task)}
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
            </div>
          )}

          {visible.length === 0 && (
            <div className="task-empty">
              <strong>{totalAll ? '当前筛选条件下没有任务。' : '还没有任何执行记录。'}</strong>
              <span>
                {totalAll
                  ? '换个状态，或清掉上面的筛选条件再看。'
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
            loading={replayLoading || resultLoading}
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
            /* agent 自己给出的那几句 —— 这个框里唯一有信息量的引导。
               来源按可信度排：回放里的具体报错 > 后端折算的下一步 > 存疑理由。 */
            hints={[
              replay?.snapshots?.find(item => item.error)?.error ?? '',
              modal.task.next_actor ?? '',
              ...(modal.task.review_why ?? []),
            ].filter(Boolean)}
            busy={busy === modal.task.thread_id}
            onClose={() => setModal({ kind: 'none' })}
            onConfirm={text => resume(modal.task, text)}
          />
        </ModalShell>
      )}

      {modal.kind === 'review' && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <DispositionModal
            eyebrow={`${modal.task.trace_id} · REVIEW`}
            title="这个数字算不算数"
            subject={modal.task.question || '（无问题文本）'}
            facts={modal.task.review_why ?? []}
            affirmLabel="采信"
            denyLabel="打回"
            denyNeedsNote
            busy={busy === modal.task.thread_id}
            onClose={() => setModal({ kind: 'none' })}
            onDecide={(affirm, note) => submitReview(modal.task, affirm, note)}
          />
        </ModalShell>
      )}

      {modal.kind === 'ops' && (
        <ModalShell onClose={() => setModal({ kind: 'none' })}>
          <DispositionModal
            eyebrow={`${modal.task.trace_id} · OPS`}
            title="这次故障处理完了吗"
            subject={modal.task.question || '（无问题文本）'}
            facts={[
              replay?.snapshots?.find(item => item.error)?.error ?? '',
              modal.task.source_name || modal.task.source || '',
            ].filter(Boolean)}
            affirmLabel="已恢复"
            denyLabel="无法恢复"
            denyNeedsNote
            busy={busy === modal.task.thread_id}
            onClose={() => setModal({ kind: 'none' })}
            onDecide={(affirm, note) => submitOps(modal.task, affirm, note)}
          />
        </ModalShell>
      )}
    </div>
  )
}

/** 行上第五列：这条线程此刻唯一值得先看的那件事。
 *
 *  它按状态取不同的东西，所以**保留行内小标签**——表头只能说到"关键信息"，
 *  具体是耗时还是存疑理由得由行自己讲。其余各列的含义都固定，由表头交代。
 *
 *  running 原来在这里放"已发起 12:58"，interrupted 放"现场：检查点在" ——
 *  前者与行首那行小字里的时间是同一个值，后者没有任何信息量（状态已经写着
 *  可续跑了）。两处都换成真正只有这一档才有的东西：跑到哪、停在哪个节点。 */
function keyInfo(task: Task) {
  if (task.status === 'waiting_review' || task.status === 'review_returned') {
    /* 存疑理由是复核人唯一要看的东西，直接摆在行上；多条时给第一条，其余挂 title */
    const why = task.review_why ?? []
    return (
      <div className="task-meta">
        <span>存疑</span>
        <strong title={why.join('；')}>
          {why.length > 1 ? `${why[0].slice(0, 6)}… +${why.length - 1}` : (why[0] ?? '—').slice(0, 10)}
        </strong>
      </div>
    )
  }
  if (task.status === 'rejected' || task.status === 'waiting_approval'
      || task.status === 'needs_operator' || task.status === 'waiting_input') {
    /* 这一格曾经对 rejected 写死 GUARD —— 接口给的是**具体规则号**，
       写死等于把"撞了哪条护栏"这个唯一有用的信息抹掉了 */
    return (
      <div className="task-meta">
        <span>原因</span>
        <strong title={ruleTitle(task.rejected_by ?? '')}>{task.rejected_by || '—'}</strong>
      </div>
    )
  }
  if (task.status === 'running') {
    return <div className="task-meta"><span>阶段</span><strong>执行中</strong></div>
  }
  if (task.status === 'interrupted') {
    return <div className="task-meta"><span>节点</span><strong>INTERRUPT</strong></div>
  }
  return <div className="task-meta"><span>耗时</span><strong>{fmtDuration(task.elapsed_ms)}</strong></div>
}

/** 当前这个人能对这条任务做什么。
 *
 *  **三个维度一起判，缺一个就会出现"按钮亮着、点下去 403"**：
 *    · 状态   —— 这一档有没有待办
 *    · 权限   —— 复核要 APPROVE，处置要 OPS_RESOLVE（服务端同一套判定）
 *    · 归属   —— 补充与凭票重跑只有发起人能做（服务端 /api/resume 校验一行没改）
 *
 *  角色码在这里判而不是等服务端回结论：队列接口各自给了 can_review /
 *  can_resolve，但任务中心一次要判几十行，不可能每行问一次。**判据必须与
 *  identity.CAPABILITIES 一致** —— 那边加位、这里漏加，症状是按钮不出现，
 *  没有任何报错。
 */
const APPROVE_ROLES = ['SYS_ADMIN']
const OPS_ROLES = ['SRE', 'SYS_ADMIN']

function hasRole(me: Me | null, allowed: string[]): boolean {
  return (me?.roles ?? []).some(r => allowed.includes(r))
}

/** 把 /api/tasks 的一行 + /api/replay 的回放拼成弹窗要的视图对象。 */
function buildDetail(task: Task, replay: Replay | null, currentUser: string,
                    finalRes: TraceResult | null,
                    me: Me | null = null): TaskDetailView {
  /* 列得出来 ≠ 动得了。这一页 2026-09-06 起列全部发起人的线程，但续跑仍然
     只有主人能做（服务端 /api/resume 校验归属）。不在这里判一次的话，别人的
     中断线程会挂着一个「补充信息并恢复」的按钮，点下去必然 404 —— 那正是
     原来"只列自己的"想避免的「列得出来、续不了」，方向反过来而已。 */
  const mine = (task.owner || '') === currentUser
  const statusLabel = STATUS_LABEL[task.status]
  const wait = STATUS_WAIT[task.status]
  const duration = fmtDuration(task.elapsed_ms)
  /* 数据源名列表自己就有（审计 summary 字段），不必等回放：回放关着的时候
     这一格原来恒显示「只读数据源」——旁边一行明明写着 ragforge 生产库，弹窗里
     却像是不知道跑在哪。org 号只有回放给得出，有就补在后面。 */
  const org = replay && replay.org_id !== null && replay.org_id !== undefined ? `org ${replay.org_id}` : ''
  const source = [task.source_name || '', org, '只读'].filter(Boolean).join(' · ')
  const list = (values: string[] | null | undefined) => (values && values.length ? values.join(', ') : '—')

  const sql = replay?.sql_final || replay?.sql_raw || ''
  // 最终结果（/api/result）：已脱敏结果行 + 答案。登录态才有；被拦/旧记录为 null。
  const hasFinal = Boolean(finalRes && ((finalRes.rows_preview?.length ?? 0) > 0 || finalRes.answer))
  const rowCount = finalRes?.rows_returned ?? replay?.rows_returned
  const result = (task.status === 'done' && (hasFinal || (replay && sql)))
    ? {
      conclusion: `本次查询返回 ${rowCount ?? '—'} 行结果`,
      note: hasFinal
        ? '下方为本次查询的结果（已脱敏、前若干行）与执行可核对信息。'
        : '审计只保留执行事实与原生 SQL，不保存结果行；下表是这次执行可核对的信息。',
      answer: finalRes?.answer || '',
      resultColumns: finalRes?.columns ?? [],
      resultRows: (finalRes?.rows_preview ?? []) as unknown[][],
      resultNote: [
        typeof rowCount === 'number' ? `共 ${rowCount} 行` : '',
        (finalRes?.rows_returned ?? 0) > (finalRes?.rows_preview?.length ?? 0) ? `仅前 ${finalRes?.rows_preview?.length} 行` : '',
        (finalRes?.masked_columns?.length ?? 0) > 0 ? `已脱敏 ${finalRes?.masked_columns?.join('、')}` : '',
      ].filter(Boolean).join(' · '),
      // 执行可核对信息来自 /api/replay（要 replay_api 开关）；关着时为空，
      // 模态按长度决定渲不渲染 —— 结果行本身走 /api/result，不依赖它。
      overview: (replay ? [
        ['返回行数', String(replay.rows_returned ?? '—')],
        ['耗时', fmtDuration(replay.elapsed_ms ?? task.elapsed_ms)],
        ['扫描估算', replay.explain_rows === null || replay.explain_rows === undefined ? '—' : String(replay.explain_rows)],
      ] : []) as [string, string][],
      rows: (replay ? [
        ['命中表', list(replay.tables_hit), '来自审计记录'],
        ['命中指标', list(replay.metrics_hit), '认证口径'],
        ['护栏规则', list(replay.rules_fired), '生成后触发'],
        ['Token 用量', `${replay.tok_in ?? 0} / ${replay.tok_out ?? 0}`, '入 / 出'],
        ['调用成本', replay.cost_cny === null || replay.cost_cny === undefined ? '—' : `¥${replay.cost_cny.toFixed(4)}`, '按模型计价'],
      ] : []) as [string, string, string][],
      sql: sql || '',
    }
    : null

  /* 三档"还有下一步"的结局，原来都落在 rejected 这一支里，弹窗一律说
     「改写问题后重新发起」—— 对等审批的人是错的（该去找负责人），对库连不上
     的人更是错的（改写法一万遍也连不上）。nextStep 是这个弹窗唯一有用的一句话，
     不能对三种人说同一句。 */
  const canReview = hasRole(me, APPROVE_ROLES)
  const canOps = hasRole(me, OPS_ROLES)
  /* 自己不能复核自己的结果（服务端 reviews.SelfReview 会 403）。
     不在这里判一次的话，管理员看自己那条会看到一个必然失败的「采信」按钮 ——
     与"列得出来不等于动得了"是同一条轴，只是换了一个维度。 */
  const reviewable = canReview && !mine

  const reason = task.status === 'waiting_review'
    ? {
      category: '结果待复核 · REVIEW',
      node: '结果可信度',
      detail: (task.review_why ?? []).join('；')
        || '这次查询跑成了，但结果带着存疑痕迹。',
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      nextStep: reviewable
        ? '采信这个数字，或打回并说明原因 —— 打回不撤销已经返回的结果，改变的是它此后的可信标记。'
        : canReview
          ? '这是你自己发起的结果，需要另一位系统管理员复核。'
          : (task.next_actor || '等系统管理员看一眼：采信这个数字，或打回并说明原因。'),
      action: reviewable ? ('review' as const) : ('none' as const),
      actionLabel: reviewable ? '采信 / 打回'
        : canReview ? '不能复核自己的结果' : '等待复核',
    }
    : task.status === 'review_returned'
    ? {
      category: '复核未通过 · RETURNED',
      node: '结果可信度',
      detail: (task.review_why ?? []).join('；')
        || '这条结果经复核判定为不可采信。',
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      nextStep: task.next_actor
        || '这个数字不采信。按复核意见换个问法重新发起 —— 原始记录与链路仍可查。',
      action: 'revise' as const,
      actionLabel: '按复核意见重问',
    }
    : task.status === 'waiting_approval'
    ? {
      category: task.approval_status === 'APPROVED'
        ? `已批准待重跑 · ${task.rejected_by ?? 'R-11'}`
        : `等待审批 · ${task.rejected_by ?? 'R-11'}`,
      node: task.rejected_by ?? '高成本查询',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '这次查询超过成本阈值，已挂起等待放行；SQL 没有在数据库上执行。',
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      /* 批准与待批是同一个状态码下的两句话，等的人正好相反（见 audit.stage）。
         服务端把差别写进 next_actor，这里照着显示，不在前端再判一遍。 */
      nextStep: task.next_actor
        || '审批通过后凭票重跑；审批是一次性的，用过即作废。',
      /* **放行票只能由发起人用**：服务端 approvals.waiver 校验"是本人的"，
         别人点下去必然 403。所以按钮只对主人出现。 */
      action: (task.approval_status === 'APPROVED' && mine)
        ? ('redeem' as const) : ('none' as const),
      actionLabel: task.approval_status === 'APPROVED'
        ? (mine ? '凭票重跑' : '已批准，待发起人重跑')
        : '等待系统管理员放行',
    }
    : task.status === 'needs_operator'
    ? {
      category: task.stale ? '执行中断 · 无现场' : '执行期故障 · EXEC',
      node: '数据源',
      detail: task.stale
        ? '这条线程只落了发起记录就再没有下文（进程中途退出），检查点里也没有可续的现场。'
        : (replay?.snapshots?.find(item => item.error)?.error
           ?? '这次调用在执行阶段失败：数据源连不上，或执行期出错。'),
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      nextStep: canOps
        ? '排除故障后标记处置结论：已恢复（发起人可原样重试）或无法恢复。'
        : (task.next_actor
           || '这不是权限问题，改写法也过不去。等数据源恢复后原样重试即可。'),
      action: canOps ? ('ops' as const) : ('revise' as const),
      actionLabel: canOps ? '标记处置结论' : '恢复后重试',
    }
    : task.status === 'waiting_input'
    ? {
      category: '信息不足 · NO_SQL',
      node: '语义理解',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '模型没能从这个问题里确定要查什么，没有产出 SQL。',
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      /* **补充回到同一条线程**，不是重新提问。
         2026-09-11 之前这里的动作是 revise（跳回查询页，还不带问题原文），
         于是补充等于开一条新线程，原来那条永远停在等待补充 —— 线上积压 294 条
         就是这么来的。现在走 /api/resume 带补充条件，审计里看得出这是第 2 次执行。 */
      nextStep: mine
        ? '补充缺的那个条件（时间范围、口径或统计维度），在同一条线程上继续。'
        : `这条线程由${task.owner ? ` ${task.owner} ` : '匿名访客'}发起，只有发起人能补充。`
          + '执行轨迹与审计记录仍然可以查看。',
      action: mine ? ('clarify' as const) : ('none' as const),
      actionLabel: mine ? '补充条件并继续' : '仅发起人可补充',
    }
    : (task.status === 'rejected' && task.ops_status)
    ? {
      /* 运维处置过的执行期故障也落在 rejected 上（见 audit.stage 那段说明），
         但它**不是护栏拒绝** —— 说成"触碰了安全边界"会把人指向完全错误的
         下一步：一个该重试，一个改写法也没用。 */
      category: task.ops_status === 'RESOLVED'
        ? '执行期故障 · 已恢复' : '执行期故障 · 无法恢复',
      node: '数据源',
      detail: task.ops_status === 'RESOLVED'
        ? '运维已确认故障排除。这条查询本身没有问题，原样重试即可。'
        : '运维判定这条恢复不了（数据源已下线，或表已不存在）。',
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      nextStep: task.ops_status === 'RESOLVED'
        ? '回查询页原样再问一次 —— 系统不替你重试，那会花掉一次你没在等的配额。'
        : '换一个能在现有数据源上回答的问法。',
      action: 'revise' as const,
      actionLabel: task.ops_status === 'RESOLVED' ? '原样重试' : '换个问法',
    }
    : task.status === 'rejected'
    ? {
      category: `护栏拒绝 · ${replay?.rejected_by ?? 'GUARD'}`,
      node: replay?.rejected_by ?? '安全护栏',
      detail: replay?.snapshots?.find(item => item.error)?.error
        ?? '这次调用被护栏拦下，SQL 没有在数据库上执行。',
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
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
      policy: `${task.kind} · ${rolesLabel(task.role)}`,
      nextStep: '稍后刷新；若长时间停在这里，到执行追踪看它停在哪个节点。',
      action: 'none' as const,
      actionLabel: '执行中',
    }
    : task.status === 'interrupted'
      ? {
        category: '信息不足 · INPUT REQUIRED',
        node: replay?.snapshots?.map(item => (item.next ?? []).join(' / ')).filter(Boolean).slice(-1)[0] || 'INTERRUPT',
        detail: '任务在生成 SQL 前暂停等待补充条件，现场已经写进检查点。',
        policy: `${task.kind} · ${rolesLabel(task.role)}`,
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
