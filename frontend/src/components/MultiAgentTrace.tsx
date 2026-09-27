import { Fragment, useState } from 'react'
import type { ReplayStep } from '../api'
import { STAGE_NAMES, STEP_NAMES, STEP_TYPE, stepFailed, stepSoft } from '../traceSteps'

export type AgentRunGroup = {
  id: string
  role: string
  label: string
  parent: string
  steps: ReplayStep[]
}

type RunStatus = 'ok' | 'soft' | 'failed'
type RunView = AgentRunGroup & {
  startMs: number | null
  endMs: number | null
  elapsedMs: number
  tokens: number
  status: RunStatus
  task: string
  output: string
}

const duration = (ms: number): string => ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : ms + 'ms'
const preview = (value: string | undefined, max = 110): string => {
  const clean = (value || '').replace(/\s+/g, ' ').trim()
  return clean.length > max ? clean.slice(0, max - 1) + '…' : clean
}
const spanKind = (step: ReplayStep): string => {
  const kind = STEP_TYPE[step.step] || 'SYS'
  return kind === 'MODEL' ? 'LLM' : kind === 'DB' || kind === 'RAG' ? 'QUERY' : kind
}
const statusOf = (steps: ReplayStep[]): RunStatus => {
  if (steps.length === 0) return 'ok'
  if (stepFailed(steps[steps.length - 1].status)) return 'failed'
  return steps.some(step => stepFailed(step.status) || stepSoft(step.status)) ? 'soft' : 'ok'
}
const statusLabel = (status: RunStatus): string => status === 'failed' ? '失败' : status === 'soft' ? '降级' : 'OK'
const icon = (role: string): string => ({ supervisor: 'S', semantic: 'S', query: 'Q', query_worker: 'W', verifier: 'V', synthesizer: 'Σ' } as Record<string, string>)[role] || role.slice(0, 1).toUpperCase()
const roleClass = (role: string): string => role === 'query_worker' ? 'worker' : role === 'verifier' ? 'gold' : role === 'synthesizer' ? 'dark' : ''

function describeRun(group: AgentRunGroup): RunView {
  const timed = group.steps.length > 0 && group.steps.every(step => Number.isFinite(step.start_ms))
  const startMs = timed ? Math.min(...group.steps.map(step => step.start_ms as number)) : null
  const endMs = timed ? Math.max(...group.steps.map(step => (step.start_ms as number) + step.ms)) : null
  const elapsedMs = startMs !== null && endMs !== null
    ? endMs - startMs : group.steps.reduce((total, step) => total + step.ms, 0)
  const workerInput = group.steps.find(step => step.step === 'query_worker' && step.input)?.input
  const legacyTitle = workerInput?.match(/['"]title['"]\s*:\s*['"]([^'"]+)/)?.[1]
  const taskStep = group.role === 'query_worker'
    ? group.steps.find(step => step.step === 'schema_recall' && step.input)
    : undefined
  const task = preview(taskStep?.input || legacyTitle || group.steps.find(step => step.note)?.note || group.steps[0]?.input) || '未记录任务摘要'
  const lastOutput = [...group.steps].reverse().find(step => step.note || step.output)
  const output = preview(lastOutput?.note || lastOutput?.output, 200) || '未记录输出摘要'
  return {
    ...group, startMs, endMs, elapsedMs,
    tokens: group.steps.reduce((total, step) => total + (step.tok_in || 0) + (step.tok_out || 0), 0),
    status: statusOf(group.steps), task, output,
  }
}

function AgentExecutionDetail({ runs, steps, traceId, selected, onSelect }: {
  runs: RunView[]
  steps: ReplayStep[]
  traceId: string
  selected: string
  onSelect: (id: string) => void
}) {
  const [view, setView] = useState<'agents' | 'timeline' | 'all'>('agents')
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set(runs.map(run => run.id)))
  const [openSpans, setOpenSpans] = useState<ReadonlySet<string>>(() => new Set())
  const owner = new Map<ReplayStep, RunView>()
  runs.forEach(run => run.steps.forEach(step => owner.set(step, run)))
  const unassigned = steps.filter(step => !owner.has(step))
  const groups = unassigned.length ? [...runs, describeRun({
    id: traceId + ':unassigned', role: 'system', label: '未归属 Span', parent: '', steps: unassigned,
  })] : runs
  const timedRuns = runs.filter(run => run.startMs !== null && run.endMs !== null)
  const maxEnd = Math.max(1, ...timedRuns.map(run => run.endMs || 0))
  const hasTime = runs.length > 0 && runs.every(run => run.startMs !== null && run.endMs !== null)
  const workerRuns = runs.filter(run => run.role === 'query_worker')
  const workersOverlap = workerRuns.some((run, i) => workerRuns.slice(i + 1).some(other =>
    run.startMs !== null && run.endMs !== null && other.startMs !== null && other.endMs !== null
    && run.startMs < other.endMs && other.startMs < run.endMs))
  const toggle = (value: string, setter: (next: ReadonlySet<string>) => void, current: ReadonlySet<string>) => {
    const next = new Set(current)
    if (!next.delete(value)) next.add(value)
    setter(next)
  }
  const row = (step: ReplayStep, run: RunView | undefined, index: number, flat = false) => {
    const key = String(index)
    const open = openSpans.has(key)
    const input = preview(step.input, 85) || (step.tok_in ? step.tok_in.toLocaleString() + ' tok' : '—')
    const output = preview(step.note || step.output, 100) || '—'
    const subtitle = [step.tool || step.model || (step.stage && STAGE_NAMES[step.stage]),
      (step.attempts_total || 0) > 1 ? `尝试 ${step.attempt || 1}/${step.attempts_total}` : ''].filter(Boolean).join(' · ')
    const state = stepFailed(step.status) ? 'failed' : stepSoft(step.status) ? 'soft' : 'ok'
    return <Fragment key={key}>
      <div className={'proto-detail-spanrow ' + (flat ? 'flat' : '')}>
        <span className={'proto-detail-kind ' + spanKind(step).toLowerCase()}>{spanKind(step)}</span>
        <span className="proto-detail-spanname">{STEP_NAMES[step.step] || step.step}<small>{subtitle}</small></span>
        {flat ? <span className="proto-detail-owner">{run?.id || '未归属'}</span> : <span className="proto-detail-io" title={step.input || ''}>{input}</span>}
        <span className="proto-detail-io out" title={step.note || ''}>{flat ? input + ' / ' + output : output}</span>
        <time>{step.ms.toLocaleString()}ms</time>
        <button type="button" className="proto-detail-expand" aria-expanded={open}
          aria-label={(STEP_NAMES[step.step] || step.step) + ' 输入输出详情'}
          onClick={() => toggle(key, setOpenSpans, openSpans)}>{open ? '⌃' : '⌄'}</button>
        <strong className={state}>{step.status.toUpperCase()}</strong>
      </div>
      {open && <div className="proto-detail-disclosure">
        <div><b>输入</b><span>{step.input || '未记录'}</span></div>
        <div><b>输出</b><span>{step.output || '未记录'}</span></div>
        <div><b>所属 Agent Run</b><span>{run?.id || '未归属'}</span></div>
        {step.note && <div><b>摘要</b><span>{step.note}</span></div>}
        {step.tables?.length ? <div><b>涉及表</b><span>{step.tables.join('、')}</span></div> : null}
        {step.model && <div><b>模型</b><span>{step.model}</span></div>}
        {step.tool && <div><b>工具</b><span>{step.tool}</span></div>}
        {(step.tok_in || step.tok_out || step.cached_in) && <div><b>Token</b><span>输入 {step.tok_in || 0} · 输出 {step.tok_out || 0}{step.cached_in ? ` · 缓存输入 ${step.cached_in}` : ''}</span></div>}
        {step.start_ms != null && <div><b>相对开始</b><span>+{duration(step.start_ms)}</span></div>}
        {step.disposition && <div><b>错误处置</b><span>{step.disposition}</span></div>}
        {step.error_code && <div><b>错误码</b><span>{step.error_code}</span></div>}
      </div>}
    </Fragment>
  }
  return <section className="proto-execution-detail">
    <header className="proto-detail-head">
      <div className="proto-detail-title"><h2>执行明细</h2><div className="proto-detail-tabs" role="tablist" aria-label="执行明细视图">
        <button type="button" role="tab" aria-selected={view === 'agents'} className={view === 'agents' ? 'active' : ''} onClick={() => setView('agents')}>按智能体 <small>{runs.length}</small></button>
        <button type="button" role="tab" aria-selected={view === 'timeline'} className={view === 'timeline' ? 'active' : ''} onClick={() => setView('timeline')}>时间线</button>
        <button type="button" role="tab" aria-selected={view === 'all'} className={view === 'all' ? 'active' : ''} onClick={() => setView('all')}>全部 Span <small>{steps.length}</small></button>
      </div></div>
      <div className="proto-detail-tools"><button type="button" onClick={() => { setExpanded(new Set(groups.map(group => group.id))); setView('agents') }}>全部展开</button><button type="button" onClick={() => { setExpanded(new Set()); setView('agents') }}>全部收起</button></div>
    </header>
    {view === 'agents' && <div className="proto-detail-groups">{groups.map(run => <div className={'proto-detail-group' + (selected === run.id ? ' selected' : '')} key={run.id}>
      <button type="button" className="proto-detail-grouphead" aria-expanded={expanded.has(run.id)}
        onClick={() => { toggle(run.id, setExpanded, expanded); if (run.role !== 'system') onSelect(run.id) }}>
        <span className="proto-detail-identity"><i className="proto-detail-chevron">{expanded.has(run.id) ? '⌄' : '›'}</i><i className={'proto-detail-role ' + roleClass(run.role)}>{icon(run.role)}</i><span><b>{run.label}</b><em>{run.role === 'system' ? '· SYSTEM' : '· AGENT'}</em></span></span>
        <span className="proto-detail-task" title={run.task}>{run.task}</span>
        <span className="proto-detail-metric"><b>{duration(run.elapsedMs)}</b></span>
        <span className="proto-detail-metric">{run.tokens ? run.tokens.toLocaleString() + ' tok' : '—'}</span>
        <strong className={'proto-detail-ok ' + run.status}>● {statusLabel(run.status)}</strong>
      </button>
      {expanded.has(run.id) && <div className="proto-detail-spans">{run.steps.map(step => row(step, run, steps.indexOf(step)))}</div>}
    </div>)}</div>}
    {view === 'timeline' && <div className="proto-detail-timeline">
      {!hasTime && <p className="proto-detail-time-note">部分历史 Span 未记录开始时间，仅显示已记录的时间段；不推断并行时序。</p>}
      <div className="proto-detail-lanes">{runs.map(run => <div className="proto-detail-lane" key={run.id}>
        <span className="proto-detail-lanename">{run.label}<small>{run.task}</small></span>
        <div className="proto-detail-rail">{run.startMs !== null && run.endMs !== null && <i className={roleClass(run.role)}
          style={{ left: (run.startMs / maxEnd * 100) + '%', width: (Math.max(.8, run.elapsedMs / maxEnd * 100)) + '%' }}
          title={run.label + ' · ' + duration(run.elapsedMs)} />}</div>
        <time>{run.startMs === null ? '时间未记录' : duration(run.elapsedMs)}</time>
      </div>)}</div>
      {timedRuns.length > 0 && <div className="proto-detail-axis">{Array.from({ length: 5 }, (_, i) => <span key={i}>{duration(Math.round(maxEnd * i / 4))}</span>)}</div>}
      <p className="proto-detail-note">{!hasTime ? '缺少开始时间的智能体仅按记录顺序展示。' : workersOverlap ? '工作智能体的时间段有重叠，图中可见并行执行与最长分支。' : '时间轴使用 /api/trace 中记录的相对开始时间。'}</p>
    </div>}
    {view === 'all' && <div className="proto-detail-all"><div className="proto-detail-spanrow flat heading"><span>类型</span><span>Span 名称</span><span>Agent Run</span><span>输入 / 输出摘要</span><span>耗时</span><span></span><span>状态</span></div>
      {steps.map((step, index) => row(step, owner.get(step), index, true))}
    </div>}
    <footer className="proto-detail-footnote">Agent Run 是智能体的一次执行实例；Span 按后端记录的 Agent Run ID 归属。{unassigned.length > 0 && '另有 ' + unassigned.length + ' 个未归属 Span。'}</footer>
  </section>
}

export function MultiAgentTrace({ traceId, groups, steps }: {
  traceId: string
  groups: AgentRunGroup[]
  steps: ReplayStep[]
}) {
  const runs = groups.map(describeRun)
  const [selected, setSelected] = useState(runs[0]?.id || '')
  const active = runs.find(run => run.id === selected) || runs[0]
  const supervisor = runs.find(run => run.role === 'supervisor')
  const semantic = runs.filter(run => run.role === 'semantic')
  const workers = runs.filter(run => run.role === 'query_worker')
  const verifiers = runs.filter(run => run.role === 'verifier')
  const synthesizers = runs.filter(run => run.role === 'synthesizer')
  const shown = new Set([supervisor?.id, ...semantic.map(run => run.id), ...workers.map(run => run.id), ...verifiers.map(run => run.id), ...synthesizers.map(run => run.id)])
  const other = runs.filter(run => !shown.has(run.id))
  const solo = !supervisor && semantic.length === 0 && verifiers.length === 0
    && synthesizers.length === 0 && workers.length + other.length === 1
  const childRuns = runs.filter(run => run.parent === active?.id)
  const parent = runs.find(run => run.id === active?.parent)
  const workerOverlap = workers.some((run, i) => workers.slice(i + 1).some(otherRun =>
    run.startMs !== null && run.endMs !== null && otherRun.startMs !== null && otherRun.endMs !== null
    && run.startMs < otherRun.endMs && otherRun.startMs < run.endMs))
  const owner = new Map<ReplayStep, RunView>()
  runs.forEach(run => run.steps.forEach(step => owner.set(step, run)))
  const events = steps.map((step, index) => ({
    time: Number.isFinite(step.start_ms) ? step.start_ms as number : null,
    order: index,
    label: owner.get(step)?.label || '系统',
    note: preview(step.note || STEP_NAMES[step.step] || step.step, 65),
    status: stepFailed(step.status) ? 'failed' : stepSoft(step.status) ? 'soft' : 'ok',
  }))
  if (events.every(event => event.time !== null)) events.sort((a, b) => (a.time as number) - (b.time as number) || a.order - b.order)
  const card = (run: RunView, extra = '') => <button type="button" key={run.id}
    className={'proto-agent-card ' + extra + ' ' + run.status + (active?.id === run.id ? ' active' : '')}
    aria-pressed={active?.id === run.id} onClick={() => setSelected(run.id)}>
    <span className={'proto-avatar ' + roleClass(run.role)}>{icon(run.role)}</span>
    <span className="proto-agent-main"><span className="proto-agent-title">{run.label}<em>AGENT</em></span>
      <small>{duration(run.elapsedMs)}{run.startMs === null ? ' 累计' : ''} · {run.tokens ? run.tokens.toLocaleString() + ' tok' : '—'} · {run.steps.length} 个 Span</small>
      {run.role !== 'query_worker' && <span className="proto-agent-task" title={run.task}>{run.task}</span>}
      <span className="proto-agent-spans">{run.steps.slice(0, 3).map((step, index) => <span className="proto-span-pill" key={index}>{STEP_NAMES[step.step] || step.step}<b className={'proto-span-kind ' + spanKind(step).toLowerCase()}>{spanKind(step)}</b></span>)}
        {run.steps.length > 3 && <span className="proto-span-pill">+{run.steps.length - 3} Span</span>}
      </span>
    </span>
    <b className={'proto-agent-status ' + run.status}>● {statusLabel(run.status)}</b>
    {run.role === 'query_worker' && <span className="proto-agent-task worker-task" title={run.task}>{run.task}</span>}
  </button>
  const connector = (label: string) => <div className="proto-connector"><span>{label}</span></div>
  return <section className="agent-topology proto-topology">
    <header className="agent-topology-head"><strong>{solo ? '智能体执行' : '智能体协作拓扑'}</strong><span>AGENT RUN GRAPH · Trace {traceId.slice(0, 8)} · 点击智能体查看执行明细</span><div className="topology-legend"><i className="ok" />成功 <i className="soft" />降级 <i className="failed" />失败</div></header>
    <div className="agent-workspace proto-workspace">
      <div className="proto-graph"><div className={'proto-flow' + (solo ? ' proto-flow-solo' : '')}>
        {solo && card(runs[0], 'proto-root-card')}
        {!solo && supervisor && card(supervisor, 'proto-root-card')}
        {!solo && semantic.map(run => <Fragment key={run.id}>{connector('语义分析')}{card(run, 'proto-root-card')}</Fragment>)}
        {!solo && workers.length > 0 && <>
          {connector('派发 ' + workers.length + ' 个子任务')}
          <section className="proto-worker-stage"><div className="proto-stage-head"><strong>工作智能体</strong><span><b>{workers.length} 个 Agent Run</b> · {workers.every(run => run.startMs !== null) ? workerOverlap ? '并行执行' : '按实际时序' : '开始时间未记录'}</span></div>
            <div className={'proto-worker-grid columns-' + Math.min(workers.length, 3)}>{workers.map(run => card(run, 'proto-worker-card'))}</div>
          </section>
        </>}
        {!solo && verifiers.map(run => <Fragment key={run.id}><div className="proto-fan-in"><span>检查 Worker 输出</span></div>{card(run, 'proto-root-card proto-final-card')}</Fragment>)}
        {!solo && synthesizers.map(run => <Fragment key={run.id}>{connector('合成答案')}{card(run, 'proto-root-card proto-final-card')}</Fragment>)}
        {!solo && other.map(run => <Fragment key={run.id}>{connector('后续智能体')}{card(run, 'proto-root-card')}</Fragment>)}
      </div>
        <div className="card proto-detail-in-graph"><AgentExecutionDetail runs={runs} steps={steps} traceId={traceId} selected={active?.id || ''} onSelect={setSelected} /></div>
      </div>
      <aside className="proto-sidebar">
        {active && <section className="proto-sidecard"><header>当前智能体 <b className={active.status}>● {statusLabel(active.status)}</b></header><div className="proto-sidebody">
          <div className="proto-inspector-title"><i className={'proto-avatar ' + roleClass(active.role)}>{icon(active.role)}</i><span><strong>{active.label}</strong><small>{active.id}</small></span><em>AGENT RUN</em></div>
          <p className="proto-inspector-output">{active.task}</p>
          <dl><div><dt>父级智能体</dt><dd>{parent?.label || (active.parent || (solo ? '—' : 'Root'))}</dd></div><div><dt>执行耗时</dt><dd>{duration(active.elapsedMs)}{active.startMs === null ? ' 累计' : ''}</dd></div><div><dt>Span 数量</dt><dd>{active.steps.length}</dd></div><div><dt>Token 用量</dt><dd>{active.tokens ? active.tokens.toLocaleString() + ' tok' : '—'}</dd></div></dl>
          <small className="proto-section-label">TASK OUTPUT</small><p className="proto-output-box">{active.output}</p>
          <small className="proto-section-label">CHILD AGENTS</small><div className="proto-child-list">{childRuns.length ? childRuns.map(run => <button type="button" key={run.id} onClick={() => setSelected(run.id)}>{run.label}</button>) : <span>无</span>}</div>
        </div></section>}
        <section className="proto-sidecard"><header>执行事件 <small>{events.every(event => event.time !== null) ? 'RELATIVE TIME' : 'RECORD ORDER'}</small></header><div className="proto-event-list">{events.map(event => <div key={event.order}><time>{event.time === null ? '#' + (event.order + 1) : '+' + duration(event.time)}</time><i className={event.status} /><span><b>{event.label}</b> {event.note}</span></div>)}</div></section>
        <section className="proto-sidecard proto-legend-card"><header>标识说明</header><div><span><i>A</i> Agent Run</span><b>智能体实例</b></div><div><span><em>LLM</em></span><b>模型调用 Span</b></div><div><span><em className="tool">TOOL</em></span><b>工具调用 Span</b></div><div><span><em className="query">QUERY</em></span><b>检索 / 查询 Span</b></div><div><span><em className="sys">SYS</em></span><b>系统操作 Span</b></div></section>
      </aside>
    </div>
  </section>
}
