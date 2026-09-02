import { useEffect } from 'react'

/** 新增口径不是一个表单。
 *
 *  口径直接决定模型怎么写 SQL —— 写错不报错，只让答案「看起来合理」，
 *  护栏一条都不会触发。它和表白名单一样属于要评审、要留痕、要能回滚的东西，
 *  不该在页面上点几下就生效、事后谁也说不清是谁改的。
 *
 *  这里给的是实际要做的事，附当前实例可用的表名。
 */
export function MetricConfigHelp({ tables, onClose }: {
  tables: string[]
  onClose: () => void
}) {
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
            <h3>如何新增业务口径</h3>
            <p>口径跟着配置文件走，页面不参与 —— 下面是实际要做的事。</p>
          </div>
          <button onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className="modal-body">
          <p className="drawer-note">
            口径直接决定模型怎么写 SQL。写错<b>不会报错</b> —— 语法正确、表在白名单里、
            租户谓词照样注入，护栏 R-01～R-17 一条都不触发，只是答案错了。
            所以它属于要评审、要留痕、要能回滚的东西，不该在页面上点几下就生效。
          </p>
          <ol className="add-steps">
            <li>
              <b>在配置里加一条</b>
              <span>
                改这份实例的 <code>*-metrics.yaml</code>。
                <code>expr</code> 进 SELECT 列表，<code>predicate</code> 进 WHERE，二选一。
              </span>
              <pre className="sql-code">{`- name: 日均成本
  aliases: [每天花多少钱, 平均日成本, 日均花费]
  scope: [model_usage_daily]
  expr: "SUM(cost) / NULLIF(COUNT(DISTINCT stat_date), 0)"
  naive: "SUM(cost) / NULLIF(COUNT(*), 0)"
  grain: 全期一个数，不得再按模型或用途分组
  owner: 平台组
  note: 分母是有记录的天数，不是行数`}</pre>
            </li>
            <li>
              <b>把同义词写全</b>
              <span>
                命中靠的就是 name 与 aliases。漏一个说法，模型就会绕过这条定义
                自行构造 —— 那正是「结果看起来合理但口径不对」的来源。
              </span>
            </li>
            <li>
              <b>写上 naive 与 grain</b>
              <span>
                <code>naive</code> 是「凭直觉的写法」，用来自动算区分度：两种写法结果
                相同的口径，当前检验不出模型有没有真的用它。
                <code>grain</code> 声明这个表达式只在什么聚合语境下成立 ——
                <code>expr</code> 是片段注入，保证得了表达式本身，保证不了它被放进什么查询里。
              </span>
            </li>
            <li>
              <b>重启这份配置的进程</b>
              <span>
                口径在启动时加载并校验：<code>scope</code> 引用了白名单之外的表会直接拒绝启动。
                当前实例可用的表：{tables.length ? tables.join('、') : '（读取中）'}
              </span>
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
