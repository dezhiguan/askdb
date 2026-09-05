import { PageHeader } from '../components/AppShell'
import { useCallback, useEffect, useState } from 'react'
import { decideApproval, fetchApprovals, type Approval, type ApprovalsResult } from '../api'

/* 高成本查询审批队列（设计文档 Q-08 / P07）。
 *
 * 这一页服务两种人，且**看到的不是同一份东西**：
 *   · 系统管理员 —— 全部申请，带放行/驳回；它是唯一的审批人
 *   · 其他角色   —— 只有自己提的，用来知道批没批、单号是多少
 * 收敛在服务端做（/api/approvals 按能力位过滤），这里只负责把差别显示清楚。
 *
 * 有意**不做**的两件事：
 *   · 不提供"代发起人执行"按钮。批准只是放行 R-11 一道，重跑由发起人自己做，
 *     服务端不替任何人执行 —— 否则审批人就可能跑出他自己查不到的数据。
 *   · 不显示结果集。审批人判断的是"该不该扫这么多行"，不是数据本身。
 */

const STATUS_CN: Record<Approval['status'], string> = {
  REQUESTED: '待审批',
  APPROVED: '已批准',
  REJECTED: '已驳回',
  CONSUMED: '已使用',
}

function fmt(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts || '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}`
}

export function ApprovalsPage({ notify }: { notify: (message: string) => void }) {
  const [data, setData] = useState<ApprovalsResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [notes, setNotes] = useState<Record<string, string>>({})

  const load = useCallback(() => {
    fetchApprovals()
      .then(value => { setData(value); setError('') })
      .catch(e => setError(String(e.message || e)))
  }, [])

  useEffect(() => { load() }, [load])

  const decide = async (item: Approval, approved: boolean) => {
    // 驳回必须写理由：申请人拿到的唯一信息就是这句话，空着等于让他去猜。
    const note = (notes[item.id] || '').trim()
    if (!approved && !note) { notify('驳回请写明理由 —— 申请人只看得到这句话'); return }
    setBusy(item.id)
    try {
      await decideApproval(item.id, approved, note)
      notify(approved ? `已放行 ${item.id}` : `已驳回 ${item.id}`)
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const items = data?.items ?? []
  const pending = items.filter(i => i.status === 'REQUESTED')

  return (
    <div className="page">
      <PageHeader
        title="高成本查询审批"
        description={data?.can_approve
          ? '预估扫描量超阈值的查询在这里等待放行。放行是一次性的，由发起人自己重跑。'
          : '你提交的高成本查询申请。批准后回到工作台，带上单号原样重发一次即可。'}
        action={<button className="secondary" onClick={load}>刷新</button>}
      />

      {error && <div className="audit-error">读取失败：{error}</div>}

      {data && !data.can_approve && (
        <section className="card notice-card">
          <h3>你看到的是自己提交的申请</h3>
          <p>
            审批权收敛在系统管理员。这样安排的原因是它没有任何数据访问权限，
            <b>永远不可能是查询的发起人</b> —— 自己批自己在结构上就不成立。
          </p>
        </section>
      )}

      <section className="card">
        <div className="card-head">
          <div>
            <strong>队列</strong>
            <p>共 {items.length} 条，其中 {pending.length} 条待处理</p>
          </div>
        </div>

        {!data && <div className="audit-empty">读取中…</div>}
        {data && !items.length && (
          <div className="audit-empty">
            还没有任何高成本查询申请。查询预估扫描量超过阈值时会自动登记到这里。
          </div>
        )}

        {items.map(item => (
          <article className="rule" key={item.id} style={{ alignItems: 'flex-start' }}>
            <i className="rule-no">{STATUS_CN[item.status]}</i>
            <div style={{ minWidth: 0, flex: 1 }}>
              <strong className="audit-question" title={item.question}>
                {item.question || '（无问题原文）'}
              </strong>
              <small>
                <span className="mono">{item.id}</span>
                {' · '}{item.user || '—'}
                {item.roles?.length ? `（${item.roles.join('+')}）` : ''}
                {' · '}{fmt(item.ts)}
                {' · 预估扫描 '}
                <b>{item.est_rows?.toLocaleString() ?? '—'}</b>
                {' 行，阈值 '}{item.threshold?.toLocaleString()}
              </small>
              {/* 审批人要看到真正会执行的那条 SQL（护栏改写后的），
                  否则他判断的是一个与实际执行不同的东西 */}
              <pre className="sql-code" style={{ marginTop: 8 }}>{item.sql}</pre>

              {item.status !== 'REQUESTED' && (
                <small>
                  {item.approver ? `${item.approver} 于 ${fmt(item.decided_ts || '')} 处理` : ''}
                  {item.note ? ` —— ${item.note}` : ''}
                </small>
              )}

              {data?.can_approve && item.status === 'REQUESTED' && (
                <div className="form-row" style={{ marginTop: 10 }}>
                  <input
                    placeholder="处理意见（驳回必填）"
                    value={notes[item.id] || ''}
                    onChange={e => setNotes({ ...notes, [item.id]: e.target.value })}
                  />
                  <button className="primary" disabled={busy === item.id}
                          onClick={() => decide(item, true)}>放行一次</button>
                  <button className="secondary" disabled={busy === item.id}
                          onClick={() => decide(item, false)}>驳回</button>
                </div>
              )}

              {!data?.can_approve && item.status === 'APPROVED' && (
                <div className="form-note">
                  已批准。回到工作台原样重发这条查询，并带上单号
                  <span className="mono"> {item.id}</span> —— <b>只能用一次</b>。
                </div>
              )}
            </div>
          </article>
        ))}
      </section>
    </div>
  )
}
