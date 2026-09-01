import { useState } from 'react'
import { PageHeader } from '../components/AppShell'

export function GlossaryPage({ notify }: { notify: (message: string) => void }) {
  const [term, setTerm] = useState('退款金额')
  return <div className="page"><PageHeader eyebrow="Phase 2 · Business Semantics" title="业务口径中心" description="统一指标定义，让模型、开发、测试和产品使用同一种业务语言。" action={<button className="primary" onClick={() => notify('新指标草稿已创建')}>＋ 新建指标</button>} />
    <div className="glossary-layout"><section className="card term-list"><input placeholder="搜索指标或字段…" />{['退款金额','支付失败订单','新客首单转化率','物流超时率'].map(item => <button className={term === item ? 'active' : ''} key={item} onClick={() => setTerm(item)}><strong>{item}</strong><small>财务域 · 已认证</small></button>)}</section>
      <section className="card definition"><div className="eyebrow">VERIFIED METRIC · FINANCE</div><h2>{term}</h2><p>统计指定时间范围内，状态为“已完成”的实际金额。仅统计业务系统确认成功的流水，取消申请和处理中记录不计入。</p><pre>SUM(refund_transactions.actual_amount){'\n'}WHERE refund_status = 'SUCCEEDED'</pre><div className="permission-grid"><div><span>负责人</span><strong>财务数据组</strong></div><div><span>当前版本</span><strong>V3.2</strong></div><div><span>来源表</span><strong>refund_transactions</strong></div><div><span>更新时间</span><strong>2026-08-21</strong></div></div></section>
    </div>
  </div>
}
