import { useEffect, useState } from 'react'
import { fetchSchema, type Schema, type SchemaMetric } from '../api'
import { PageHeader } from '../components/AppShell'
import type { View } from '../types'

const KIND_LABEL: Record<string, string> = {
  expr: '表达式 · 进 SELECT',
  predicate: '谓词 · 进 WHERE',
}

export function GlossaryPage({ onNavigate }: { onNavigate: (view: View) => void }) {
  const [schema, setSchema] = useState<Schema | null>(null)
  const [error, setError] = useState('')
  const [picked, setPicked] = useState('')
  const [showAdd, setShowAdd] = useState(false)

  useEffect(() => {
    let alive = true
    fetchSchema()
      .then(value => { if (alive) setSchema(value) })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [])

  const metrics = schema?.metrics ?? []
  // 三五条数据，直接算 —— useMemo 依赖的是每次渲染都新建的数组，
  // 缓存不了任何东西，只会多一条 lint 噪音
  const current = metrics.find(m => m.name === picked) ?? metrics[0]

  return (
    <div className="page">
      <PageHeader
        eyebrow="Phase 2 · Business Semantics"
        title="业务口径"
        description="统一指标定义，让模型与人用同一种业务语言。口径写不下来，模型就只能猜，而猜错时输出仍然看起来合理。"
        action={<button className="primary" onClick={() => setShowAdd(true)}>＋ 新建指标</button>}
      />

      {error && <div className="audit-error">读取业务口径失败：{error}</div>}

      {schema && metrics.length === 0 ? (
        <section className="card notice-card">
          <h3>当前没有可见的业务口径</h3>
          <p>
            可能是这份配置没有定义口径，也可能是当前角色看不到口径所依赖的表 ——
            口径引用的表若不可见，口径会一并摘掉。留着只会让模型照口径写出引用
            不可见表的 SQL，然后被 R-03 拦下，报错指向一个无法理解的地方。
          </p>
          <div className="nr-act">
            <button className="secondary" onClick={() => onNavigate('sources')}>看当前可见的表 →</button>
          </div>
        </section>
      ) : (
        <div className="glossary-layout">
          <aside className="card term-list">
            {metrics.map(m => (
              <button
                key={m.name}
                className={current?.name === m.name ? 'active' : ''}
                onClick={() => setPicked(m.name)}
              >
                <span>
                  <strong>{m.name}</strong>
                  <small>{m.scope.join('、') || '未声明来源表'} · {KIND_LABEL[m.kind] ?? '未定义'}</small>
                </span>
                <b>{m.aliases.length}</b>
              </button>
            ))}
          </aside>

          {current && <MetricDetail metric={current} onNavigate={onNavigate} />}
        </div>
      )}

      {showAdd && <AddMetricModal onClose={() => setShowAdd(false)} />}
    </div>
  )
}

function MetricDetail({ metric, onNavigate }: {
  metric: SchemaMetric
  onNavigate: (view: View) => void
}) {
  return (
    <section className="card definition">
      {/* 原型这里写「VERIFIED METRIC · FINANCE」并标 v3.2 已认证。
          askdb 的口径模型里没有域、没有版本、没有认证状态 —— 那三样都是
          设计稿的虚构。写上去会让人以为有一套评审流程在背后。 */}
      <div className="eyebrow">BUSINESS METRIC</div>
      <h2>{metric.name}</h2>
      <p>{metric.note || '这条口径没有写说明。'}</p>

      <pre className="sql-code">{metric.definition || '（未定义）'}</pre>

      <div className="permission-grid">
        <div><span>负责人</span><strong>{metric.owner || '未指定'}</strong></div>
        <div><span>用法</span><strong>{KIND_LABEL[metric.kind] ?? '未定义'}</strong></div>
        <div><span>来源表</span><strong>{metric.scope.join('、') || '—'}</strong></div>
        <div><span>同义词</span><strong>{metric.aliases.length} 个</strong></div>
      </div>

      <div className="rule-row">
        <span>01</span>
        <div>
          <strong>命中即强制使用</strong>
          <small>
            问题里出现下列任一说法，这条定义会被注入提示词，模型不得自行构造：
            {[metric.name, ...metric.aliases].map(a => <em className="tag" key={a}>{a}</em>)}
          </small>
        </div>
      </div>
      <div className="rule-row">
        <span>02</span>
        <div>
          <strong>来源表不可见时整条摘掉</strong>
          <small>
            口径引用的表若不在当前角色的可见范围内，这条口径不会进提示词 ——
            否则模型会照它写出引用不可见表的 SQL，然后被 R-03 拦下。
          </small>
        </div>
      </div>

      <div className="nr-act">
        <button className="secondary" onClick={() => onNavigate('sources')}>查看来源表结构 →</button>
      </div>
    </section>
  )
}

/** 新建指标不是一个表单。
 *
 *  口径直接决定模型怎么写 SQL —— 写错不会报错，只会让答案"看起来合理"。
 *  它和表白名单一样跟着配置文件走，改动应当经过评审并留在版本库里，
 *  而不是在页面上点几下就生效、事后谁也说不清是谁改的。
 */
function AddMetricModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onMouseDown={e => { if (e.currentTarget === e.target) onClose() }}>
      <div className="modal">
        <header className="modal-dark">
          <div>
            <h3>新建指标</h3>
            <p>口径跟着配置文件走，页面不参与 —— 下面是实际要做的事。</p>
          </div>
          <button onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="modal-body">
          <p className="drawer-note">
            口径直接决定模型怎么写 SQL。写错不会报错，只会让答案<b>看起来合理</b> ——
            所以它和表白名单一样属于要评审、要留痕、要能回滚的东西，
            不该在页面上点几下就生效。
          </p>
          <ol className="add-steps">
            <li>
              <b>在配置里加一条</b>
              <span>
                改这份实例的 <code>*-metrics.yaml</code>。
                <code>expr</code> 直接进 SELECT 列表，<code>predicate</code> 进 WHERE，二选一。
              </span>
              <pre className="sql-code">{`- name: 文档数
  aliases: [文档数量, 已入库文档]
  scope: [documents]
  expr: "COUNT(*) FILTER (WHERE status = 'COMPLETED')"
  owner: 平台组
  note: 口径为 COMPLETED 的数量，不是全部行数`}</pre>
            </li>
            <li>
              <b>把同义词写全</b>
              <span>
                命中靠的就是 name 与 aliases。漏一个说法，模型就会绕过这条定义
                自行构造 —— 而那正是"结果看起来合理但口径不对"的来源。
              </span>
            </li>
            <li>
              <b>重启这份配置的进程</b>
              <span>口径在启动时加载并做一致性校验：scope 引用了白名单之外的表会直接拒绝启动。</span>
              <pre className="sql-code">python -m askdb.cli serve -c config/你的配置.yaml</pre>
            </li>
          </ol>
          <div className="modal-actions">
            <button className="primary" onClick={onClose}>知道了</button>
          </div>
        </div>
      </div>
    </div>
  )
}
