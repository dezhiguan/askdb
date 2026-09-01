import { useState } from 'react'
import type { ResultTab, View } from '../types'

const sql = `-- policy: PROD_RO / max_rows: 100 / timeout: 15s
SELECT failure_reason, COUNT(*) AS order_count
FROM payment_orders
WHERE status = 'FAILED'
  AND created_at >= current_date
GROUP BY failure_reason
ORDER BY order_count DESC
LIMIT 100;`

const scenarios = {
  input: ['信息缺失 → 用户补充', '缺少时间范围与统计口径，任务在生成 SQL 前暂停。', '查询发起人', 'GENERATE_SQL#04'],
  approval: ['高风险 / 高成本 → 数据负责人审批', '预计扫描 2.8M 行、成本 87%，命中人工审批策略。', '数据负责人', 'QUERY#05'],
  schema: ['Schema 漂移 → 开发者复核', '字段绑定结果与保存版本不一致。', '数据开发者', 'GENERATE_SQL#04'],
  retry: ['连接器瞬时失败 → 自动重试', '只读副本连接超时，幂等查询按退避策略恢复。', '系统自动', 'QUERY#05'],
} as const

export function ResultTabs({ active, onChange, onNavigate, notify }: {
  active: ResultTab
  onChange: (tab: ResultTab) => void
  onNavigate: (view: View) => void
  notify: (message: string) => void
}) {
  const tabs: [ResultTab, string][] = [['result', '查询结果'], ['sql', '原生 SQL'], ['chain', '执行链路'], ['checkpoint', '人工介入 / 断点恢复 · 4']]
  return (
    <div className="result-area">
      <div className="answer-card">
        <div><strong>今天共有 18 笔支付失败订单</strong><p>相比昨日同期下降 14.3%。其中 11 笔为支付回调超时，5 笔为余额不足，2 笔为风控拒绝。</p></div>
        <div className="answer-actions"><button onClick={() => onChange('sql')}>查看原生 SQL</button><button onClick={() => onNavigate('traces')}>查看执行链路</button></div>
        <div className="evidence-strip">
          {[
            ['查询 ID', 'QRY-183108-7A2F'],
            ['SQL SHA-256', '8ad2…91cf'],
            ['数据快照', '2026-08-28 18:31'],
            ['返回 / 扫描', '3 / 1,842 ROWS'],
          ].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
        </div>
      </div>
      <div className="result-tabs">
        {tabs.map(([id, label]) => <button className={active === id ? 'active' : ''} key={id} onClick={() => onChange(id)}>{label}</button>)}
      </div>
      {active === 'result' && <ResultTable />}
      {active === 'sql' && <SqlPane notify={notify} />}
      {active === 'chain' && <ExecutionChain onNavigate={onNavigate} />}
      {active === 'checkpoint' && <CheckpointPane notify={notify} />}
    </div>
  )
}

function ResultTable() {
  return (
    <div className="table-scroll"><table><thead><tr><th>失败原因</th><th>订单数</th><th>占比</th><th>较昨日</th><th>示例用户</th></tr></thead>
      <tbody>
        <tr><td>支付回调超时</td><td className="good">11</td><td>61.1%</td><td>-8.3%</td><td><span className="mask">138****0281</span></td></tr>
        <tr><td>余额不足</td><td className="good">5</td><td>27.8%</td><td>-20.0%</td><td><span className="mask">186****9270</span></td></tr>
        <tr><td>风控拒绝</td><td className="good">2</td><td>11.1%</td><td>持平</td><td><span className="mask">139****4882</span></td></tr>
      </tbody></table></div>
  )
}

function SqlPane({ notify }: { notify: (message: string) => void }) {
  const copy = async () => {
    await navigator.clipboard?.writeText(sql).catch(() => undefined)
    notify('原生 SQL 已复制')
  }
  return <div className="sql-pane"><div className="sql-toolbar"><span>PostgreSQL 15 · SHA-256: 8ad2…91cf · 未经格式改写</span><button onClick={copy}>复制原生 SQL</button></div><pre>{sql}</pre></div>
}

function ExecutionChain({ onNavigate }: { onNavigate: (view: View) => void }) {
  return <div><div className="mini-trace">{['身份与权限 · 12ms', '元数据检索 · 83ms', '模型生成 SQL · 740ms', 'SQL Guard · 18ms', '数据库查询 · 480ms', '结果解释 · 310ms'].map((step, index) => <span key={step}><strong>{step.split(' · ')[0]}</strong><small>{step.split(' · ')[1]}</small>{index < 5 && <i>→</i>}</span>)}</div><div className="align-right"><button className="secondary" onClick={() => onNavigate('traces')}>打开完整 Trace</button></div></div>
}

function CheckpointPane({ notify }: { notify: (message: string) => void }) {
  const [scenario, setScenario] = useState<keyof typeof scenarios>('input')
  const [state, setState] = useState<'WAITING' | 'RESUMING' | 'COMPLETED'>('WAITING')
  const info = scenarios[scenario]
  const resume = () => {
    if (state === 'COMPLETED') { setState('WAITING'); return }
    setState('RESUMING'); notify('正在重验权限与 Schema')
    window.setTimeout(() => { setState('COMPLETED'); notify('断点恢复完成 · 已完成节点未重跑') }, 1200)
  }
  return (
    <div className="checkpoint-console">
      <aside>
        <strong>选择中断场景</strong>
        {(Object.keys(scenarios) as (keyof typeof scenarios)[]).map(key => <button className={scenario === key ? 'active' : ''} key={key} onClick={() => { setScenario(key); setState('WAITING') }}>{scenarios[key][0].split(' → ')[0]}</button>)}
        <small>仅在检查点持久化最小状态；续跑不会重做已完成节点。</small>
      </aside>
      <section>
        <div className="checkpoint-head"><div><strong>{info[0]}</strong><p>{info[1]}</p></div><span className={`state ${state.toLowerCase()}`}>{state}</span></div>
        <div className="checkpoint-facts"><div><span>责任角色</span><code>{info[2]}</code></div><div><span>精确恢复节点</span><code>{info[3]}</code></div><div><span>CHECKPOINT</span><code>意图 + AuthZ + 候选表</code></div></div>
        <div className="checkpoint-flow">{['理解问题', '权限校验', '人工检查点', '生成 / 护栏', '只读查询', '结果解释'].map((label, index) => <div className={state === 'COMPLETED' || index < 2 ? 'done' : index === 2 ? 'paused' : ''} key={label}><i>{index + 1}</i><strong>{label}</strong><small>{state === 'COMPLETED' || index < 2 ? 'DONE' : index === 2 ? state : 'PENDING'}</small></div>)}</div>
        <div className="resume-proof"><div><strong>恢复前强制重验 · 不是盲目续跑</strong><small>权限令牌 / 数据范围 + Schema v42 / 字段映射</small></div><span>✓ 权限 · ✓ Schema</span></div>
        <div className="checkpoint-footer"><small>{state === 'COMPLETED' ? `已从 ${info[3]} 完成续跑` : '已完成节点将沿用 checkpoint，不会重新执行'}</small><button onClick={resume}>{state === 'COMPLETED' ? '重置' : state === 'RESUMING' ? '恢复中…' : '确认并续跑'}</button></div>
      </section>
    </div>
  )
}
