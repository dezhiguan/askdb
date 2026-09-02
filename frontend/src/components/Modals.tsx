import { useMemo, useState } from 'react'
import type { ModalName } from '../types'

export function ModalLayer({ active, onClose, notify }: {
  active: ModalName
  onClose: () => void
  notify: (message: string) => void
}) {
  if (!active) return null
  const finish = (message: string) => { onClose(); notify(message) }
  return (
    <div className="modal-backdrop" onMouseDown={event => { if (event.currentTarget === event.target) onClose() }}>
      {active === 'clarification' && <ClarificationModal onClose={onClose} onFinish={() => finish('补充信息已写入任务状态 · 已从 INTERRUPT 节点恢复')} />}
      {active === 'langfuse' && <FormModal title="接入 Langfuse" description="配置可观测平台；敏感内容默认不上报。" onClose={onClose}>
        <label>Langfuse Host<input defaultValue="https://langfuse.company.internal" /></label>
        <div className="form-grid"><label>Public Key<input defaultValue="pk-lf-••••••" /></label><label>Secret Key / Vault<input type="password" defaultValue="vault://observability/langfuse" /></label></div>
        <div className="rule-row"><span>01</span><div><strong>上报 Trace 元数据</strong><small>耗时、Token、模型、状态和工具名称。</small></div><button className="toggle on"><i /></button></div>
        <div className="modal-actions"><button className="ghost" onClick={onClose}>取消</button><button className="secondary" onClick={() => notify('Langfuse 连接成功 · Trace Schema 兼容')}>测试连接</button><button className="primary" onClick={() => finish('Langfuse 集成已保存 · 隐私上报策略已生效')}>保存集成</button></div>
      </FormModal>}
    </div>
  )
}

function FormModal({ title, description, onClose, children }: { title: string; description: string; onClose: () => void; children: React.ReactNode }) {
  return <div className="modal"><header><div><h3>{title}</h3><p>{description}</p></div><button onClick={onClose}>×</button></header><div className="modal-body">{children}</div></div>
}

function ClarificationModal({ onClose, onFinish }: { onClose: () => void; onFinish: () => void }) {
  const [values, setValues] = useState({ period: '最近 7 天', metric: '成功退款金额', group: '按支付渠道', source: '财务只读库' })
  const preview = useMemo(() => Object.values(values).join(' · '), [values])
  const group = (key: keyof typeof values, options: string[]) => <div className="clarify-options">{options.map(option => <label key={option}><input type="radio" checked={values[key] === option} onChange={() => setValues(current => ({ ...current, [key]: option }))} /><span><strong>{option}</strong><small>结构化任务参数</small></span></label>)}</div>
  return <div className="modal clarification-modal"><header><div><span className="eyebrow">TASK-0831 · LANGGRAPH INTERRUPT</span><h3>任务需要补充信息</h3><p>补充内容将写入当前任务状态，然后从暂停节点继续执行。</p></div><button onClick={onClose}>×</button></header><div className="modal-body">
    <div className="clarify-alert"><strong>问题“查询退款金额”存在关键歧义</strong><span className="status wait">WAITING FOR INPUT</span></div>
    <div className="clarify-progress">{['01 理解问题', '02 检查完整性', '03 等待补充', '04 生成 SQL', '05 安全执行'].map((step, i) => <span className={i < 2 ? 'done' : i === 2 ? 'active' : ''} key={step}>{step}</span>)}</div>
    <div className="clarify-field"><b>时间范围</b>{group('period', ['今天', '昨天', '最近 7 天'])}</div>
    <div className="clarify-field"><b>退款口径</b>{group('metric', ['成功退款金额', '退款申请金额'])}</div>
    <div className="clarify-field"><b>统计维度</b>{group('group', ['仅汇总', '按支付渠道', '按退款原因'])}</div>
    <div className="clarify-field"><b>数据源</b>{group('source', ['财务只读库', '订单中心只读镜像'])}</div>
    <div className="clarify-preview"><span>RESUME SPEC · 结构化任务参数</span><strong>{preview}</strong><small>补充参数不会保存为对话历史，也不会影响其他任务。</small></div>
    <div className="modal-actions"><button className="ghost" onClick={onClose}>稍后处理</button><button className="primary" onClick={onFinish}>确认并继续执行</button></div>
  </div></div>
}
