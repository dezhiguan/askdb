import { useState } from 'react'
import { PageHeader } from '../components/AppShell'

const roles = {
  product: ['产品角色 · Product', '允许查看生产只读镜像中的脱敏业务数据，不允许查看原始个人信息。', 'PROD-RO', '90 DAYS', 'MASKED', 'AGG ONLY'],
  dev: ['开发角色 · Developer', '允许访问开发和测试环境；生产排障需要临时审批。', 'DEV + STAGING', 'ALL TEST', 'PARTIAL', 'ALLOWED'],
  test: ['测试角色 · QA', '允许访问测试环境和合成数据，不能访问生产用户明细。', 'STAGING', '180 DAYS', 'MASKED', '≤ 10K'],
  owner: ['数据负责人 · Data Owner', '负责数据域策略、业务口径和高风险查询审批。', 'DOMAIN ALL', '365 DAYS', 'ON DEMAND', 'APPROVAL'],
}

export function PermissionsPage({ notify }: { notify: (message: string) => void }) {
  const [role, setRole] = useState<keyof typeof roles>('product')
  const [toggles, setToggles] = useState([true, true, true, true])
  const detail = roles[role]
  return <div className="page"><PageHeader eyebrow="Phase 2 · Identity & Access" title="身份与权限中心" description="企业 SSO 提供身份，RBAC 定义角色，ABAC 根据环境和数据属性动态收敛权限。" action={<button className="primary" onClick={() => notify('企业组织同步完成（样例数据，未接后端）')}>同步企业组织</button>} />
    <div className="policy-layout"><section className="card role-list"><div className="card-head"><strong>角色</strong></div>{(Object.keys(roles) as (keyof typeof roles)[]).map((key, index) => <button className={role === key ? 'active' : ''} key={key} onClick={() => setRole(key)}><span><strong>{['产品','开发','测试','数据负责人'][index]}</strong><small>{roles[key][2]}</small></span><b>{[24,38,17,6][index]}</b></button>)}</section>
      <section className="card permission-detail"><div className="eyebrow">ROLE POLICY</div><h2>{detail[0]}</h2><p>{detail[1]}</p><div className="permission-grid">{['环境范围','数据期限','敏感字段','导出权限'].map((label, index) => <div key={label}><span>{label}</span><strong>{detail[index + 2]}</strong></div>)}</div>
        {['生产环境强制只读','个人信息默认脱敏','高成本查询二次确认','查询结果禁止用于模型训练'].map((rule, index) => <div className="rule-row" key={rule}><span>P{String(index * 2 + 1).padStart(2,'0')}</span><div><strong>{rule}</strong><small>策略在 SQL 执行前强制应用并写入审计记录。</small></div><button className={`toggle ${toggles[index] ? 'on' : ''}`} onClick={() => { setToggles(current => current.map((value, i) => i === index ? !value : value)); notify('策略状态已更新（样例数据，未接后端）') }}><i /></button></div>)}
      </section></div>
  </div>
}

export function GlossaryPage({ notify }: { notify: (message: string) => void }) {
  const [term, setTerm] = useState('退款金额')
  return <div className="page"><PageHeader eyebrow="Phase 2 · Business Semantics" title="业务口径中心" description="统一指标定义，让模型、开发、测试和产品使用同一种业务语言。" action={<button className="primary" onClick={() => notify('新指标草稿已创建')}>＋ 新建指标</button>} />
    <div className="glossary-layout"><section className="card term-list"><input placeholder="搜索指标或字段…" />{['退款金额','支付失败订单','新客首单转化率','物流超时率'].map(item => <button className={term === item ? 'active' : ''} key={item} onClick={() => setTerm(item)}><strong>{item}</strong><small>财务域 · 已认证</small></button>)}</section>
      <section className="card definition"><div className="eyebrow">VERIFIED METRIC · FINANCE</div><h2>{term}</h2><p>统计指定时间范围内，状态为“已完成”的实际金额。仅统计业务系统确认成功的流水，取消申请和处理中记录不计入。</p><pre>SUM(refund_transactions.actual_amount){'\n'}WHERE refund_status = 'SUCCEEDED'</pre><div className="permission-grid"><div><span>负责人</span><strong>财务数据组</strong></div><div><span>当前版本</span><strong>V3.2</strong></div><div><span>来源表</span><strong>refund_transactions</strong></div><div><span>更新时间</span><strong>2026-08-21</strong></div></div></section>
    </div>
  </div>
}
