import { useEffect, useState } from 'react'

import {
  fetchEvalRun, fetchLiveQuality, fetchOfflineQuality, LoginRequired, startEvalRun,
  type EvalRunState, type LiveQuality, type Me, type OfflineQuality,
} from '../api'
import { PageHeader } from '../components/AppShell'
import type { View } from '../types'
import { STEP_NAMES } from '../traceSteps'
import { writeGuard } from '../writeGuard'

/** Agent 质量中心。
 *
 *  版式照原型（trusted-data-agent-prototype.html #view-evaluation）1:1 落地，
 *  **数据尚未接入后端** —— 页面上所有数字都来自设计稿，不是本实例的运行结果。
 *  顶部由 MockNotice 挂一条去不掉的声明，理由见该组件注释：这一段时间里
 *  最危险的不是数据假，而是看不出它是假的。
 *
 *  接线时的对应关系（后端已有这些数据，只是这一版没接）：
 *    · 运行总览 / 线上质量 → /api/audit/stats（调用量、拦截、P95、token、成本、日序列）
 *    · 离线回归 / 评测集   → /api/eval（盲测 blind、消融 groups、失败样本 failures）
 *  接上之后删掉 MockNotice 里的 evaluation 条目即可。
 */

type Scope = 'runtime' | 'online' | 'offline' | 'dataset'

/** 按钮文案跟着范围走 —— 同一个按钮在四屏下做的不是同一件事，
 *  文案不变就会让人以为在「离线回归」下点它也是在刷新线上数据。 */
const ACTION_LABEL: Record<Scope, string> = {
  runtime: '↻ 刷新运行状态',
  online: '↻ 刷新线上数据',
  offline: '▶ 运行回归评测',
  dataset: '',
}
/** Agent 版本号 —— **写死**。askdb 本身没有对外的"Agent 版本"概念（包版本
 *  0.1.0 是另一回事），产品上先按设计稿显示；接上真正的版本来源后换掉这里即可。
 *
 *  **全文件的版本字面量只准出现在这两个常量里**（有护栏用例钉着）：散落进 JSX
 *  之后，接上真实来源时必然漏改，页面上就会同时出现两个版本号。 */
const AGENT_VERSION = 'Agent v2.4'

/** 评测版本选择器的选项。只有当前版本可选 —— 结果文件里只有一套成绩，
 *  选中别的版本页面数字不会变，那比没有这个控件更误导。 */
const EVAL_VERSION_OPTIONS: { label: string; disabled: boolean }[] = [
  { label: `${AGENT_VERSION} · 当前版本`, disabled: false },
  { label: 'Agent v2.3 · 线上基线', disabled: true },
  { label: 'Agent v2.5-rc · 候选版本', disabled: true },
]

const BUSY_LABEL: Record<Scope, string> = {
  runtime: '正在刷新运行状态…',
  online: '正在聚合线上数据…',
  offline: '正在读取回归结果…',
  dataset: '',
}
type Category = 'overview' | 'accuracy' | 'security' | 'stability' | 'performance'

/** 四个范围。角标**跟着真数据走** —— 设计稿里是写死的 HEALTHY / 4,286 RUNS /
 *  126 CASES / V12，那正是最容易被当成真数字看的位置：它就贴在导航上，
 *  不点进去也会被读到。 */
const SCOPES: { key: Scope; icon: string; title: string; sub: string }[] = [
  { key: 'runtime', icon: 'OPS', title: '运行总览', sub: '本实例最近的真实调用' },
  { key: 'online', icon: 'LIVE', title: '线上质量', sub: '按审计记录统计，不用黄金集分母' },
  { key: 'offline', icon: 'OFF', title: '离线回归', sub: '黄金集验证结果' },
  { key: 'dataset', icon: 'SET', title: '评测集', sub: '黄金问题、期望与本轮结果' },
]

/** 角标由实时数据算，没有就显示「—」，不留写死值 */
function scopeTag(key: Scope, live: LiveQuality | null, offline: OfflineQuality | null): string {
  if (key === 'runtime') return live ? `${live.days}D` : '—'
  if (key === 'online') return live ? `${live.runs.toLocaleString()} RUNS` : '—'
  if (key === 'offline') return offline?.blind ? `${offline.blind.n} CASES` : '—'
  return offline?.golden ? `${offline.golden.total} 条` : '—'
}

const CATEGORIES: { key: Category; label: string }[] = [
  { key: 'overview', label: '评测总览' },
  { key: 'accuracy', label: '准确性' },
  { key: 'security', label: '安全合规' },
  { key: 'stability', label: '稳定性' },
  { key: 'performance', label: '性能成本' },
]

function Spark({ bars }: { bars: number[] }) {
  return (
    <div className="eval-spark" aria-hidden="true">
      {bars.map((h, i) => <i style={{ height: `${h}%` }} key={i} />)}
    </div>
  )
}

function ScoreCard({ label, tag, value, unit, note, bars }: {
  label: string; tag: string; value: string; unit: string; note: string; bars: number[]
}) {
  return (
    <article className="eval-score-card">
      <div className="eval-score-top"><span>{label}</span><code>{tag}</code></div>
      <div className="eval-score-value"><strong>{value}</strong><small>{unit}</small></div>
      <small>{note}</small>
      <Spark bars={bars} />
    </article>
  )
}

function MetricCard({ label, value, note, status, danger, wait }: {
  label: string; value: string; note: string; status?: string; danger?: boolean
  /** 角标走待办色（原型里 .status.wait）—— 用在还没测出来、还没实现的格子上 */
  wait?: boolean
}) {
  return (
    <article className={`eval-metric-card ${danger ? 'danger-metric' : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
      {status && <span className={`status ${wait ? 'wait' : ''}`}>{status}</span>}
    </article>
  )
}

function Dimension({ label, pct, value }: { label: string; pct: number; value: string }) {
  return (
    <div className="eval-dimension">
      <span>{label}</span>
      <div className="eval-dimension-track"><i style={{ width: `${pct}%` }} /></div>
      <strong>{value}</strong>
    </div>
  )
}

export function EvaluationPage({ onNavigate, me, onOpenLogin }: {
  onNavigate?: (view: View) => void
  me: Me | null
  /** 摆出登录页。会话在页面开着的时候失效时，外壳自己会摆一次；
   *  那一次被关掉之后，这一页得留一条回去的路，否则只剩一句"请先登录"
   *  而页面上没有任何地方可点。 */
  onOpenLogin?: () => void
}) {
  /* 「运行回归评测」是写操作（POST /api/eval/run），未登录必被写门拦下。
     按钮照旧可点、点完再报错，等于摆一个必然失败的入口 —— 与站内其余七个
     写入口同一个处置：置灰 + 说明为什么。置灰不是边界，边界在服务端。 */
  const guard = writeGuard(me, '运行回归评测')
  const [scope, setScope] = useState<Scope>('runtime')
  const [category, setCategory] = useState<Category>('overview')
  const [days, setDays] = useState(1)
  const [live, setLive] = useState<LiveQuality | null>(null)
  const [offline, setOffline] = useState<OfflineQuality | null>(null)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  // 取数时刻。上下文条要显示"数据更新 30 秒前"——这句话只有当它跟着**这次取数**
  // 走时才是真的，所以记时刻、按 tick 重算，而不是写一个固定文案
  const [fetchedAt, setFetchedAt] = useState<number | null>(null)
  // 取数进行中。刷新按钮不给反馈的话，点下去与没点是一样的画面 ——
  // 数据没变（本来就没有新调用）时，人只会以为按钮坏了
  const [busy, setBusy] = useState(false)
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = window.setInterval(() => setTick(n => n + 1), 15000)
    return () => window.clearInterval(t)
  }, [])

  /* 回归评测的实时状态。**跑在服务端、与这个页面无关** —— 别人触发的、
     或者自己刷新过页面的，一进来都得能看到它还在跑，所以初始就拉一次，
     running 期间每 2 秒轮询，跑完再把结果文件重新读一遍。 */
  const [run, setRun] = useState<EvalRunState | null>(null)
  const [runError, setRunError] = useState('')
  /** 「这件事要登录」与「这件事出错了」分开存。前者不是故障，画成红色
   *  故障条会让人去查一个不存在的事故 —— 与 api.ts 里 LoginRequired 同一个理由。 */
  const [runBlocked, setRunBlocked] = useState('')
  useEffect(() => {
    let alive = true
    let timer = 0
    const poll = () => {
      fetchEvalRun()
        .then(v => {
          if (!alive) return
          setRun(prev => {
            // 从 running 变成终态的那一刻，把 /api/eval 重新拉一遍 ——
            // 成绩就在这一刻变了，不重拉的话页面上还是上一轮的数字
            if (prev?.status === 'running' && v.status !== 'running') setReload(n => n + 1)
            return v
          })
          if (v.status === 'running') timer = window.setTimeout(poll, 2000)
        })
        .catch(() => {})
    }
    poll()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [])

  const running = run?.status === 'running'
  /* 这次部署跑不了回归时的理由（空串 = 跑得了）。后端在 GET /api/eval/run 上
     回它 —— 对外实例的镜像不带回放器，那上面这个按钮点一次失败一次。
     字段缺失（老后端）当作跑得了：宁可点下去由 501 如实报，也不要凭猜置灰。 */
  const unavailable = run && run.available === false ? (run.unavailable_reason || '本次部署不能跑回归评测') : ''

  const triggerRun = () => {
    setRunError('')
    setRunBlocked('')
    startEvalRun()
      .then(v => {
        setRun(v)
        // 起完立刻进入轮询：这里不等下一次 effect，按钮要马上变成"跑第 0/17 题"
        const poll = () => fetchEvalRun().then(x => {
          setRun(prev => {
            if (prev?.status === 'running' && x.status !== 'running') setReload(n => n + 1)
            return x
          })
          if (x.status === 'running') window.setTimeout(poll, 2000)
        }).catch(() => {})
        window.setTimeout(poll, 2000)
      })
      .catch(e => {
        // 会话在页面开着的时候失效（票过期、实例把 auth.required 打开了）：
        // 外壳已经把登录页摆出来了，这里再画一条红条纯属噪音
        if (e instanceof LoginRequired) setRunBlocked(String(e.message || e))
        else setRunError(String(e.message || e))
      })
  }

  // 线上指标随时间窗重取；离线回归是跑出来的文件，不随窗口变
  useEffect(() => {
    let alive = true
    setBusy(true)
    fetchLiveQuality(days)
      .then(v => { if (alive) { setLive(v); setError(''); setFetchedAt(Date.now()) } })
      .catch(e => { if (alive) setError(String(e.message || e)) })
      .finally(() => { if (alive) setBusy(false) })
    return () => { alive = false }
  }, [days, reload])

  useEffect(() => {
    let alive = true
    fetchOfflineQuality()
      .then(v => { if (alive) setOffline(v) })
      .catch(() => {})
    return () => { alive = false }
  }, [reload])

  /* 「稳定性」里的重试恢复率与断点恢复率取线上审计，**窗口固定 30 天**，
     不跟上面那个时间窗走。中断与重试是稀有故障事件：24 小时窗口里通常一条
     样本都没有，跟着走会让这两格常年空着，读起来像功能坏了。窗口写在角标上
     （线上 30D），与页面其余按窗口走的数字区分开。 */
  const [live30, setLive30] = useState<LiveQuality | null>(null)
  useEffect(() => {
    let alive = true
    fetchLiveQuality(30)
      .then(v => { if (alive) setLive30(v) })
      .catch(() => {})
    return () => { alive = false }
  }, [reload])

  return (
    <div className="page">
      <PageHeader
        title="Agent 质量中心"
        description="持续观测当前生产 Agent 的运行健康、结果质量、安全与成本，并用离线回归验证版本变更。"
        action={
          /* 工具条跟着范围切换，规则照设计稿 activateEvaluationScope()：
              时间窗口只在「运行总览 / 线上质量」下出现（另两屏的数不按窗口算，
              留着会让人以为切了窗口离线成绩也会变）；「评测集」下整个按钮消失
              —— 那一屏是题库本身，没有可刷新的运行结果。
              设计稿在「离线回归」下是「▶ 运行回归评测」，askdb 的回归由 CLI 跑
              （见评测集文件旁的 replay），页面上没有触发入口，所以这里是刷新
              已有结果，不做一个点了不跑的按钮。 */
          <div className="eval-toolbar">
            {(scope === 'runtime' || scope === 'online') && (
              <select
                aria-label="选择线上统计时间范围"
                value={days}
                onChange={e => setDays(Number(e.target.value))}
              >
                <option value={1}>最近 24 小时</option>
                <option value={7}>最近 7 天</option>
                <option value={30}>最近 30 天</option>
              </select>
            )}
            {/* 评测版本选择器。只有当前版本可选：结果文件里只有一套成绩，
                选中 v2.3 页面上的数字**不会变** —— 那比没有这个控件更误导，
                所以另两项禁用，形状照留。 */}
            {(scope === 'offline' || scope === 'dataset') && (
              <select aria-label="选择评测版本"
                      value={EVAL_VERSION_OPTIONS[0].label} onChange={() => {}}>
                {EVAL_VERSION_OPTIONS.map(v => (
                  <option key={v.label} value={v.label} disabled={v.disabled}
                          title={v.disabled ? '结果文件里只有当前版本的成绩' : undefined}>
                    {v.label}
                  </option>
                ))}
              </select>
            )}
            {scope !== 'dataset' && (
              <button className="primary" type="button"
                      disabled={scope === 'offline' ? (running || !guard.can || unavailable !== '') : busy}
                      onClick={scope === 'offline' ? triggerRun : () => setReload(n => n + 1)}
                      title={scope !== 'offline'
                        ? undefined
                        : !guard.can
                          ? guard.props.title
                          : unavailable
                            ? unavailable
                            : run?.datasource
                              ? `固定跑在「${run.datasource}」上 —— 换库成绩就不可比`
                              : undefined}>
                {scope === 'offline'
                  ? (running
                      ? `正在跑 ${run?.done ?? 0} / ${run?.total ?? 0} 题…`
                      : ACTION_LABEL.offline)
                  : (busy ? BUSY_LABEL[scope] : ACTION_LABEL[scope])}
              </button>
            )}
          </div>
        }
      />

      <div className="eval-scope-tabs" role="tablist" aria-label="Agent 质量范围">
        {SCOPES.map(item => (
          <button
            className={`eval-scope-tab ${scope === item.key ? 'active' : ''}`}
            type="button" role="tab" aria-selected={scope === item.key}
            key={item.key}
            onClick={() => setScope(item.key)}
          >
            <i className="eval-scope-icon">{item.icon}</i>
            <span><strong>{item.title}</strong><small>{item.sub}</small></span>
            <code>{scopeTag(item.key, live, offline)}</code>
          </button>
        ))}
      </div>

      {/* 上下文条照原型：运行总览不显示，其余三个范围各说各的出处。
          目前只有「线上质量」接了真实出处，其余两个仍走各自面板里的说明。 */}
      {scope === 'online' && live && (
        <div className="eval-context">
          <div className="eval-context-copy">
            <i className="eval-context-mark">LIVE</i>
            <div>
              <strong>生产运行质量 · 真实调用</strong>
              <small>
                {live.runs.toLocaleString()} 个任务 ·{' '}
                {(live.tools?.calls ?? 0).toLocaleString()} 次工具调用 ·
                数据来自本实例审计记录里的节点级 trace
              </small>
            </div>
          </div>
          <div className="eval-context-meta">
            <span>数据更新 <b>{sinceText(fetchedAt)}</b></span>
            <span>统计窗口 <b>{live.days === 1 ? '24 小时' : `${live.days} 天`}</b></span>
            <span className="status">LIVE</span>
          </div>
        </div>
      )}

      {error && <div className="audit-error">读取质量数据失败：{error}</div>}
      {runBlocked && (
        <div className="eval-login-required">
          <span>{runBlocked}</span>
          {onOpenLogin && <button className="secondary" type="button" onClick={onOpenLogin}>去登录</button>}
        </div>
      )}
      {runError && <div className="audit-error">回归没能开跑：{runError}</div>}
      {run?.status === 'failed' && (
        <div className="audit-error">上一轮回归中断：{run.error || '未知原因'}</div>
      )}
      {scope === 'runtime' && <RuntimeScope live={live} offline={offline} days={days} />}
      {scope === 'online' && <OnlineScope live={live} days={days} onNavigate={onNavigate} />}
      {scope === 'offline' && (
        <OfflineScope category={category} onCategory={setCategory}
                      onDataset={() => setScope('dataset')} offline={offline}
                      live={live} live30={live30} onNavigate={onNavigate} />
      )}
      {scope === 'dataset' && <DatasetScope offline={offline} />}
    </div>
  )
}

/** 运行总览 —— 版式与字段位置严格照原型 #qualityRuntimeOverview：
 *  左侧一个 0–100 的健康分环 + 三枚判据药丸，右侧四格
 *  「当前生产版本 / 实际任务量 / 当前告警 / 最新离线回归」。
 *
 *  与原型的唯一区别在**数字的来源**：原型里 97.6、Agent v2.4、4,286 是设计稿
 *  写死的，这里每一个都由本实例的真实审计记录算出来。两处例外必须让人看见，
 *  否则就成了"看起来精确的编造"：
 *    · 健康分的权重（任务 60% / 工具 40%）与告警阈值是**本项目设定的策略**，
 *      不是测量值 —— 与离线门禁的 policy_note 同理，写在卡片说明里。
 *    · 「当前生产版本」的版本号照原型写死（见 AGENT_VERSION），日期与天数用真值。
 */

type Alert = { level: '高优先级' | '中优先级' | '低优先级'; text: string; why?: string }

/** 告警由真实指标按固定阈值判出来。阈值是策略，判定过程不是 —— 每条都写明依据。 */
function alertsOf(live: LiveQuality): Alert[] {
  const out: Alert[] = []
  if (live.failed > 0) out.push({ level: '高优先级', text: `执行失败 ${live.failed} 次（数据源或模型调用）` })
  if (live.security_events > 0) out.push({ level: '高优先级', text: `护栏安全事件 ${live.security_events} 次（R-xx 规则命中）` })
  for (const n of live.nodes) {
    if ((n.success_rate ?? 1) < 0.95) {
      out.push({ level: '中优先级', text: `${STEP_NAMES[n.step] ?? n.step} 成功率 ${pct(n.success_rate)}，低于 95%` })
    }
  }
  out.push(...latencyAlerts(live))
  return out
}

/** 当前窗口怎么称呼。设计稿写的是「最近 24 小时」，而窗口是可切的 ——
 *  选了 7 天还说 24 小时，就是在报一个没算过的范围。 */
function windowText(days: number): string {
  return days === 1 ? '24 小时' : `${days} 天`
}

/** 上一个等长窗口怎么称呼。窗口长度变了措辞也得跟着变 ——
 *  选了「最近 7 天」还说"较昨日"，是在报一个没算过的对比。 */
function prevLabel(days: number): string {
  return days === 1 ? '较昨日' : days === 7 ? '较上周' : days === 30 ? '较上月' : `较上一个 ${days} 天`
}

/** 延迟告警，一律按设计稿的措辞出环比："生成 SQL P95 较昨日上升 8%"。
 *
 *  只报环比，不报绝对值：18.77s 这个数单看不知道是不是常态 —— 这条链路本来
 *  就有十几秒的调用 —— "较昨日上升 8%"才指向"有东西变了"。绝对阈值那种
 *  "超过 10s 目标"的说法已去掉（产品决定），端到端 P95 的当前值在上面
 *  「端到端耗时」那格里照常看得到，没有被藏起来。
 *
 *  环比的前提是两个窗口都有足够样本。上个窗口只跑了两三次时，P95 就是那
 *  两三次里的最大值，涨跌纯属噪声 —— **这种情况不报**，宁可这一格写"无告警"，
 *  也不拿噪声当信号。
 */
const MIN_COMPARE_CALLS = 5

function latencyAlerts(live: LiveQuality): Alert[] {
  const label = prevLabel(live.days)
  const prev = live.prev

  // 逐节点找涨得最多的那一个 —— 原型报的就是具体某个工具，不是笼统的端到端
  let worst: { step: string; delta: number } | null = null
  if (prev && prev.runs >= MIN_COMPARE_CALLS) {
    for (const n of live.nodes) {
      const before = prev.nodes[n.step]
      if (!before || before.calls < MIN_COMPARE_CALLS || n.calls < MIN_COMPARE_CALLS) continue
      if (!before.p95_ms || !n.p95_ms) continue
      const delta = (n.p95_ms - before.p95_ms) / before.p95_ms
      if (delta >= 0.05 && (!worst || delta > worst.delta)) worst = { step: n.step, delta }
    }
    if (worst) {
      return [{
        level: worst.delta >= 0.3 ? '中优先级' : '低优先级',
        text: `${STEP_NAMES[worst.step] ?? worst.step} P95 ${label}上升 ${Math.round(worst.delta * 100)}%`,
      }]
    }
    // 端到端整体的环比 —— 单个节点都没超阈值，但总耗时可能被多段合力推高
    if (prev.p95_ms && live.p95_ms) {
      const delta = (live.p95_ms - prev.p95_ms) / prev.p95_ms
      if (delta >= 0.05) {
        return [{
          level: delta >= 0.3 ? '中优先级' : '低优先级',
          text: `P95 端到端 ${label}上升 ${Math.round(delta * 100)}%`,
          why: `${fmtMs(prev.p95_ms)} → ${fmtMs(live.p95_ms)}`,
        }]
      }
    }
    return []
  }

  // 没有可比的上一窗口 —— 不报。编不出环比就不编，这一格显示"无告警"。
  return []
}

/** 工具成功率：节点级 ok / calls 的合计。原型那格叫「工具成功」，
 *  askdb 的「工具」就是链路节点（generate_sql / guard / execute …）。 */
function toolSuccess(live: LiveQuality): number | null {
  const calls = live.nodes.reduce((a, n) => a + n.calls, 0)
  if (!calls) return null
  const ok = live.nodes.reduce((a, n) => a + n.calls * (n.success_rate ?? 1), 0)
  return ok / calls
}

/** 健康分 0–100。权重是策略：任务成功 60% + 工具成功 40%，
 *  出现安全事件直接压到 60 以下 —— 安全不该被高成功率平均掉。 */
function healthScore(live: LiveQuality): number | null {
  if (!live.runs) return null
  const tool = toolSuccess(live)
  const base = (live.success_rate ?? 0) * 60 + (tool ?? 1) * 40
  return Math.round((live.security_events > 0 ? Math.min(base, 59.9) : base) * 10) / 10
}

function RuntimeScope({ live, offline, days }: {
  live: LiveQuality | null
  offline: OfflineQuality | null
  days: number
}) {
  if (!live) return <p className="drawer-note">读取运行数据…</p>

  const idle = !live.runs
  const score = healthScore(live)
  const tool = toolSuccess(live)
  const alerts = idle ? [] : alertsOf(live)
  // 上一个等长窗口够不够拿来做环比。不够时"无告警"只代表"没算出异常"，
  // 不代表延迟正常 —— 这个区别要让人看得到，所以下面的小字分两种说法。
  const comparable = (live.prev?.runs ?? 0) >= MIN_COMPARE_CALLS
  const top = alerts[0]
  const band = score == null ? 'IDLE' : score >= 95 ? 'HEALTHY' : score >= 85 ? 'WATCH' : 'DEGRADED'
  const verdict = score == null
    ? '最近没有调用记录'
    : band === 'HEALTHY' ? '当前生产服务运行健康'
    : band === 'WATCH' ? '当前生产服务需要关注'
    : '当前生产服务运行降级'

  // 离线回归那格显示发布门禁总分（原型是 92.9 / 100）。
  // **出处不是当前数据源时必须说出来** —— 拿别的库的成绩当本实例的，比没有成绩更糟。
  const sc = offline?.available ? offline.score : undefined
  const prov = offline?.provenance
  const sameSrc = prov?.matches_current === true

  // 天数与日期取**首条审计记录**。没有任何地方记录部署动作，这是能拿到的
  // 最接近"这套服务从什么时候开始在跑"的真值 —— 措辞按原型写「最近部署于」，
  // 接上真正的部署记录后把这两行换掉即可。
  const sinceDays = live.first_ts
    ? Math.max(1, Math.round((Date.now() - new Date(live.first_ts).getTime()) / 86400000))
    : null
  const sinceDate = live.first_ts ? live.first_ts.slice(0, 10) : ''

  return (
    <section className="quality-overview" aria-label="当前生产 Agent 运行总览">
      <div className="quality-verdict">
        <div className="quality-index"
             title="分数的权重（任务成功 60% / 工具成功 40%，有安全事件则压到 60 以下）与告警阈值是本项目设定的策略，不是测量值">
          {score ?? '—'}<small>/100</small>
        </div>
        <div className="quality-verdict-copy">
          <span>PRODUCTION AGENT · {band}</span>
          <strong>{verdict}</strong>
          {/* 照设计稿原文。分数权重与告警阈值是本项目设定的策略、不是测量值 ——
              这句原先在正文里，为与设计稿一致挪到了健康分环的 title 上（见上）。
              **不能整句删掉**：一个 0–100 的分数不说明权重从哪来，就会被当成测出来的。 */}
          <p>
            基于最近 {windowText(live.days)}真实请求持续计算；
            {idle
              ? '本窗口没有调用记录，没有可报的运行质量。'
              : top
                ? `当前 ${top.level}告警：${top.text}。`
                : '无高优告警，性能、工具调用和安全状态均在正常范围。'}
          </p>
          <div className="quality-gates">
            <span>任务成功 {pct(live.success_rate)}</span>
            <span>工具成功 {pct(tool)}</span>
            <span>安全事件 {idle ? '—' : live.security_events}</span>
          </div>
        </div>
      </div>
      <div className="quality-kpis">
        <div className="quality-kpi">
          <div className="quality-kpi-head">
            <span>当前生产版本</span>
            <code>{sinceDays ? `${sinceDays} DAYS` : '—'}</code>
          </div>
          <strong>{AGENT_VERSION}</strong>
          <small>
            稳定运行 · 最近部署于 {sinceDate || '—'}
          </small>
        </div>
        <div className="quality-kpi">
          <div className="quality-kpi-head"><span>实际任务量</span><code>PROD · {live.days === 1 ? '24H' : `${live.days}D`}</code></div>
          <strong>{live.runs.toLocaleString()}</strong>
          {/* 设计稿这行只有「成功完成 / 中断或失败」两栏，所以护栏拦截与执行失败
              在这里合并显示。两者性质不同（拦截是护栏做对事），拆分挂在 title 上，
              判语区的「安全事件」那枚药丸也仍然单独报。 */}
          <small title={`护栏拦截 ${live.blocked} · 执行失败 ${live.failed}`}>
            {idle
              ? `最近 ${windowText(days)}没有调用`
              : `成功完成 ${live.ok.toLocaleString()} · 中断或失败 ${live.blocked + live.failed}`}
          </small>
        </div>
        <div className="quality-kpi">
          <div className="quality-kpi-head"><span>当前告警</span><code>LIVE</code></div>
          <strong>{idle ? '—' : alerts.length ? `${alerts.length} 个${top!.level}` : '无告警'}</strong>
          <small title={top?.why ?? (comparable ? undefined
            : `上一个等长窗口调用不足 ${MIN_COMPARE_CALLS} 次，延迟环比这一轮算不出来`)}>
            {idle
              ? '无调用可判'
              : top ? top.text
                : comparable ? '失败、节点成功率、延迟环比三项均在阈值内'
                  : '失败与节点成功率正常 · 延迟环比暂无可比窗口'}
          </small>
        </div>
        <div className="quality-kpi">
          <div className="quality-kpi-head"><span>最新离线回归</span><code>辅助验证</code></div>
          <strong>{sc ? `${sc.overall} / 100` : '尚未运行'}</strong>
          <small title={sc && !sameSrc
            ? `这组分数出自 ${prov?.datasource || '另一个数据源'}、模型 ${prov?.model || '未知'}，不是本实例跑的`
            : undefined}>
            {/* 照设计稿原文（产品要求）。**出处对不上时这句是不准的** ——
                这组分数可能出自另一个库、另一个模型，「当前版本」四个字就不成立。
                出处挂在 title 上，离线回归那一屏顶部仍有一条显眼的红色提示，
                信息没丢，但这一格不再自己声明。 */}
            {!sc ? '跑一次黄金集后这里才有数' : '当前版本黄金集结果 · 不是线上统计'}
          </small>
        </div>
      </div>
    </section>
  )
}

/** "数据更新 X 前"。取的是**本页最后一次取数成功**的时刻，
 *  不是服务端算这组数的时刻 —— 两者差一次网络往返，措辞上按前者说。 */
function sinceText(at: number | null): string {
  if (at == null) return '—'
  const sec = Math.max(0, Math.round((Date.now() - at) / 1000))
  if (sec < 10) return '刚刚'
  if (sec < 60) return `${sec} 秒前`
  const min = Math.round(sec / 60)
  return min < 60 ? `${min} 分钟前` : `${Math.round(min / 60)} 小时前`
}

function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—'
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${v}ms`
}

/** 线上质量。版式与字段位置严格照原型 #evalOnlinePanel：
*  一条 LIVE 声明 → 四张带 sparkline 的评分卡 → 三格计量 → 工具健康度 + 质量信号
*  → 线上持续评测闭环。
*
*  与原型的区别只在**数字的来源**，以及三处原型有、本实例还没有的信号：
*    · 节点名照真实链路（traceSteps.STEP_NAMES），不照抄设计稿那五个
*      schema.retrieve / metric.resolve / … —— 那等于凭空造出五个不存在的工具。
*    · 数据来自本实例的审计记录，不是 Langfuse / OpenTelemetry。
*    · 「用户结果采纳率」「用户主动纠错」没有采集来源，显示「—」并写明原因；
*      设计稿上的 94.6% / 0.9% 是稿子里的数，照抄就是编。
*/

/** P95 端到端的目标线。**是本项目设定的策略，不是测量值** ——
*  与 latencyAlerts 里那道绝对阈值同一个数，只写一次。 */
const P95_TARGET_MS = 10000
/** 单任务成本目标（元）。与 P95 目标一样，是**本项目设定的策略、不是测量值** ——
 *  显示成"目标 < ¥0.03"而不是把它混进指标里，就是为了让人看得出这是谁定的。 */
const COST_TARGET_CNY = 0.03
/** 准确性三项的目标线（百分点）。与上面两条同一条纪律：**是按设计稿定下的发布
 *  策略，不是测量值**，所以写成一处常量、在卡上显示成"目标 ≥ 92%"，让人看得出
 *  这个数是谁定的、不是跑出来的。达标与否由真实值和它比出来，不写死颜色。 */
const ACCURACY_TARGETS = { sql: 92, metric: 95, complete: 93 }

/** sparkline 的柱高。等分七段的真实序列 → 0–100 的高度。
*
*  用段内最小值做基线而不是从 0 起：成功率这类数全挤在 95%–100%，
*  从 0 起画出来七根一样高的柱子，趋势就看不见了。空段（没有调用）给 0，
*  它和"有调用但值很低"必须能分得开。
*/
function sparkBars(values: (number | null)[]): number[] {
  const real = values.filter((v): v is number => v != null)
  if (!real.length) return values.map(() => 0)
  const max = Math.max(...real)
  const min = Math.min(...real)
  return values.map(v => {
    if (v == null) return 0
    if (max === min) return 70
    return Math.round(25 + 75 * ((v - min) / (max - min)))
  })
}

/** 环比。两个窗口都得有足够样本，否则不报 —— 与告警那边同一条纪律。 */
function delta(now: number | null | undefined, before: number | null | undefined,
       prevRuns: number): string {
  if (now == null || before == null || !before || prevRuns < MIN_COMPARE_CALLS) return ''
  const d = (now - before) / before
  if (Math.abs(d) < 0.005) return '持平'
  return `${d > 0 ? '↑' : '↓'} ${Math.abs(d * 100).toFixed(1)}%`
}

function OnlineScope({ live, days, onNavigate }: {
  live: LiveQuality | null
  days: number
  onNavigate?: (view: View) => void
}) {
  if (!live) return <p className="drawer-note">读取中…</p>
  if (!live.runs) {
    return (
      <section className="eval-scope-panel active">
        <p className="drawer-note">最近 {days} 天没有调用记录，线上指标无从算起。</p>
      </section>
    )
  }

  const series = live.series ?? []
  const tools = live.tools
  const sql = live.sql
  const retry = live.retry
  const prevRuns = live.prev?.runs ?? 0
  const label = prevLabel(live.days)

  const toolRate = tools && tools.calls ? tools.ok / tools.calls : null
  const sqlRate = sql && sql.calls ? sql.ok / sql.calls : null
  const runsDelta = delta(live.runs, prevRuns, prevRuns)
  const topFail = tools?.top_fail_step
    ? `${STEP_NAMES[tools.top_fail_step] ?? tools.top_fail_step} 占 ${Math.round((tools.top_fail_share ?? 0) * 100)}%`
    : ''

  return (
    <section className="eval-scope-panel active" aria-label="线上质量">
      <div className="eval-note">
        <i>LIVE</i>
        <div>
          <strong>以下指标来自本实例的审计记录，按真实调用统计，不使用黄金集分母</strong>
          <small>
            工具调用、SQL 执行、耗时、Token 与成本直接按实际请求统计；
            线上结果准确性没有天然标准答案，要靠离线回归与抽样人工判断补充。
          </small>
        </div>
      </div>

      <div className="eval-score-grid">
        <ScoreCard
          label="实际任务数"
          tag={`AUDIT · ${live.days === 1 ? '24H' : `${live.days}D`}`}
          value={live.runs.toLocaleString()} unit="RUNS"
          note={runsDelta ? `${runsDelta} ${label}`
            : `上一个窗口调用不足 ${MIN_COMPARE_CALLS} 次，不出环比`}
          bars={sparkBars(series.map(s => s.runs))}
        />
        <ScoreCard
          label="工具调用成功率"
          tag={tools ? `TRACE · ${tools.ok.toLocaleString()} / ${tools.calls.toLocaleString()}` : 'TRACE'}
          value={toolRate == null ? '—' : (toolRate * 100).toFixed(1)} unit="%"
          note={tools && tools.fails
            ? `${tools.fails} 次失败${topFail ? ` · ${topFail}` : ''}`
            : '窗口内没有失败的节点'}
          bars={sparkBars(series.map(s => s.tool_rate))}
        />
        <ScoreCard
          label="SQL 执行成功率"
          tag={sql ? `${sql.ok.toLocaleString()} / ${sql.calls.toLocaleString()}` : '—'}
          value={sqlRate == null ? '—' : (sqlRate * 100).toFixed(1)} unit="%"
          note="不等于结果准确率 —— SQL 跑通了、口径用错了，这里照样是 100%"
          bars={sparkBars(series.map(s => s.sql_rate))}
        />
        <ScoreCard
          label="P95 端到端耗时"
          tag="AUDIT TRACE"
          value={live.p95_ms == null ? '—' : (live.p95_ms / 1000).toFixed(1)} unit="SEC"
          note={`目标 < ${P95_TARGET_MS / 1000} 秒（本项目设定）· ${
            (live.p95_ms ?? 0) > P95_TARGET_MS ? '偏慢' : '正常'}`}
          bars={sparkBars(series.map(s => s.p95_ms))}
        />
      </div>

      <div className="eval-metric-grid">
        <MetricCard
          label="线上平均 Token" value={live.avg_tok?.toLocaleString() ?? '—'}
          note={`${live.runs.toLocaleString()} 个真实任务的模型输入与输出消耗`}
          status={delta(live.avg_tok, live.prev?.avg_tok, prevRuns) || `最近 ${live.days} 天`}
        />
        <MetricCard
          label="线上单任务成本"
          value={live.avg_cost_cny == null ? '—' : `¥${live.avg_cost_cny.toFixed(4)}`}
          note="模型开销，按成功和失败任务共同计算"
          status={delta(live.avg_cost_cny, live.prev?.avg_cost_cny, prevRuns)
            || `合计 ¥${live.cost_cny.toFixed(2)}`}
        />
        <MetricCard
          label="自动重试恢复率"
          value={retry?.rate == null ? '—' : `${(retry.rate * 100).toFixed(1)}%`}
          note={retry && retry.retried
            ? `${retry.retried} 次重试的任务中，${retry.recovered} 次最终完成`
            : '窗口内没有任务触发重试（attempts 均为 1）'}
          status={retry && retry.retried ? 'ATTEMPTS > 1' : '无样本'}
        />
      </div>

      <div className="online-health-grid">
        <article className="eval-card">
          <div className="eval-card-head">
            <div>
              <strong>生产工具健康度</strong>
              <small>按审计记录里的 steps 聚合 · 最近 {live.days} 天</small>
            </div>
            {onNavigate && (
              <button className="secondary" type="button"
                  onClick={() => onNavigate('traces')}>打开执行追踪</button>
            )}
          </div>
          <div className="eval-table-wrap">
            <table>
              <thead>
                <tr><th>工具</th><th className="num">调用次数</th><th className="num">成功率</th>
                  <th className="num">P95</th><th>主要失败原因</th></tr>
              </thead>
              <tbody>
                {live.nodes.map(n => (
                  <tr key={n.step}>
                    <td>{STEP_NAMES[n.step] ?? n.step}</td>
                    <td className="num">{n.calls.toLocaleString()}</td>
                    <td className={`num ${(n.success_rate ?? 1) < 0.95 ? 'eval-fail' : 'eval-pass'}`}>
                      {pct(n.success_rate)}
                    </td>
                    <td className="num">{fmtMs(n.p95_ms)}</td>
                    <td>
                      {/* 原因来自模型自己给的理由，长短不受控（"给定的表 A / B / C 中都没有
                          城市字段…"）。限宽后换行显示，不截断 —— 截断省略号看着干净，
                          但要看清死在什么上就得悬停，等于把主要信息藏起来了。
                          表名这类长串没有空格，overflow-wrap 让它也能断行 */}
                      <span className="eval-fail-reason">
                        {n.fail_reason || '—'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </article>

        <article className="eval-card">
          <div className="eval-card-head">
            <div><strong>线上质量信号</strong><small>没有标准答案时使用代理指标</small></div>
            <span className="status">{live.days === 1 ? '24H' : `${live.days}D`}</span>
          </div>
          <div className="eval-card-body online-signal-list">
            <div className="online-signal">
              <div><strong>用户结果采纳率</strong><small>需要"看完之后有没有重来"的反馈信号，本实例未采集</small></div>
              <span>—</span>
            </div>
            <div className="online-signal">
              <div><strong>人工介入率</strong><small>查询触发审批的比例 · 来自审批流水</small></div>
              <span>{live.intervention?.rate == null ? '—' : pct(live.intervention.rate)}</span>
            </div>
            <div className="online-signal">
              <div>
                <strong>同问题重复查询率</strong>
                <small>
                  同一发起人 {live.repeat?.window_min ?? 10} 分钟内再次提交；
                  匿名调用会被并成一个人，这个数偏高
                </small>
              </div>
              <span>{live.repeat?.rate == null ? '—' : pct(live.repeat.rate)}</span>
            </div>
            <div className="online-signal">
              <div><strong>用户主动纠错</strong><small>没有"结果不准确"的反馈入口，无从统计</small></div>
              <span>—</span>
            </div>
          </div>
        </article>
      </div>

      <article className="eval-card" style={{ marginTop: 10 }}>
        <div className="eval-card-head">
          <div><strong>线上持续评测闭环</strong><small>把真实场景转化为可重复的离线回归资产</small></div>
          <span className="status">未接入</span>
        </div>
        <div className="eval-card-body">
          <div className="online-eval-flow">
            <div className="online-eval-step"><i>01 · SAMPLE</i><strong>生产 Trace 抽样</strong><small>按业务域、风险与异常信号分层抽取</small></div>
            <div className="online-eval-step"><i>02 · CHECK</i><strong>规则 + Judge 初评</strong><small>确定性校验与 LLM-as-Judge 组合评分</small></div>
            <div className="online-eval-step"><i>03 · REVIEW</i><strong>人工复核</strong><small>低置信度与高风险样本由专家确认</small></div>
            <div className="online-eval-step"><i>04 · PROMOTE</i><strong>沉淀黄金集</strong><small>确认无误的样本进入黄金集下一版</small></div>
          </div>
        </div>
      </article>
    </section>
  )
}


function OfflineScope({ category, onCategory, onDataset, offline, live, live30, onNavigate }: {
  category: Category
  onCategory: (c: Category) => void
  onDataset: () => void
  offline: OfflineQuality | null
  /** 性能页的阶段拆解取线上真实调用 —— 离线样本量撑不起分位数 */
  live: LiveQuality | null
  /** 稳定性页专用的固定 30 天窗口。中断与重试太稀疏，24 小时窗口取不到样本 */
  live30: LiveQuality | null
  /** 「待改进样本」右上角的「查看失败 Trace」要跳执行追踪页 */
  onNavigate?: (view: View) => void
}) {
  if (!offline) return <p className="drawer-note">读取离线回归结果…</p>
  if (!offline.available) {
    return (
      <section className="eval-scope-panel active">
        <div className="eval-note">
          <i>OFF</i>
          <div>
            <strong>尚未跑过离线回归</strong>
            <small>
              这一页的每个数字都来自 evals/results/ 下的结果文件。
              没跑过就没有结果 —— 不会拿线上统计冒充，也不会显示 0。
            </small>
          </div>
        </div>
        <pre className="sql-code">python -m evals.golden -c config/askdb.yaml</pre>
      </section>
    )
  }

  return (
    <section className="eval-scope-panel active" aria-label="离线回归">
      <OfflineContext d={offline} />
      <div className="eval-tabs" role="tablist">
        {CATEGORIES.map(item => (
          <button
            className={`eval-tab ${category === item.key ? 'active' : ''}`}
            type="button" role="tab" aria-selected={category === item.key}
            key={item.key} onClick={() => onCategory(item.key)}
          >{item.label}</button>
        ))}
      </div>

      {category === 'overview' && <OverviewPanel d={offline} onDataset={onDataset} />}
      {category === 'accuracy' && <AccuracyPanel d={offline} onNavigate={onNavigate} />}
      {category === 'security' && <SecurityPanel d={offline} />}
      {category === 'stability' && <StabilityPanel d={offline} live={live30} />}
      {category === 'performance' && <PerformancePanel d={offline} live={live} />}
    </section>
  )
}

/** 本轮回归的抬头。版式照原型 `#evalContext`：左边一枚标记 + 标题与构成，
 *  右边是最近评测时间、模型与状态角标。
 *
 *  原型标题写的是「核心问数黄金集 · V12」、副标题「126 个问题 · 6 类场景」——
 *  数字与版本号都照真值来：评测集没有版本号，题数与场景数按黄金集文件算。
 *
 *  **出处仍然是这张卡最重要的一件事**：同一份代码会部署成多个实例，
 *  拿别的库跑出来的成绩当本实例的，比没有成绩更糟。所以数据源不一致时，
 *  右侧角标从 READY 变成「换库需重跑」，副标题直接写明跑于哪、现在连的是哪。
 */
function OfflineContext({ d }: { d: OfflineQuality }) {
  const p = d.provenance
  const g = d.golden
  const same = p?.matches_current === true
  const cats = Object.keys(g?.by_category ?? {}).length
  const total = g?.total ?? d.blind?.n ?? 0

  return (
    <div className="eval-context">
      <div className="eval-context-copy">
        <i className="eval-context-mark">QA</i>
        <div>
          <strong>黄金评测集 · {g?.path?.split('/').pop() || '未知文件'}</strong>
          <small>
            {total} 个问题{cats ? ` · ${cats} 类场景` : ''} · 真实模型与工具执行
            {!same && p && (
              <> · ⚠ 跑于 {p.datasource || '—'}，当前连的是 {p.current_datasource || '—'}，
                这组分数不能代表本实例</>
            )}
          </small>
        </div>
      </div>
      <div className="eval-context-meta">
        <span>最近评测 <b>{d.ran_at ? fmtRunTime(d.ran_at) : '—'}</b></span>
        <span>模型 <b>{p?.model || '—'}</b></span>
        <span className={`status ${same ? '' : 'bad'}`}>{same ? 'READY' : '换库需重跑'}</span>
      </div>
    </div>
  )
}

/** 评测时间的短写法，照原型的「今天 17:40」。跨天的显示 MM-DD HH:MM ——
 *  只写时分会让上周跑的那轮看起来像刚跑完。 */
function fmtRunTime(iso: string): string {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return iso.slice(0, 16).replace('T', ' ')
  const hm = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`
  const now = new Date()
  const sameDay = t.toDateString() === now.toDateString()
  return sameDay
    ? `今天 ${hm}`
    : `${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')} ${hm}`
}

function OverviewPanel({ d, onDataset }: { d: OfflineQuality; onDataset: () => void }) {
  const b = d.blind!
  const sc = d.score
  const passed = Math.round(b.accuracy * b.n)
  const runs = d.runs ?? []
  const linkFail = b.failure_kinds?.['链路失败'] ?? 0

  return (
    <>
      {/* 四张卡的标签与版式照原型。数值全部来自本轮结果；
          右上角 code 位改成写**这个数是怎么来的**，而不是原型里的
          "118 / 126"这类写死值。 */}
      <div className="eval-score-grid">
        <ScoreCard label="离线质量分" tag={`发布门禁 ≥ ${sc?.gate ?? 90}`}
                   value={sc ? String(sc.overall) : '—'} unit="/ 100"
                   note={sc ? (sc.pass ? '达到发布门禁' : `距门禁还差 ${(sc.gate - sc.overall).toFixed(1)}`) : ''}
                   bars={sc ? sc.dimensions.map(x => x.value) : []} />
        <ScoreCard label="任务成功率" tag={`${passed} / ${b.n}`}
                   value={(b.accuracy * 100).toFixed(1)} unit="%"
                   note={`${b.n - passed} 个失败样本`}
                   bars={[]} />
        <ScoreCard label="结果准确率" tag="RESULT MATCH"
                   value={(b.accuracy * 100).toFixed(1)} unit="%"
                   note="按执行结果判定，等价 SQL 会通过比对"
                   bars={[]} />
        <ScoreCard label="链路完成率" tag={`离线 · ${b.n - linkFail} / ${b.n}`}
                   value={((1 - linkFail / Math.max(b.n, 1)) * 100).toFixed(1)} unit="%"
                   note={linkFail ? `${linkFail} 次链路失败` : '无链路失败'}
                   bars={[]} />
      </div>

      <div className="eval-two-col">
        <article className="eval-card">
          <div className="eval-card-head">
            <div>
              <strong>离线发布门禁</strong>
              <small>仅用于判断候选版本能否上线，不代表生产运行健康</small>
            </div>
            {sc && (
              <span className={`status ${sc.pass ? '' : 'bad'}`}>
                {sc.pass ? '允许发布' : '未达门禁'}
              </span>
            )}
          </div>
          {sc && (
            <div className="eval-card-body">
              {sc.dimensions.map(x => (
                <Dimension key={x.key}
                           label={`${x.label} · ${(x.weight * 100).toFixed(0)}%`}
                           pct={x.value} value={x.value.toFixed(1)} />
              ))}
            </div>
          )}
          {/* 这句必须显示：把策略当测量，是这一页最容易骗人的地方 */}
          {sc && (
            <div className="eval-card-body eval-gate-note">
              <small>
                {sc.policy_note}
                各维度来源 —— {sc.dimensions.map(x => `${x.label}：${x.source}`).join('；')}。
              </small>
            </div>
          )}
        </article>

        <article className="eval-card">
          <div className="eval-card-head">
            <div><strong>最近回归记录</strong><small>同一数据源下的历次结果</small></div>
            <button className="ghost" type="button" onClick={onDataset}>查看评测集</button>
          </div>
          {runs.length ? (
            <div className="eval-card-body">
              {runs.map((r, i) => (
                <div className="eval-run" key={r.file}>
                  {/* 原型这里是版本号（2.4 / 2.3 / 2.2）。结果文件不记 Agent 版本，
                      askdb 也没有别的地方记它 —— 编三行版本号就是编，改成轮次序号 */}
                  <i className="eval-run-id">{String(runs.length - i).padStart(2, '0')}</i>
                  <div>
                    <strong>{r.file.replace(/\.json$/, '')}{r.current ? ' · 本轮' : ''}</strong>
                    <small>{r.n} CASES · {fmtRunTime(r.ran_at)}</small>
                  </div>
                  <span className={`eval-run-score ${r.pass ? 'eval-pass' : 'eval-fail'}`}>
                    {r.overall} {r.pass ? 'PASS' : 'FAIL'}
                  </span>
                </div>
              ))}
            </div>
          ) : <p className="drawer-note">只有本轮结果，没有可比的历史记录。</p>}
          {d.golden && (
            <div className="eval-card-body eval-gate-note">
              <small>
                评测集全集 {d.golden.total} 条，本轮盲测实跑 {d.golden.blind_n} 条 ——
                两个数一起看才不会误判覆盖面。历次结果按同一套门禁权重现算，
                时间取结果文件的最后写入时间。
              </small>
            </div>
          )}
        </article>
      </div>
    </>
  )
}

/** 每种失败原因对应的**判定标准**，也就是这条题"本该怎样"。
 *
 *  判分器只在「结果不一致」这一种情形下写出结构化的 `期望 X，实得 Y`；链路失败、
 *  被护栏拦截这些在到达结果比对之前就返回了，detail 里只有错误串，没有可比的期望
 *  值。但"本该怎样"是**判定规则本身**，不是猜出来的数 —— 照 evals/replay.py 里
 *  各分支的判据逐条写在这里，比留一列「—」有用。改判据时这张表要跟着改。 */
const EXPECTED_BY_REASON: Record<string, string> = {
  链路失败: '能生成可执行 SQL',
  被护栏拦截: '不触发护栏',
  应拒未拒: '该拒即拒',
  拦截规则不符: '命中预期的护栏规则',
  行数超出预期区间: '落在预期行数区间',
  标准答案不可用: '标准答案本身可执行',
  配额拒绝: '不受配额影响',
}

/** 失败明细拆成原型那两列（预期 / 实际）。 */
function splitDetail(reason: string, detail: string): [string, string] {
  const m = /^期望\s*(.+?)\s*，\s*实得\s*(.+?)\s*$/.exec(detail || '')
  if (!m) return [EXPECTED_BY_REASON[reason] ?? '—', detail || '—']
  // 判分器这句只比了**行数**。行数相同却判不通过，说明差在内容上 ——
  // 两列写同一个数会看起来像"预期等于实际却算失败"，这里把差在哪写明
  return m[1] === m[2] ? [m[1], `${m[2]}（内容不一致）`] : [m[1], m[2]]
}

/** 准确性。版式照原型 [data-eval-panel="accuracy"]：判定说明 + 三张指标卡 +
 *  「待改进样本」。
 *
 *  三项里只有 SQL 准确率 askdb 真的在算 —— 业务口径命中率要逐条比对命中的口径
 *  有没有真的进最终 SQL，回答忠实度要判断结论能否由结果集完整支撑，两者都缺判定
 *  器。这两格按原型的卡片形状占位（值「—」、角标走待办色），不拿设计稿里的
 *  96.1% / 91.7% 充数：这一页是用来判断能不能发布的，在这里编数字的后果比别处
 *  都严重。
 *
 *  「节点」一列同理 —— 失败样本记了 trace_id，但没有记栽在哪个节点，整列占位，
 *  由「查看失败 Trace」把人送进执行追踪去看。 */
function AccuracyPanel({ d, onNavigate }: {
  d: OfflineQuality
  onNavigate?: (view: View) => void
}) {
  const b = d.blind!
  const passed = Math.round(b.accuracy * b.n)
  const failures = d.failures ?? []
  return (
    <>
      <div className="eval-note">
        <i>≠</i>
        <div>
          <strong>准确率按执行结果判定，不要求 SQL 字符串完全相同</strong>
          <small>
            等价 SQL 会通过结果比对；判定同时看返回列与行数约束是否符合预期。
          </small>
        </div>
      </div>
      <div className="eval-metric-grid">
        <MetricCard label="SQL 准确率" value={pct(b.accuracy)}
                    note={`${b.n} 条盲测用例中，${passed} 条结果与标准答案一致`}
                    status={`目标 ≥ ${ACCURACY_TARGETS.sql}%`}
                    wait={b.accuracy * 100 < ACCURACY_TARGETS.sql} />
        <MetricCard label="业务口径命中率" value={pct(b.metric_hit_rate)}
                    note={b.metric_graded_n
                      ? `注入认证口径的 ${b.metric_graded_n} 条题里，`
                        + `${Math.round((b.metric_hit_rate ?? 0) * b.metric_graded_n)} `
                        + '条最终 SQL 真的用上了定义式'
                      : '认证口径被正确引用'}
                    status={`目标 ≥ ${ACCURACY_TARGETS.metric}%`}
                    wait={(b.metric_hit_rate ?? 0) * 100 < ACCURACY_TARGETS.metric} />
        {/* 原型这格是「回答忠实度」（答案结论可由查询结果完整支撑）。askdb 的链路里
            没有 summarize 节点、AskResult 里也没有任何自然语言字段 —— 它交回去的是
            SQL 与表格，不写结论，"结论超出结果集"这件事无从谈起。同一层意思在这个
            产品形态下能测的是反过来的一问：交回去的结果本身够不够作答。 */}
        <MetricCard label="结果完整度" value={pct(b.completeness)}
                    note={b.complete_graded_n
                      ? `${b.complete_graded_n} 条跑出结果的题里，`
                        + `${Math.round((b.completeness ?? 0) * b.complete_graded_n)} `
                        + '条结果可直接作答 —— 未被 LIMIT 截断、未提前收敛、召回非盲选'
                      : '结果集可直接作答，未被截断、未提前收敛、召回非盲选'}
                    status={`目标 ≥ ${ACCURACY_TARGETS.complete}%`}
                    wait={(b.completeness ?? 0) * 100 < ACCURACY_TARGETS.complete} />
      </div>

      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>待改进样本</strong>
            <small>按错误类型聚类，点击 Trace 可定位具体节点</small>
          </div>
          {onNavigate && (
            <button className="secondary" type="button" onClick={() => onNavigate('traces')}>
              查看失败 Trace
            </button>
          )}
        </div>
        {failures.length ? (
          <div className="eval-table-wrap">
            <table>
              <thead><tr><th>评测问题</th><th>错误类型</th><th>预期</th>
                         <th>实际</th><th>节点</th></tr></thead>
              <tbody>
                {failures.map(f => {
                  const [want, got] = splitDetail(f.reason, f.detail)
                  return (
                    <tr key={f.id}>
                      <td className="eval-case-question" title={f.id}>{f.question || f.id}</td>
                      <td><span className="status wait">{f.reason || '—'}</span></td>
                      <td>{want}</td>
                      <td className="eval-case-question">{got}</td>
                      <td>—</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="eval-card-body">
            <p className="drawer-note">本轮盲测没有失败样本。</p>
          </div>
        )}
      </article>
    </>
  )
}

/** 原型「安全场景覆盖」的四类场景。评测集目前只有一类笼统的 reject 用例，
 *  分不出这四格各自跑了多少 —— 四行一起占位，不把 reject 拆着填。 */
/** 安全场景覆盖的四行。key 与 evals/golden_ragforge.py 里的 scene 一一对应 ——
 *  两处分开写，就会出现"页面上有这一行、评测里没有这类题"，而那正是这张卡
 *  最不该出现的错。顺序照原型。 */
const SECURITY_SCENARIOS: { key: string; label: string }[] = [
  { key: 'write_ddl', label: '写入与 DDL' },
  { key: 'escalation', label: '跨角色越权' },
  { key: 'sensitive', label: '敏感信息' },
  { key: 'injection', label: '提示注入' },
]

/** 安全合规。版式、指标名、指标描述与目标全部照原型
 *  [data-eval-panel="security"]，三项指标各自有真实分母：
 *
 *    危险 SQL 拦截率 = 写入 / DDL / 绕过变体里被拦下的比例
 *    越权率         = 跨租户、跨库用例里真的取回了越界数据的比例
 *    敏感数据泄漏率 = 个人信息用例里返回了明文的比例
 *
 *  三个数都由 evals/replay.py 按链路自己记下的事实判定（rejected_by /
 *  rules_fired / masked_columns 与返回值本身），不经模型判分。
 *
 *  分母为 0 时后端给 null，这里显示「—·未覆盖」而不是 0% —— 一道题都没考
 *  与一次都没漏，在这一页上是完全相反的两件事。 */
function SecurityPanel({ d }: { d: OfflineQuality }) {
  const b = d.blind!
  const scenes = b.scenes ?? {}
  // 分子分母从本轮结果现算，用来写卡片下面那行说明 —— 原型那句
  // "28 / 28 个 UPDATE、DELETE、DDL 与绕过变体已拦截"里的两个数，
  // 必须是本轮真实的题数，不能是常量。
  const tally = (key: string) => scenes[key] ?? [0, 0]
  const [wOk, wN] = tally('write_ddl')
  const [eOk, eN] = tally('escalation')
  const [pOk, pN] = tally('sensitive')
  // 角标取四行之和，不取评测集里的安全题总数：判不动的题（例如跨租户题
  // 跑在没有租户维度的源上）不进任何一行，角标写总数就会比四行加起来大，
  // 那个差额没有任何地方解释得了。
  const total = SECURITY_SCENARIOS.reduce((n, sc) => n + tally(sc.key)[1], 0)
  return (
    <>
      <div className="eval-note">
        <i>盾</i>
        <div>
          <strong>安全指标采用红线门禁</strong>
          <small>
            敏感数据泄漏或未拦截高危写入是红线，不用综合高分抵消 ——
            一次放行危险 SQL，比准确率低几个点严重得多。
          </small>
        </div>
      </div>
      <div className="eval-metric-grid">
        <MetricCard
          label="危险 SQL 拦截率"
          value={b.danger_block_rate == null ? '—' : pct(b.danger_block_rate)}
          note={wN
            ? `${wOk} / ${wN} 个 UPDATE、DELETE、DDL 与绕过变体已拦截`
            : '本轮没有写入类用例'}
          status={b.danger_block_rate == null ? '未覆盖'
            : b.danger_block_rate < 1 ? '红线未过' : '红线通过'}
          wait={b.danger_block_rate == null || b.danger_block_rate < 1}
          danger={b.danger_block_rate != null && b.danger_block_rate < 1} />
        <MetricCard
          label="越权率"
          value={b.escalation_rate == null ? '—' : pct(b.escalation_rate)}
          note={eN
            ? `${eN - eOk} / ${eN} 个跨角色、跨数据域测试发生越权访问`
            : '本轮没有越权用例'}
          status={b.escalation_rate == null ? '未覆盖' : '目标 = 0'}
          wait={b.escalation_rate == null}
          danger={!!b.escalation_rate} />
        <MetricCard
          label="敏感数据泄漏率"
          value={b.leak_rate == null ? '—' : pct(b.leak_rate)}
          note={pN
            ? `手机号、证件号、地址等字段 ${pOk} / ${pN} 完成阻断或脱敏`
            : '本轮没有个人信息用例'}
          status={b.leak_rate == null ? '未覆盖' : '目标 = 0'}
          wait={b.leak_rate == null}
          danger={!!b.leak_rate} />
      </div>
      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>安全场景覆盖</strong>
            <small>不仅测试关键词，还包含 SQL 变体、提示注入与权限边界</small>
          </div>
          <span className={`status ${total ? '' : 'wait'}`}>
            {total ? `${total} CASES` : '待接入'}
          </span>
        </div>
        <div className="eval-card-body">
          {SECURITY_SCENARIOS.map(sc => {
            const [ok, n] = tally(sc.key)
            return (
              <Dimension key={sc.key} label={sc.label}
                         pct={n ? Math.round(ok / n * 100) : 0}
                         value={n ? `${ok}/${n}` : '—'} />
            )
          })}
        </div>
      </article>
    </>
  )
}

/* 稳定性。版式严格照原型（trusted-data-agent-prototype.html
 * [data-eval-panel="stability"]）：三张指标卡 + 「故障注入结果 / 恢复原则」两栏。
 *
 * 版式照搬，数字不照搬 —— 原型里的 88.5% / 11 12 是设计稿的示意值。
 * 三格各有各的出处，角标必须写明，不然三个数会被一起读成同一轮离线成绩：
 *   · 执行成功率 = 离线盲测的非链路失败比例（后端 score.stability）
 *   · 重试恢复率 = 线上审计里 attempts>1 的任务最终完成的比例
 *   · 断点恢复率 = 线上审计里断过的线程最终正常收尾的比例
 * 后两项在离线回归里没有样本、也没有故障注入去造，但线上是真发生过就有 ——
 * 所以取审计而不是标"未测量"；分母为 0 时报「无样本」，那与 0% 不是一回事。
 * 线上执行成功率在「运行总览」里有（成功完成 / 中断或失败），这里不再多占一格 ——
 * 原型这排就是三格，多出来的第四格会掉到下一行，正是版式对不上的地方。
 *
 * 「恢复原则」是**行为说明**不是测量值，三条的标题、顺序、描述全照原型 ——
 * 承载真假的是右侧那一列状态，不是描述：三条都是 askdb 实际做到的 —— 检查点里存着
 * question / org_id / schema_prompt 与各节点产物；续跑前重验可见范围、数据源连接与
 * 表结构（graph.precheck_resume），任一不过就不续跑；已完成的模型调用不再打。
 *
 * 一处措辞取舍：03 的"不重复计费"按**成本**说是真的（完成的调用不重跑，token
 * 不再花），askdb 与它的差别在**每日配额** —— 续跑另计一次。这句由续跑横幅在
 * 真要花钱的地方说，不塞进这张说明卡。
 */
/** 没有注入结果时占位用的三类故障。顺序与 evals/chaos.py 的 FAULTS 一致 ——
 *  两处分开写就会出现"页面上有的类别评测里没跑"。 */
const FAULT_SLOTS = [
  { key: 'db_timeout', label: '数据库超时' },
  { key: 'llm_rate_limit', label: '模型限流' },
  { key: 'schema_drift', label: 'Schema 漂移' },
] as const

function StabilityPanel({ d, live }: { d: OfflineQuality; live: LiveQuality | null }) {
  const b = d.blind!
  // 不在前端重推一遍 —— 后端 /api/eval 的 score.stability 已经按
  // outcomes 里的"链路失败"算过。两处各算各的迟早会对不上。
  const dim = d.score?.dimensions.find(x => x.key === 'stability')
  // 后两格取**线上审计**：重试与中断都是运行时才发生的事，离线回归里没有样本，
  // 也没有故障注入去造。分母为 0 时报「无样本」而不是 0% —— 窗口内一次没断过
  // 与"断了都没恢复"是两回事。角标写明出处，免得被读成离线成绩。
  const retry = live?.retry
  const resume = live?.resume
  const win = live ? (live.days === 1 ? '24H' : `${live.days}D`) : '—'
  const chaos = d.chaos
  return (
    <>
      <div className="eval-metric-grid">
        <MetricCard label="执行成功率" value={dim ? `${dim.value}%` : '—'}
                    note="数据库、模型与策略节点整体执行成功"
                    status={dim?.source ?? `N=${b.n}`} />
        <MetricCard label="重试恢复率"
                    value={retry?.rate == null ? '—' : pct(retry.rate)}
                    note="连接超时、限流等瞬时故障自动恢复成功"
                    wait={!retry?.retried}
                    status={retry?.retried
                      ? `线上 ${win} · ${retry.recovered}/${retry.retried}`
                      : `线上 ${win} · 无样本`} />
        <MetricCard label="断点恢复率"
                    value={resume?.rate == null ? '—' : pct(resume.rate)}
                    note="人工补充或审批后从 CHECKPOINT 精确续跑"
                    wait={!resume?.interrupted}
                    status={resume?.interrupted
                      ? `线上 ${win} · ${resume.recovered}/${resume.interrupted}`
                      : `线上 ${win} · 无样本`} />
      </div>

      <div className="eval-two-col">
        <article className="eval-card">
          <div className="eval-card-head">
            <div>
              <strong>故障注入结果</strong>
              <small>模拟真实依赖异常验证恢复能力</small>
            </div>
            {/* 角标是**样本量**不是口号：分母怎么来的要能一眼看见。
                没跑过就写未测量 —— 三行空条比一组来历不明的数字诚实。 */}
            {chaos
              ? <span className={`status ${chaos.matches_current ? '' : 'wait'}`}
                      title={chaos.matches_current ? undefined
                        : `这一轮跑在 ${chaos.datasource}，不是当前连接的库`}>
                  {chaos.matches_current ? '' : '其他数据源 · '}{chaos.n_cases} CASES
                </span>
              : <span className="status wait">未测量</span>}
          </div>
          <div className="eval-card-body">
            {(chaos?.faults ?? FAULT_SLOTS).map(f => (
              <Dimension key={f.key} label={f.label}
                         pct={'rate' in f && f.rate != null ? Math.round(f.rate * 100) : 0}
                         value={'injected' in f && f.injected
                           ? `${f.recovered}/${f.injected}` : '—'} />
            ))}
          </div>
        </article>

        <article className="eval-card">
          <div className="eval-card-head">
            <div>
              <strong>恢复原则</strong>
              <small>失败不等于从头重跑</small>
            </div>
          </div>
          <div className="eval-card-body">
            <div className="eval-run">
              <span className="eval-run-id">01</span>
              <div>
                <strong>保存最小任务状态</strong>
                <small>意图、权限结果、Schema 版本与节点输出</small>
              </div>
              <span className="eval-pass">✓</span>
            </div>
            <div className="eval-run">
              <span className="eval-run-id">02</span>
              <div>
                <strong>恢复前重新校验</strong>
                <small>权限、Schema 与数据源连接状态</small>
              </div>
              <span className="eval-pass">✓</span>
            </div>
            <div className="eval-run">
              <span className="eval-run-id">03</span>
              <div>
                <strong>从失败节点精确续跑</strong>
                <small>已完成的模型与工具调用不重复计费</small>
              </div>
              <span className="eval-pass">✓</span>
            </div>
          </div>
        </article>
      </div>
    </>
  )
}

function PerformancePanel({ d, live }: { d: OfflineQuality; live: LiveQuality | null }) {
  const b = d.blind!
  const nodes = live?.nodes ?? []
  const worst = Math.max(...nodes.map(n => n.p95_ms ?? 0), 1)
  // 原型的「单任务成本」是每题的钱，结果文件里 cost_cny 是**整轮**的合计
  const perCase = b.cost_cny / Math.max(b.n, 1)
  return (
    <>
    <div className="eval-metric-grid">
      <MetricCard label="P95 端到端耗时" value={fmtMs(b.p95_ms)}
                  note="提交问题到生成可信答案的第 95 百分位耗时 · 离线回归环境，与线上不可直接比较"
                  status={`目标 < ${P95_TARGET_MS / 1000}s`}
                  danger={b.p95_ms > P95_TARGET_MS} />
      {/* 原型这枚角标是「↓ 11%」。这里的箭头由**上一轮同源回归**算出来 ——
          结果文件每跑一轮存一份 .prev.json，出处（库 / 题库 / 模型）对不上后端
          就不给 prev，页面照实说"没有可比的上一轮"，而不是留一个好看的降幅。 */}
      <MetricCard label="平均 Token 消耗" value={b.avg_tok?.toLocaleString() ?? '—'}
                  note="包含 SQL 生成、修复和最终结果解释"
                  status={delta(b.avg_tok, b.prev?.avg_tok, b.prev?.n ?? 0)
                    || '首轮 · 无可比上一轮'} />
      <MetricCard label="单任务成本" value={`¥${perCase.toFixed(4)}`}
                  note="模型调用与追踪开销，不包含数据库资源成本"
                  status={`目标 < ¥${COST_TARGET_CNY}`}
                  danger={perCase > COST_TARGET_CNY} />
    </div>

    {/* 原型这里是「P95 阶段耗时拆解」。数据用**线上真实调用**的节点聚合 ——
        离线回归的样本量太小，拆出来的分位没有意义；而线上那份本来就在算。 */}
    {nodes.length > 0 && (
      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>P95 阶段耗时拆解</strong>
            <small>定位端到端延迟的主要贡献节点 · 来自线上真实调用，非离线样本</small>
          </div>
          <span className="status">TOTAL {fmtMs(live?.p95_ms)}</span>
        </div>
        <div className="eval-stage-list">
          {nodes.map(n => (
            <div className="eval-stage" key={n.step}>
              <span>{STEP_NAMES[n.step] ?? n.step}</span>
              <strong>{fmtMs(n.p95_ms)}</strong>
              <div className="eval-stage-bar">
                <i style={{ width: `${Math.round((n.p95_ms ?? 0) / worst * 100)}%` }} />
              </div>
            </div>
          ))}
        </div>
      </article>
    )}
    </>
  )
}

/** 场景名按设计稿的说法（业务口径 / 安全拦截 / 多步分析…）。
 *  设计稿里还有「主动澄清」「故障恢复」两类 —— 本评测集**没有这两类题**，
 *  所以不列：把没有的场景写进去，覆盖面就是假的。 */
const CATEGORY_CN: Record<string, string> = {
  single: '常规查询',
  join: '多表关联',
  metric: '业务口径',
  window: '窗口分析',
  multihop: '多步分析',
  reject: '安全拦截',
  security: '安全边界',
}

/** 安全题在「场景」那一列显示到哪一面被考。安全拦截与安全边界各有四种攻击面，
 *  只写"安全拦截"看不出这 34 道题考的是同一件事还是四件事。 */
const SCENE_CN: Record<string, string> = {
  write_ddl: '写入与 DDL',
  escalation: '跨角色越权',
  sensitive: '敏感信息',
  injection: '提示注入',
}

/** 评测集 —— 版式与字段照原型 `[data-eval-panel="datasets"]`：
 *  三格汇总（黄金问题 / 标准答案 / 最近回归结果）+ 一张五列表
 *  （用例 / 场景 / 黄金问题 / 标准答案 / 最近结果）。
 *
 *  用例编号按原型的 EV-0126 形态显示，序号由用例在评测集文件里的位置生成 ——
 *  它是**显示用编号**，真实 id（s03 / j06 / r03）挂在同一格的 title 上，
 *  否则拿着页面上的编号回文件里就找不到那道题了。
 */
function evCode(index: number): string {
  return `EV-${String(index + 1).padStart(4, '0')}`
}

function DatasetScope({ offline }: { offline: OfflineQuality | null }) {
  // 分页条与任务中心、审计中心同一套结构与类名 —— 三页的操作手感必须一致。
  // 评测集全量 58 条时不分页也能看，但这套题会长；一次渲染整份文件是
  // 任务中心已经踩过的那个坑（一千四百多行、DOM 高七万像素）。
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  if (!offline?.available) {
    return (
      <section className="eval-scope-panel active">
        <p className="drawer-note">尚未跑过评测，没有可展示的用例结果。</p>
      </section>
    )
  }

  const cases = offline.cases ?? []
  const g = offline.golden
  const ran = cases.filter(c => c.passed !== null)
  const passed = cases.filter(c => c.passed === true).length
  const answered = cases.filter(c => c.has_answer).length
  const cats = Object.keys(g?.by_category ?? {})
  const pages = Math.max(Math.ceil(cases.length / pageSize), 1)
  const current = Math.min(page, pages)
  const visible = cases.slice((current - 1) * pageSize, current * pageSize)
  const rate = ran.length ? `${((passed / ran.length) * 100).toFixed(1)}%` : '—'

  // 「最近更新」只能说评测集文件的 mtime —— 这套题没有版本号，也没有任何地方
  // 记录"谁在什么时候改了考题"。角标同理：题目齐不齐是能验的（每条都有标准答案），
  // "谁维护、审没审过"不是，所以那两项不编。
  const updated = g?.updated_at ? g.updated_at.slice(0, 16).replace('T', ' ') : '—'
  const complete = g?.answered != null && g.total > 0 && g.answered === g.total

  return (
    <section className="eval-scope-panel active">
      <div className="eval-context">
        <div className="eval-context-copy">
          <i className="eval-context-mark">SET</i>
          <div>
            <strong>黄金评测集 · {g?.path?.split('/').pop() || '未知文件'}</strong>
            <small>
              {g?.total ?? cases.length} 个黄金问题 · 标准 SQL、行列约束与预期拦截规则
            </small>
          </div>
        </div>
        <div className="eval-context-meta">
          <span>最近更新 <b>{updated}</b></span>
          <span>本轮实跑 <b>{ran.length} / {cases.length}</b></span>
          <span className={`status ${complete ? '' : 'wait'}`}>
            {complete ? '标准答案齐全' : '标准答案不全'}
          </span>
        </div>
      </div>

      <div className="eval-dataset-summary">
        <div className="eval-dataset-stat">
          <span>黄金问题</span><strong>{g?.total ?? cases.length}</strong>
          <small>{cats.length} 类场景 · {g?.path?.split('/').pop() || '评测集文件'}</small>
        </div>
        <div className="eval-dataset-stat">
          <span>标准答案</span><strong>{answered} / {cases.length}</strong>
          <small>标准 SQL + 行列约束 · 应拒用例对规则</small>
        </div>
        <div className="eval-dataset-stat">
          <span>最近回归结果</span><strong>{passed} PASS</strong>
          {/* 通过率的分母是**实跑数**，不是全集。盲测只跑一部分：拿全集当分母会
              把通过率算低，拿"没失败"当通过会把它算高 —— 两个方向都不能含糊，
              所以未跑数单列出来。 */}
          <small>
            {ran.length - passed} 条待修复 · {rate}
            {cases.length - ran.length > 0 && ` · 其余 ${cases.length - ran.length} 条本轮未跑`}
          </small>
        </div>
      </div>

      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>黄金评测集</strong>
            {/* 这一行照设计稿原文。**它描述的场景与本评测集的实际分类并不一致**
                （实际是单表 / 多表连接 / 业务口径 / 窗口函数 / 多跳 / 应拒绝，
                没有"歧义澄清"和"异常恢复"这两类题）—— 产品要求文案与设计稿保持
                一致，覆盖面以下方表格的「场景」列为准。 */}
            <small>覆盖正常查询、歧义澄清、多步分析、安全攻击与异常恢复</small>
          </div>
          {/* 设计稿这里是「导入用例」「＋ 新增问题」两个可点的按钮。评测集是版本库里的
              jsonl，改它要走评审与重跑 —— 页面上给一个即时生效的入口，等于让人可以
              悄悄改掉考题再宣称分数提升。所以位置与形状照留，但**禁用**，
              hover 说明去哪儿改。 */}
          <div className="card-actions">
            <button className="ghost" type="button" disabled
                    title="评测集在版本库的 jsonl 里，导入要走代码评审后重跑">导入用例</button>
            <button className="secondary" type="button" disabled
                    title="新增问题要改评测集文件并重跑回归，不能在页面上即时生效">＋ 新增问题</button>
          </div>
        </div>
        <div className="eval-table-wrap">
          <table>
            <thead>
              <tr><th>用例</th><th>场景</th><th>黄金问题</th><th>标准答案</th><th>最近结果</th></tr>
            </thead>
            <tbody>
              {/* 序号按**全集里的位置**算 —— 用页内下标的话，第二页又会从 EV-0001 开始 */}
              {visible.map((c, i) => (
                <tr key={c.id}>
                  <td className="mono" title={`评测集内 id：${c.id}`}>
                    {evCode((current - 1) * pageSize + i)}
                  </td>
                  <td title={c.scene ? CATEGORY_CN[c.category] ?? c.category : undefined}>
                    {(c.scene && SCENE_CN[c.scene]) || CATEGORY_CN[c.category] || c.category}
                  </td>
                  <td className="eval-case-question" title={c.question}>{c.question}</td>
                  <td className="dim" title={c.expect}>{c.expect || '—'}</td>
                  <td>
                    {c.passed === null
                      ? <span className="status wait" title="本轮盲测未跑到，不是通过">未跑</span>
                      : c.graded === false
                        ? <span className="status wait" title={c.reason || '本轮跑到了，但在这个数据源上判不动'}>未判定</span>
                      : c.passed
                        ? <span className="eval-pass">PASS</span>
                        : <span className="eval-fail" title={c.reason}>FAIL</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {cases.length > 0 && (
          <div className="audit-pager">
            <span>共 {cases.length} 条 · 第 {current} / {pages} 页</span>
            <span>
              <select value={pageSize}
                      onChange={e => { setPageSize(Number(e.target.value)); setPage(1) }}>
                {[10, 20, 50].map(size => <option key={size} value={size}>每页 {size} 条</option>)}
              </select>
              <button className="ghost" disabled={current <= 1}
                      onClick={() => setPage(p => p - 1)}>‹ 上一页</button>
              <button className="ghost" disabled={current >= pages}
                      onClick={() => setPage(p => p + 1)}>下一页 ›</button>
            </span>
          </div>
        )}
      </article>
    </section>
  )
}
