import { useState } from 'react'
import { PageHeader } from '../components/AppShell'
import type { ModalName } from '../types'

export function ConnectorsPage({ openModal }: { openModal: (modal: ModalName) => void }) {
  const nodes = [['华东生产 VPC','connector-east-prod-01 · v1.4.2','3','12s','96'],['测试网络','connector-staging-02 · v1.4.2','5','8s','100'],['物流数据域','connector-wms-01 · v1.3.8','2','26s','72']]
  return <div className="page"><PageHeader eyebrow="Phase 3 · Distributed Data Plane" title="Connector 节点" description="Connector 部署在数据库所在网络，只接收经过授权和校验的查询计划。" action={<button className="primary" onClick={() => openModal('connector')}>＋ 部署 Connector</button>} />
    <div className="topology"><div><i>UI</i><strong>企业统一入口</strong><small>SSO · 策略 · Agent Harness</small></div><span /><div><i>CN</i><strong>本地 Connector</strong><small>mTLS · Outbound Only</small></div><span /><div><i>DB</i><strong>数据库安全域</strong><small>只读账号 · 私有网络</small></div></div>
    <div className="connector-grid">{nodes.map((node,index) => <article className="source-card" key={node[0]}><div className="source-top"><i className="db-icon">CN</i><span className={`card-status ${index === 2 ? 'off' : ''}`}>{index === 2 ? '● UPGRADE' : <><i className="online" /> ONLINE</>}</span></div><h3>{node[0]}</h3><p>{node[1]}</p><div className="mini-metrics"><div className="mini-metric"><span>数据库</span><strong>{node[2]}</strong></div><div className="mini-metric"><span>心跳</span><strong>{node[3]}</strong></div></div><div className="health-bar"><i style={{ width: `${node[4]}%` }} /></div></article>)}</div>
  </div>
}

export function DeveloperPage({ notify }: { notify: (message: string) => void }) {
  const tools = [['CLI · AVAILABLE','askdb CLI','在终端中提交自然语言查询，沿用统一身份、权限和审计策略。','brew install askdb'],['IDE · BETA','Cursor / VS Code 插件','选中代码中的订单号或错误信息，直接发起受控数据排查。','ext install askdb.ide'],['API · CONTROLLED','Task API','供 CI、告警和内部系统创建审计化的数据查询任务。','POST /v1/query-tasks']]
  return <div className="page"><PageHeader eyebrow="Phase 4 · Developer Experience" title="开发者工具" description="统一平台之外，为开发者提供受控的 CLI、IDE 插件和 API。" action={<button className="primary" onClick={() => notify('短时令牌 dst_•••••• 已生成 · 30 分钟后失效')}>生成短时令牌</button>} /><div className="tool-grid">{tools.map(tool => <article className="tool-card" key={tool[1]}><span>{tool[0]}</span><h3>{tool[1]}</h3><p>{tool[2]}</p><div className="code-line"><code>{tool[3]}</code><button onClick={() => { navigator.clipboard?.writeText(tool[3]); notify('命令已复制') }}>复制</button></div><button className="secondary">查看文档</button></article>)}</div></div>
}

const phaseData = [
  ['内网统一查询入口','用最小范围证明自然语言查数能够减少研发排障成本。','MVP · 4–6 周'],
  ['身份权限与治理','让产品、测试和开发在统一规则下安全使用生产数据。','GOVERNANCE'],
  ['分布式 Connector','连接不同 VPC、机房和网络隔离区的数据资源。','SCALE'],
  ['CLI 与 IDE 插件','把可信数据能力带回开发人员最常用的工作环境。','DX'],
]
export function RoadmapPage({ notify }: { notify: (message: string) => void }) {
  const [phase, setPhase] = useState(0)
  return <div className="page"><PageHeader eyebrow="Product Delivery Plan" title="四阶段落地路线" description="先验证查数价值，再补齐治理、跨网络接入和开发者体验。" action={<button className="ghost" onClick={() => notify('产品规划已准备导出（样例数据，未接后端）')}>导出规划</button>} />
    <div className="roadmap">{phaseData.map((item,index) => <button className={phase === index ? 'active' : ''} key={item[0]} onClick={() => setPhase(index)}><i>0{index + 1}</i><h3>{item[0]}</h3><p>{item[1]}</p><ul><li>统一身份与安全边界</li><li>可验证输出和完整审计</li><li>阶段化验收与推广</li></ul><span>{item[2]}</span></button>)}</div>
    <div className="phase-detail"><h3>阶段{['一','二','三','四'][phase]}验收目标</h3><p>{phaseData[phase][1]} 所有入口复用平台身份、权限、SQL 护栏和审计链，并具备清晰的验收指标。</p></div>
  </div>
}
