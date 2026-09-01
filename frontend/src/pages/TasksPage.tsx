import { useState } from 'react'
import { PageHeader } from '../components/AppShell'

export function TasksPage({ onClarify, notify }: { onClarify: () => void; notify: (message: string) => void }) {
  const [approved, setApproved] = useState(false)
  const tasks = [
    ['?', '查询退款金额', 'TASK-0831 · 林晓 · 刚刚', '缺少条件', '3 项', '等待补充'],
    ['04', '昨日高额退款订单排查', 'TASK-0827 · 林晓 · 17:42', '预计扫描', '82K', approved ? '已完成' : '等待确认'],
    ['▶', '近七天物流超时趋势', 'TASK-0819 · 周琪 · 17:36', '当前节点', 'QUERY', '执行中'],
    ['✓', '新客首单转化率', 'TASK-0808 · 陈晨 · 16:05', '耗时', '2.3S', '已完成'],
    ['!', '批量导出用户支付记录', 'TASK-0796 · 王凯 · 15:48', '原因', 'PII', '已拦截'],
  ]
  return <div className="page"><PageHeader eyebrow="Phase 1 · Governed Tasks" title="任务中心" description="需要确认、耗时较长或包含复杂分析步骤的查询会自动升级为任务。" action={<button className="primary" onClick={() => notify('已创建空白任务草稿 · 上下文独立')}>＋ 创建任务</button>} />
    <div className="stats">{[['运行中','3','均在安全阈值内'],['待处理','3','1 补充信息 · 2 审批'],['今日完成','28','成功率 96.7%'],['已拦截','4','越权或写入意图']].map(stat => <div key={stat[0]}><span>{stat[0]}</span><strong>{stat[1]}</strong><small>{stat[2]}</small></div>)}</div>
    <section className="card"><div className="card-head"><div><strong>查询任务</strong><p>每个任务拥有独立状态、执行轨迹和审计记录。</p></div><button className="ghost">全部状态⌄</button></div><div className="table-scroll">
      {tasks.map((task, index) => <div className="task-row" key={task[1]}><i className={index === 0 || index === 4 ? 'warn' : ''}>{task[0]}</i><div className="task-main"><strong>{task[1]}</strong><small>{task[2]}</small></div><div><span>{task[3]}</span><strong>{task[4]}</strong></div><span className={`status ${index === 0 || index === 1 || index === 4 ? 'wait' : ''}`}>{task[5]}</span><button className={index === 4 ? 'danger' : index > 1 ? 'ghost' : 'primary'} onClick={() => { if (index === 0) onClarify(); else if (index === 1) { setApproved(true); notify('任务已从审批节点恢复并执行完成') } else notify('任务详情已打开（样例数据，未接后端）') }}>{index === 0 ? '补充信息' : index === 1 ? approved ? '查看结果' : '批准执行' : index === 4 ? '查看原因' : '查看轨迹'}</button></div>)}
    </div></section>
  </div>
}
