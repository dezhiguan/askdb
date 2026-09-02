import { useEffect, useMemo, useState } from 'react'
import { checkMetrics, fetchSchema, type MetricCheck, type Schema, type SchemaMetric } from '../api'
import { MetricConfigHelp } from '../components/MetricConfigHelp'
import { PageHeader } from '../components/AppShell'
import type { View } from '../types'

const KIND_LABEL: Record<string, string> = {
  expr: '表达式 · 进 SELECT',
  predicate: '谓词 · 进 WHERE',
}

export function GlossaryPage({ onNavigate, notify }: {
  onNavigate: (view: View) => void
  notify: (message: string) => void
}) {
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
  const tableNames = useMemo(
    () => (schema?.tables ?? []).map(t => t.name),
    [schema?.tables],
  )
  const current = metrics.find(m => m.name === picked) ?? metrics[0]

  // 区分度：按定义算 vs 凭直觉算差多少。按需跑 —— 每条口径一次库查询
  const [checks, setChecks] = useState<Record<string, MetricCheck>>({})
  const [checking, setChecking] = useState(false)

  const runCheck = async () => {
    setChecking(true)
    try {
      const r = await checkMetrics()
      setChecks(Object.fromEntries(r.items.map(i => [i.name, i])))
      notify(`已按当前数据核对 ${r.items.length} 条口径的区分度`)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="Phase 2 · Business Semantics"
        title="业务口径"
        description="统一指标定义，让模型与人用同一种业务语言。口径写不下来，模型就只能猜，而猜错时输出仍然看起来合理。"
        action={
          <div className="card-actions">
            <button className="secondary" disabled={checking || !metrics.length} onClick={runCheck}>
              {checking ? '核对中…' : '核对区分度'}
            </button>
            <button className="ghost" onClick={() => setShowAdd(true)}>如何新增</button>
          </div>
        }
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
                {checks[m.name]?.status === 'ok' && !checks[m.name].differs
                  ? <b className="degenerate" title="两种写法结果相同，当前检验不出模型是否真的用了它">退化</b>
                  : <b>{m.aliases.length}</b>}
              </button>
            ))}
          </aside>

          {current && <MetricDetail metric={current} check={checks[current.name]} onNavigate={onNavigate} />}
        </div>
      )}

      {showAdd && (
        <MetricConfigHelp tables={tableNames} onClose={() => setShowAdd(false)} />
      )}
    </div>
  )
}

function MetricDetail({ metric, check, onNavigate }: {
  metric: SchemaMetric
  check?: MetricCheck
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

      {/* 粒度是硬约束，单独一块 —— 它管的不是表达式对不对，
          而是这个表达式能不能被放进别的聚合语境 */}
      {metric.grain && (
        <div className="notice info grain-note">
          <div className="t">聚合粒度</div>
          <div className="why">{metric.grain}</div>
        </div>
      )}

      <Discrimination check={check} />

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



/** 区分度：按定义算 vs 凭直觉算差多少。
 *
 *  这是这页唯一无法靠翻配置文件替代的东西 —— 口径写错不报错、不越权，
 *  护栏 R-01～R-17 一条都不会触发，那么它自己就必须有别的方式被检验。
 *  两种写法结果相同的口径当前检验不出模型有没有真的用它，也不该拿来出评测题。
 */
function Discrimination({ check }: { check?: MetricCheck }) {
  if (!check) {
    return (
      <p className="drawer-note">
        区分度未核对。点右上角「核对区分度」按当前数据实算一次 ——
        每条口径一次库查询。
      </p>
    )
  }

  if (check.status !== 'ok') {
    return (
      <div className={`notice ${check.status === 'blocked' ? 'bad' : 'info'} grain-note`}>
        <div className="t">区分度：{check.status === 'skipped' ? '无法核对' : '核对失败'}</div>
        <div className="why">{check.detail}</div>
      </div>
    )
  }

  const same = !check.differs
  return (
    <div className={`notice ${same ? 'bad' : 'info'} grain-note`}>
      <div className="t">区分度：{same ? '当前退化' : '有效'}</div>
      <div className="why">
        按口径算 <b className="num-cell">{fmt(check.value)}</b>
        ，凭直觉算 <b className="num-cell">{fmt(check.naive)}</b>。
        {same
          ? ' 两种写法结果相同 —— 这条口径当前检验不出模型有没有真的用它，也不该拿来出评测题。'
          : ' 差异真实存在，模型不用这条定义就会答错。'}
      </div>
    </div>
  )
}

function fmt(v: MetricCheck['value']): string {
  if (v == null) return '—'
  if (typeof v === 'number') {
    return Number.isInteger(v) ? v.toLocaleString() : v.toPrecision(4)
  }
  return String(v)
}
