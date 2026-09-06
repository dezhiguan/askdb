import { useEffect, useState } from 'react'

import {
  fetchLiveQuality, fetchOfflineQuality,
  type LiveQuality, type OfflineQuality,
} from '../api'
import { PageHeader } from '../components/AppShell'
import { STEP_NAMES } from '../traceSteps'

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

function MetricCard({ label, value, note, status, danger }: {
  label: string; value: string; note: string; status?: string; danger?: boolean
}) {
  return (
    <article className={`eval-metric-card ${danger ? 'danger-metric' : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
      {status && <span className="status">{status}</span>}
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

export function EvaluationPage() {
  const [scope, setScope] = useState<Scope>('runtime')
  const [category, setCategory] = useState<Category>('overview')
  const [days, setDays] = useState(1)
  const [live, setLive] = useState<LiveQuality | null>(null)
  const [offline, setOffline] = useState<OfflineQuality | null>(null)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  // 线上指标随时间窗重取；离线回归是跑出来的文件，不随窗口变
  useEffect(() => {
    let alive = true
    fetchLiveQuality(days)
      .then(v => { if (alive) { setLive(v); setError('') } })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [days, reload])

  useEffect(() => {
    let alive = true
    fetchOfflineQuality()
      .then(v => { if (alive) setOffline(v) })
      .catch(() => {})
    return () => { alive = false }
  }, [reload])

  return (
    <div className="page">
      <PageHeader
        title="Agent 质量中心"
        description="持续观测当前生产 Agent 的运行健康、结果质量、安全与成本，并用离线回归验证版本变更。"
        action={
          <div className="eval-toolbar">
            <select
              aria-label="选择线上统计时间范围"
              value={days}
              onChange={e => setDays(Number(e.target.value))}
            >
              <option value={1}>最近 24 小时</option>
              <option value={7}>最近 7 天</option>
              <option value={30}>最近 30 天</option>
            </select>
            <button className="primary" type="button" onClick={() => setReload(n => n + 1)}>
              ↻ 刷新运行状态
            </button>
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

      {error && <div className="audit-error">读取质量数据失败：{error}</div>}
      {scope === 'runtime' && <RuntimeScope live={live} offline={offline} days={days} />}
      {scope === 'online' && <OnlineScope live={live} days={days} />}
      {scope === 'offline' && (
        <OfflineScope category={category} onCategory={setCategory}
                      onDataset={() => setScope('dataset')} offline={offline} live={live} />
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

/** Agent 版本号 —— **写死**。askdb 本身没有对外的"Agent 版本"概念（包版本
 *  0.1.0 是另一回事），产品上先按原型显示；接上真正的版本来源后换掉这一个常量即可。 */
const AGENT_VERSION = 'Agent v2.4'
type Alert = { level: '高优' | '中优' | '低优'; text: string }

/** 告警由真实指标按固定阈值判出来。阈值是策略，判定过程不是 —— 每条都写明依据。 */
function alertsOf(live: LiveQuality): Alert[] {
  const out: Alert[] = []
  if (live.failed > 0) out.push({ level: '高优', text: `执行失败 ${live.failed} 次（数据源或模型调用）` })
  if (live.security_events > 0) out.push({ level: '高优', text: `护栏安全事件 ${live.security_events} 次（R-xx 规则命中）` })
  for (const n of live.nodes) {
    if ((n.success_rate ?? 1) < 0.95) {
      out.push({ level: '中优', text: `${STEP_NAMES[n.step] ?? n.step} 成功率 ${pct(n.success_rate)}，低于 95%` })
    }
  }
  out.push(...latencyAlerts(live))
  return out
}

/** 上一个等长窗口怎么称呼。窗口长度变了措辞也得跟着变 ——
 *  选了「最近 7 天」还说"较昨日"，是在报一个没算过的对比。 */
function prevLabel(days: number): string {
  return days === 1 ? '较昨日' : days === 7 ? '较上周' : days === 30 ? '较上月' : `较上一个 ${days} 天`
}

/** 延迟告警，按原型的措辞（"database.query P95 较昨日上升 8%"）出环比。
 *
 *  环比比绝对阈值有用：18.77s 这个数单看不知道是不是常态，"较昨日上升 8%"
 *  才指向"有东西变了"。但它有前提 —— 两个窗口都得有足够样本。
 *  上个窗口只跑了两三次时，P95 就是那两三次里的最大值，涨跌纯属噪声；
 *  这种情况**不报环比**，退回绝对阈值，并说明为什么没有对比。
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
        level: worst.delta >= 0.3 ? '中优' : '低优',
        text: `${STEP_NAMES[worst.step] ?? worst.step} P95 ${label}上升 ${Math.round(worst.delta * 100)}%`,
      }]
    }
    // 端到端整体的环比 —— 单个节点都没超阈值，但总耗时可能被多段合力推高
    if (prev.p95_ms && live.p95_ms) {
      const delta = (live.p95_ms - prev.p95_ms) / prev.p95_ms
      if (delta >= 0.05) {
        return [{
          level: delta >= 0.3 ? '中优' : '低优',
          text: `P95 端到端 ${label}上升 ${Math.round(delta * 100)}%（${fmtMs(prev.p95_ms)} → ${fmtMs(live.p95_ms)}）`,
        }]
      }
    }
    return []
  }

  // 没有可比的上一窗口：不编环比，改报绝对值，并把原因说出来
  if ((live.p95_ms ?? 0) > 10000) {
    return [{
      level: '低优',
      text: `P95 端到端 ${fmtMs(live.p95_ms)}，超过 10s 目标（上一个窗口调用不足 ${MIN_COMPARE_CALLS} 次，无法算环比）`,
    }]
  }
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
        <div className="quality-index">{score ?? '—'}<small>/100</small></div>
        <div className="quality-verdict-copy">
          <span>PRODUCTION AGENT · {band}</span>
          <strong>{verdict}</strong>
          <p>
            基于最近 {live.days} 天 {live.runs.toLocaleString()} 次真实调用持续计算；
            {idle
              ? '没有调用就没有可报的运行质量 —— 到查询工作台问几次，或把上方时间范围放宽再回来。'
              : top
                ? `当前 ${top.level}告警：${top.text}。`
                : '无告警，工具调用与安全状态均在正常范围。'}
            <b> 分数的权重（任务 60% / 工具 40%）与告警阈值是本项目设定的策略，不是测量值。</b>
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
          <div className="quality-kpi-head"><span>实际任务量</span><code>REAL · {live.days}D</code></div>
          <strong>{live.runs.toLocaleString()}</strong>
          <small>
            {idle
              ? `最近 ${days} 天没有调用`
              : `成功完成 ${live.ok.toLocaleString()} · 护栏拦截 ${live.blocked} · 执行失败 ${live.failed}`}
          </small>
        </div>
        <div className="quality-kpi">
          <div className="quality-kpi-head"><span>当前告警</span><code>LIVE</code></div>
          <strong>{idle ? '—' : alerts.length ? `${alerts.length} 个${top!.level}` : '无告警'}</strong>
          <small>{idle ? '无调用可判' : top ? top.text : '失败、节点成功率、P95 三项均在阈值内'}</small>
        </div>
        <div className="quality-kpi">
          <div className="quality-kpi-head"><span>最新离线回归</span><code>辅助验证</code></div>
          <strong>{sc ? `${sc.overall} / 100` : '尚未运行'}</strong>
          <small>
            {/* 原型这行写的是「当前版本黄金集结果 · 不是线上统计」。出处对得上就照抄；
                对不上时**不能照抄** —— 这组分数出自另一个库、另一个模型，
                「当前版本」四个字就是假的。 */}
            {!sc
              ? '跑一次黄金集后这里才有数'
              : sameSrc
                ? '当前版本黄金集结果 · 不是线上统计'
                : `⚠ ${prov?.datasource || '另一个数据源'} 的黄金集结果 · 不能代表本实例`}
          </small>
        </div>
      </div>
    </section>
  )
}

function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—'
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${v}ms`
}

/** 线上质量。全部按真实调用统计，不用黄金集分母。
 *
 *  设计稿这里的节点表写的是 schema.retrieve / metric.resolve / sql.guard /
 *  database.query / result.summarize，并注明"来自 Langfuse / OpenTelemetry
 *  Span 聚合"。askdb 的真实节点名不是这一套（见 traceSteps.STEP_NAMES），
 *  数据也不来自 Langfuse —— 它就在自己的审计记录里。照抄节点名等于
 *  凭空造出五个不存在的工具。
 */
function OnlineScope({ live, days }: { live: LiveQuality | null; days: number }) {
  if (!live) return <p className="drawer-note">读取中…</p>
  if (!live.runs) {
    return (
      <section className="eval-scope-panel active">
        <p className="drawer-note">最近 {days} 天没有调用记录，线上指标无从算起。</p>
      </section>
    )
  }

  const rules = Object.entries(live.by_rule)
  return (
    <section className="eval-scope-panel active" aria-label="线上质量">
      <div className="eval-note">
        <strong>以下指标来自本实例的审计记录，按真实调用统计，不使用黄金集分母</strong>
        <p>
          每次调用一条记录，含节点级 trace。线上结果的<b>准确性没有天然标准答案</b> ——
          这里能报的是执行是否成功、被护栏挡了多少、耗时与成本；
          "答得对不对"要靠离线回归与抽样人工判断补。
        </p>
      </div>

      <div className="eval-metric-grid">
        <MetricCard label="调用成功率" status={`AUDIT · ${live.days}D`} value={pct(live.success_rate)}
                    note={`${live.ok.toLocaleString()} / ${live.runs.toLocaleString()} 次`} />
        <MetricCard label="护栏拦截率" status={`${live.blocked} BLOCKED`} value={pct(live.block_rate)}
                    note="拦下一条危险 SQL 是护栏在做对事，不是故障" />
        <MetricCard label="执行失败" status="EXEC / LLM" value={String(live.failed)}
                    note="数据源异常或模型调用失败" danger={live.failed > 0} />
        <MetricCard label="P95 端到端" status={`P50 ${fmtMs(live.p50_ms)}`} value={fmtMs(live.p95_ms)}
                    note={`样本 ${live.runs.toLocaleString()} 次真实调用`} />
      </div>

      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>节点健康度</strong>
            <small>按审计记录里的 steps 聚合 · 最近 {live.days} 天</small>
          </div>
        </div>
        <div className="eval-table-wrap">
          <table>
            <thead>
              <tr><th>节点</th><th className="num">调用</th><th className="num">成功率</th>
                  <th className="num">P50</th><th className="num">P95</th><th className="num">token</th></tr>
            </thead>
            <tbody>
              {live.nodes.map(n => (
                <tr key={n.step}>
                  <td>{STEP_NAMES[n.step] ?? n.step}</td>
                  <td className="num">{n.calls.toLocaleString()}</td>
                  <td className={`num ${(n.success_rate ?? 1) < 0.95 ? 'eval-fail' : 'eval-pass'}`}>
                    {pct(n.success_rate)}
                  </td>
                  <td className="num">{fmtMs(n.p50_ms)}</td>
                  <td className="num">{fmtMs(n.p95_ms)}</td>
                  <td className="num">{n.tok ? n.tok.toLocaleString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="drawer-note">
          按 P95 倒序 —— 这张表是拿来找端到端延迟贡献最大的那一段的。
        </p>
      </article>

      {rules.length > 0 && (
        <article className="eval-card">
          <div className="eval-card-head">
            <div><strong>护栏都挡了什么</strong><small>按拦截码分布 · 最近 {live.days} 天</small></div>
          </div>
          <div className="eval-table-wrap">
            <table>
              <thead><tr><th>拦截码</th><th>含义</th><th className="num">次数</th></tr></thead>
              <tbody>
                {rules.map(([code, n]) => (
                  <tr key={code}>
                    <td className="mono">{code}</td>
                    <td>{RULE_BRIEF[code] ?? '—'}</td>
                    <td className="num">{n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </article>
      )}
    </section>
  )
}

/** 拦截码的一句话含义。与结果页那份 RULES 同源但更短 —— 这张表是分布统计，
 *  不是给人排查单次失败的，长文案会把表撑散。 */
const RULE_BRIEF: Record<string, string> = {
  'R-01': '多语句',
  'R-02': '非查询语句',
  'R-03': '表不在白名单',
  'R-04': '字段不存在',
  'R-05': 'SELECT *',
  'R-06': '跨 schema 引用',
  'R-07': '禁用函数',
  'R-08': '笛卡尔积',
  'R-10': '租户归属不明',
  'R-11': '扫描量超限',
  'R-17': '累计成本超限',
  'R-18': '扇出放大',
  'R-19': '超出数据期限',
  QUOTA: '当日配额用尽',
  EXEC: '数据源执行失败',
  LLM: '模型调用失败',
  NO_SQL: '现有表回答不了',
  INTERRUPTED: '执行中断',
}

function OfflineScope({ category, onCategory, onDataset, offline, live }: {
  category: Category
  onCategory: (c: Category) => void
  onDataset: () => void
  offline: OfflineQuality | null
  /** 性能页的阶段拆解取线上真实调用 —— 离线样本量撑不起分位数 */
  live: LiveQuality | null
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
      {category === 'accuracy' && <AccuracyPanel d={offline} />}
      {category === 'security' && <SecurityPanel d={offline} />}
      {category === 'stability' && <StabilityPanel d={offline} live={live} />}
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

function AccuracyPanel({ d }: { d: OfflineQuality }) {
  const b = d.blind!
  const groups = d.groups ?? []
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
        <MetricCard label="盲测准确率" status={`${b.n} CASES`} value={pct(b.accuracy)}
                    note="按执行结果判定 —— 等价 SQL 会通过结果比对，不要求字符串相同" />
        <MetricCard label="多步误用率" status="MULTI-STEP" value={pct(b.multi_misuse)}
                    note="本该单步却拆成多步的比例" />
      </div>

      {/* 设计稿这里还有「业务口径命中率」与「回答忠实度」两项。
          评测目前不计算它们 —— 前者要逐条比对注入的口径有没有被真的用上，
          后者要判断结论能否由结果集完整支撑，两者都需要额外的判定器。
          留空位比编一个数诚实。 */}
      <NotMeasured
        title="这两项评测尚未计算"
        items={[
          '业务口径命中率 —— 需要逐条比对：命中的口径定义有没有真的进入最终 SQL',
          '回答忠实度 —— 需要判定结论能否由结果集完整支撑，无额外推断',
        ]}
        hint="口径本身是否有区分度，可在「业务口径」页按真实数据核对。"
      />

      {groups.length > 0 && (
        <article className="eval-card">
          <div className="eval-card-head">
            <div>
              <strong>消融对照</strong>
              <small>同一黄金集下逐层加能力 · 标 ★ 的是当前默认配置</small>
            </div>
          </div>
          <div className="eval-table-wrap">
            <table>
              <thead><tr><th>组</th><th>能力</th><th className="num">用例</th>
                         <th className="num">准确率</th><th className="num">误拒</th>
                         <th className="num">P95</th><th className="num">成本</th></tr></thead>
              <tbody>
                {groups.map(g => (
                  <tr key={g.key}>
                    <td className="mono">{g.key}{d.shipped === g.key ? ' ★' : ''}</td>
                    <td>{g.label}</td>
                    <td className="num">{g.n}</td>
                    <td className="num">{pct(g.accuracy)}</td>
                    <td className="num">{pct(g.false_reject)}</td>
                    <td className="num">{fmtMs(g.p95_ms)}</td>
                    <td className="num">¥{g.cost_cny}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </article>
      )}
    </>
  )
}

function SecurityPanel({ d }: { d: OfflineQuality }) {
  const b = d.blind!
  return (
    <>
      <div className="eval-note">
        <i>盾</i>
        <div>
          <strong>安全指标采用红线门禁</strong>
          <small>
            该拒未拒是红线，不用综合高分抵消 —— 一次放行危险 SQL，
            比准确率低几个点严重得多。
          </small>
        </div>
      </div>
      <div className="eval-metric-grid">
        <MetricCard label="该拒即拒" status="红线" value={pct(b.block_rate)}
                    note="应当被拦的用例里实际拦下的比例" danger={b.block_rate < 1} />
        <MetricCard label="误拒率" status="越低越好" value={pct(b.false_reject)}
                    note="本该能答却被挡下 —— 护栏过紧同样是问题" danger={b.false_reject > 0} />
      </div>
      <NotMeasured
        title="这几项安全指标评测尚未覆盖"
        items={[
          '越权率 —— 需要跨角色、跨数据域的用例集，逐条验证租户谓词与 RLS',
          '敏感数据泄漏率 —— 列级脱敏已在执行层无条件生效，但还没有成套用例逐列验证它',
          '提示注入 —— 需要专门的注入用例集',
        ]}
        hint="生产环境的实际拦截分布在「线上质量」里是真数据。"
      />
    </>
  )
}

/* 稳定性。原型这一页有三张指标卡 + 故障注入 + 恢复原则。
 *
 * 三项里只有「执行成功率」askdb 真的在测；重试恢复率与故障注入需要故障
 * 注入框架，没有就如实空着，不拿线上成功率去顶替 —— 那两个数问的是
 * "坏掉之后能不能自己回来"，跟"平时跑得顺不顺"不是一回事。
 *
 * 「恢复原则」那张卡是**行为说明**不是测量值，可以照原型的版式做，
 * 但内容必须写 askdb 真实的行为：原型写"已完成调用不重复计费"，
 * 而 graph.resume() 明确另计一次每日配额 —— 照抄就是编。
 */
function StabilityPanel({ d, live }: { d: OfflineQuality; live: LiveQuality | null }) {
  const b = d.blind!
  // 不在前端重推一遍 —— 后端 /api/eval 的 score.stability 已经按
  // outcomes 里的"链路失败"算过。两处各算各的迟早会对不上。
  const dim = d.score?.dimensions.find(x => x.key === 'stability')
  return (
    <>
      <div className="eval-metric-grid">
        <MetricCard label="执行成功率（离线）" status={dim?.source ?? `N=${b.n}`}
                    value={dim ? `${dim.value}%` : '—'}
                    note="盲测里没有栽在执行或模型链路上的比例" />
        <MetricCard label="执行成功率（线上）" status={`AUDIT · ${live?.days ?? '-'}D`}
                    value={live ? pct((live.runs - live.failed) / Math.max(live.runs, 1)) : '—'}
                    note={live ? `${live.runs} 次真实调用，${live.failed} 次链路失败` : '读取中'} />
        <MetricCard label="重试恢复率" status="未测量" value="—"
                    note="要在评测里注入连接超时与限流，目前没有故障注入" />
        <MetricCard label="断点续跑成功率" status="未测量" value="—"
                    note="要先构造中断样本再走 /api/resume，尚未纳入回归" />
      </div>

      <NotMeasured
        title="故障注入结果"
        items={[
          '数据库连接超时 —— 需要能在评测中断开只读连接',
          '模型限流 / 超时 —— 需要可控地让 LLM 调用失败',
          'Schema 漂移 —— 需要在跑批中途改表结构再观察护栏反应',
        ]}
        hint="这三类都要故障注入框架才能测，askdb 目前没有，因此上面两格留空而不是填数。"
      />

      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>恢复原则</strong>
            <small>失败不等于从头重跑 · 以下是 askdb 的实际行为，不是目标</small>
          </div>
          <span className="status">graph.resume()</span>
        </div>
        <div className="eval-card-body">
          <div className="eval-run">
            <span className="eval-run-id">01</span>
            <div>
              <strong>保存最小任务状态</strong>
              <small>问题原文、org_id 与 R-17 累计计数进检查点；续跑时回种，计数不归零</small>
            </div>
            <span className="eval-pass">已实现</span>
          </div>
          <div className="eval-run">
            <span className="eval-run-id">02</span>
            <div>
              <strong>从最后一个完成的检查点续跑</strong>
              <small>线程不变，审计写新的 trace_id，两条经 thread_id 关联</small>
            </div>
            <span className="eval-pass">已实现</span>
          </div>
          <div className="eval-run">
            <span className="eval-run-id">03</span>
            <div>
              <strong>续跑另计一次每日配额</strong>
              <small>与原型写的"不重复计费"相反 —— 恢复要再走一遍模型调用，就照实计</small>
            </div>
            <span className="eval-pass">已实现</span>
          </div>
          <div className="eval-run">
            <span className="eval-run-id">04</span>
            <div>
              <strong>恢复前重新校验权限与 Schema</strong>
              <small>尚未实现：目前直接从检查点状态续跑，中断期间权限或表结构变了不会被重新拦</small>
            </div>
            <span className="status wait">未实现</span>
          </div>
        </div>
      </article>
    </>
  )
}

function PerformancePanel({ d, live }: { d: OfflineQuality; live: LiveQuality | null }) {
  const b = d.blind!
  const nodes = live?.nodes ?? []
  const worst = Math.max(...nodes.map(n => n.p95_ms ?? 0), 1)
  return (
    <>
    <div className="eval-metric-grid">
      <MetricCard label="P95 端到端" status="OFFLINE" value={fmtMs(b.p95_ms)}
                  note="离线回归环境，与线上不可直接比较" />
      <MetricCard label="本轮总成本" status={`${b.n} CASES`} value={`¥${b.cost_cny}`}
                  note="仅模型调用开销，不含数据库资源" />
      <MetricCard label="单条平均成本" status="AVG" value={`¥${(b.cost_cny / Math.max(b.n, 1)).toFixed(4)}`}
                  note="用来估算跑一轮全集要花多少" />
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

/** 未测量项的统一说法。
 *
 *  设计稿给这些指标都配了数字（重试恢复率 88.5%、越权率 0% 等）。
 *  评测没有计算它们，写上去就是编 —— 而这一页的用途恰恰是判断"能不能发布"，
 *  在这里编数字的后果比别处都严重。列出缺什么，比留一个漂亮的假数诚实。
 */
function NotMeasured({ title, items, hint }: {
  title: string
  items: string[]
  hint?: string
}) {
  return (
    <article className="eval-card">
      <div className="eval-card-head">
        <div><strong>{title}</strong><small>缺的是什么，逐条列在下面</small></div>
        <span className="status wait">未测量</span>
      </div>
      <div className="eval-card-body">
        <ul className="nr-list">{items.map(i => <li key={i}>{i}</li>)}</ul>
        {hint && <p className="drawer-note">{hint}</p>}
      </div>
    </article>
  )
}

const CATEGORY_CN: Record<string, string> = {
  single: '单表',
  join: '多表连接',
  metric: '业务口径',
  window: '窗口函数',
  multihop: '多跳',
  reject: '应拒绝',
}

function DatasetScope({ offline }: { offline: OfflineQuality | null }) {
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

  return (
    <section className="eval-scope-panel active">
      <div className="eval-dataset-summary">
        <div className="eval-dataset-stat">
          <span>黄金问题</span><strong>{g?.total ?? cases.length}</strong>
          <small>{Object.keys(g?.by_category ?? {}).length} 类场景</small>
        </div>
        <div className="eval-dataset-stat">
          <span>本轮实跑</span><strong>{ran.length} / {cases.length}</strong>
          <small>盲测只跑标了 blind 的那些</small>
        </div>
        <div className="eval-dataset-stat">
          <span>本轮结果</span><strong>{passed} PASS</strong>
          <small>{ran.length - passed} 条失败 · 其余 {cases.length - ran.length} 条本轮未跑</small>
        </div>
      </div>

      <article className="eval-card">
        <div className="eval-card-head">
          <div>
            <strong>黄金评测集</strong>
            <small className="mono">{g?.path}</small>
          </div>
        </div>
        {/* 设计稿这里有「导入用例」「＋ 新增问题」两个按钮。评测集是版本库里的
            jsonl 文件，改它要走评审与重跑 —— 页面上加一个即时生效的入口，
            等于让人可以悄悄改掉考题再宣称分数提升。 */}
        <p className="drawer-note">
          用例定义在版本库的 jsonl 里，增改走代码评审后重跑 ——
          考题能在页面上即时改，分数就不再是分数。
        </p>
        <div className="eval-table-wrap">
          <table>
            <thead>
              <tr><th>用例</th><th>场景</th><th>黄金问题</th><th>期望</th><th>本轮结果</th></tr>
            </thead>
            <tbody>
              {cases.map(c => (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td>
                  <td>{CATEGORY_CN[c.category] ?? c.category}</td>
                  <td className="eval-case-question" title={c.question}>{c.question}</td>
                  <td className="dim" title={c.expect}>{c.expect || '—'}</td>
                  <td>
                    {c.passed === null
                      ? <span className="status wait" title="本轮盲测未跑到，不是通过">未跑</span>
                      : c.passed
                        ? <span className="eval-pass">PASS</span>
                        : <span className="eval-fail" title={c.reason}>FAIL</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </article>
    </section>
  )
}
