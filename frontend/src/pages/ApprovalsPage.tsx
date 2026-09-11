import { PageHeader } from '../components/AppShell'
import { useCallback, useEffect, useState } from 'react'
import {
  decideApproval, decideReview, fetchApprovals, fetchOps, fetchReviews,
  resolveOps,
  type Approval, type ApprovalsResult, type OpsQueue, type ReviewQueue,
} from '../api'
import { roleLabel } from '../roles'

/* 人工处置队列（设计文档 Q-08 / P07 + 2026-09-11 扩展）。
 *
 * **一页三条队列，因为它们服务的是同一个动作：把一条卡住的任务往前推。**
 * 但三条队列本身不能合并 —— 判据、决策人、证据来源没有一处重合：
 *
 *   · 审批 —— 事前：这条该不该去跑。判据是预估扫描量，决策人是系统管理员，
 *     产出一张一次性的放行票。
 *   · 复核 —— 事后：跑出来的数字算不算数。判据是审计痕迹，决策人是系统管理员，
 *     产出一个采信/打回的结论。
 *   · 运维 —— 旁路：库通了没有。判据是 rejected_by=="EXEC"，决策人是运维，
 *     产出一句"可以重试了"或"这条没救"。
 *
 * 这一页服务两种人，且**看到的不是同一份东西**：
 *   · 有权处置的 —— 全部待办，带动作按钮
 *   · 其他人     —— 只有自己提的，用来知道进展
 * 收敛在服务端做（三个接口各自按能力位过滤），这里只负责把差别显示清楚。
 *
 * 有意**不做**的两件事：
 *   · 不提供"代发起人执行"按钮。放行只是放行 R-11 一道，重跑由发起人自己做 ——
 *     否则处置人就可能跑出他自己查不到的数据，或花掉别人没在等的配额。
 *   · 不显示结果集。审批人判断的是"该不该扫这么多行"，不是数据本身。
 */

const STATUS_CN: Record<Approval['status'], string> = {
  REQUESTED: '待审批',
  APPROVED: '已批准',
  REJECTED: '已驳回',
  CONSUMED: '已使用',
}

type Tab = 'approvals' | 'reviews' | 'ops'

const TAB_LABEL: Record<Tab, string> = {
  approvals: '审批', reviews: '复核', ops: '运维',
}

function fmt(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts || '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 一条队列记录的动作区：一个备注框 + 两个出口。
 *
 *  三条队列共用它，因为"判定 + 备注 + 两个出口"确实是同一个形状；各写一遍
 *  必然在其中一处漏掉"否定侧必填备注"—— 而那正是这一格存在的理由：
 *  被否掉的人拿到的唯一解释就是这句话。 */
function DecideRow({ id, affirm, deny, busy, onDecide }: {
  id: string
  affirm: string
  deny: string
  busy: boolean
  onDecide: (affirm: boolean, note: string) => void
}) {
  const [note, setNote] = useState('')
  return (
    <div className="form-row" style={{ marginTop: 10 }}>
      <input
        placeholder="处理意见（否定时必填）"
        value={note}
        maxLength={200}
        onChange={e => setNote(e.target.value)}
      />
      <button className="primary" disabled={busy}
              onClick={() => onDecide(true, note.trim())}>{affirm}</button>
      <button className="secondary" disabled={busy || !note.trim()}
              onClick={() => onDecide(false, note.trim())}
              title={note.trim() ? undefined : '请先写明理由'}>{deny}</button>
      <span className="mono" style={{ opacity: 0.5 }}>{id}</span>
    </div>
  )
}

export function ApprovalsPage({ notify }: { notify: (message: string) => void }) {
  const [tab, setTab] = useState<Tab>('approvals')
  const [data, setData] = useState<ApprovalsResult | null>(null)
  const [reviews, setReviews] = useState<ReviewQueue | null>(null)
  const [ops, setOps] = useState<OpsQueue | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  /* 三条队列一次全取。**不按 tab 懒加载**：标签上要显示各自的待办数，
     而"这个标签下有没有事"恰恰是人切过去之前就想知道的。三个请求都很轻
     （各自一页队列），省这两个往返换来的是三个恒显示 0 的标签。 */
  const load = useCallback(() => {
    Promise.allSettled([fetchApprovals(), fetchReviews(), fetchOps()])
      .then(([a, r, o]) => {
        if (a.status === 'fulfilled') setData(a.value)
        if (r.status === 'fulfilled') setReviews(r.value)
        if (o.status === 'fulfilled') setOps(o.value)
        // 三条里有一条挂了就说哪一条挂了，不把整页变成一行红字 ——
        // 审批存储不可用不该让人连复核队列都打不开。
        const bad = [
          a.status === 'rejected' ? '审批' : '',
          r.status === 'rejected' ? '复核' : '',
          o.status === 'rejected' ? '运维' : '',
        ].filter(Boolean)
        setError(bad.length ? `${bad.join('、')}队列读取失败` : '')
      })
  }, [])

  useEffect(() => { load() }, [load])

  const decide = async (item: Approval, approved: boolean, note: string) => {
    // 驳回必须写理由：申请人拿到的唯一信息就是这句话，空着等于让他去猜。
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

  const review = async (traceId: string, accepted: boolean, note: string) => {
    if (!accepted && !note) { notify('打回请写明理由 —— 发起人只看得到这句话'); return }
    setBusy(traceId)
    try {
      await decideReview(traceId, accepted, note)
      notify(accepted ? '已采信这条结果' : '已打回')
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const resolve = async (traceId: string, resolved: boolean, note: string) => {
    if (!resolved && !note) { notify('判为无法恢复请写明理由'); return }
    setBusy(traceId)
    try {
      await resolveOps(traceId, resolved ? 'RESOLVED' : 'WONTFIX', note)
      notify(resolved ? '已标记为故障已排除' : '已标记为无法恢复')
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  const items = data?.items ?? []
  const pendingApprovals = items.filter(i => i.status === 'REQUESTED')
  const reviewItems = reviews?.items ?? []
  const opsItems = ops?.items ?? []
  const counts: Record<Tab, number> = {
    approvals: pendingApprovals.length,
    reviews: reviews?.pending ?? 0,
    ops: ops?.pending ?? 0,
  }
  const canAct = tab === 'approvals' ? !!data?.can_approve
    : tab === 'reviews' ? !!reviews?.can_review
    : !!ops?.can_resolve

  const heading: Record<Tab, string> = {
    approvals: canAct
      ? '预估扫描量超阈值的查询在这里等待放行。放行是一次性的，由发起人自己重跑。'
      : '你提交的高成本查询申请。批准后到任务中心点「凭票重跑」。',
    reviews: canAct
      ? '跑成了、但结果带存疑痕迹的那些。采信或打回 —— 打回不撤销已经返回的数字，改变的是它此后的可信标记。'
      : '你发起的、结果待复核的查询。',
    ops: canAct
      ? '执行期故障（数据源连不上、执行中断）。排除后标记结论，发起人据此决定要不要重试。'
      : '你发起的、卡在执行期故障上的查询。',
  }

  return (
    <div className="page">
      <PageHeader
        title="人工处置"
        description={heading[tab]}
        action={<button className="secondary" onClick={load}>刷新</button>}
      />

      {error && <div className="audit-error">{error}</div>}

      <div className="filter-chips" style={{ marginBottom: 12 }}>
        {(['approvals', 'reviews', 'ops'] as Tab[]).map(t => (
          <button key={t} className={`chip ${tab === t ? 'active' : ''}`}
                  onClick={() => setTab(t)}>
            {TAB_LABEL[t]}{counts[t] ? ` · ${counts[t]}` : ''}
          </button>
        ))}
      </div>

      {!canAct && (
        <section className="card notice-card">
          <h3>你看到的是自己发起的那些</h3>
          <p>
            {tab === 'ops'
              ? <>故障处置权在<b>运维</b>与系统管理员。这一档判的是"库通了没有"，
                  是一句系统事实，与提问本身无关。</>
              : <>审批与复核都收敛在<b>系统管理员</b>。系统管理员现在也能查数
                  （2026-09-06 起），所以"自己批自己"不再是结构上不可能的事 ——
                  它由一条显式判定挡着：<b>发起人不得处置自己的请求</b>。
                  这也是这个实例上必须有两位管理员的原因。</>}
          </p>
        </section>
      )}

      {tab === 'approvals' && (
        <section className="card">
          <div className="card-head">
            <div>
              <strong>审批队列</strong>
              <p>共 {items.length} 条，其中 {pendingApprovals.length} 条待处理</p>
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
                  {' · '}<span title={item.user ? `申请人账号 ${item.user}` : undefined}>{item.user_name || item.user || '—'}</span>
                  {item.roles?.length ? `（${item.roles.map(roleLabel).join('+')}）` : ''}
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
                  <DecideRow id={item.id} affirm="放行一次" deny="驳回"
                             busy={busy === item.id}
                             onDecide={(ok, note) => decide(item, ok, note)} />
                )}

                {!data?.can_approve && item.status === 'APPROVED' && (
                  <div className="form-note">
                    已批准。到<b>任务中心</b>找到这条任务，点「凭票重跑」——
                    票绑在问题原文上，且<b>只能用一次</b>。
                  </div>
                )}
              </div>
            </article>
          ))}
        </section>
      )}

      {tab === 'reviews' && (
        <section className="card">
          <div className="card-head">
            <div>
              <strong>复核队列</strong>
              <p>共 {reviewItems.length} 条，其中 {reviews?.pending ?? 0} 条待复核</p>
            </div>
          </div>

          {!reviews && <div className="audit-empty">读取中…</div>}
          {reviews && !reviewItems.length && (
            <div className="audit-empty">
              没有待复核的结果。跑成了但带存疑痕迹的查询会自动进入这个队列。
            </div>
          )}

          {reviewItems.map(item => (
            <article className="rule" key={item.trace_id} style={{ alignItems: 'flex-start' }}>
              <i className="rule-no">
                {item.review_status === 'REQUESTED' ? '待复核'
                  : item.review_status === 'ACCEPTED' ? '已采信' : '已打回'}
              </i>
              <div style={{ minWidth: 0, flex: 1 }}>
                <strong className="audit-question" title={item.question ?? ''}>
                  {item.question || '（无问题原文）'}
                </strong>
                <small>
                  <span className="mono">{item.trace_id}</span>
                  {' · '}{item.owner || '匿名'}
                  {item.ts ? ` · ${fmt(item.ts)}` : ''}
                </small>
                {/* 为什么进这个队列 —— 复核人要判断的正是这几句。
                    让他自己猜，这个队列就没人会用。 */}
                {(item.review_why ?? []).length > 0 && (
                  <ul className="clarify-hints">
                    {(item.review_why ?? []).map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                )}
                {item.review_status !== 'REQUESTED' && (
                  <small>
                    {item.reviewer ? `${item.reviewer} 于 ${fmt(item.decided_ts || '')} 处理` : ''}
                    {item.note ? ` —— ${item.note}` : ''}
                  </small>
                )}
                {reviews?.can_review && item.review_status === 'REQUESTED' && (
                  <DecideRow id={item.trace_id} affirm="采信" deny="打回"
                             busy={busy === item.trace_id}
                             onDecide={(ok, note) => review(item.trace_id, ok, note)} />
                )}
              </div>
            </article>
          ))}
        </section>
      )}

      {tab === 'ops' && (
        <section className="card">
          <div className="card-head">
            <div>
              <strong>运维队列</strong>
              <p>共 {opsItems.length} 条，其中 {ops?.pending ?? 0} 条待处置</p>
            </div>
          </div>

          {!ops && <div className="audit-empty">读取中…</div>}
          {ops && !opsItems.length && (
            <div className="audit-empty">
              没有待处置的执行期故障。数据源连不上或执行中断的查询会自动进入这个队列。
            </div>
          )}

          {opsItems.map(item => (
            <article className="rule" key={item.trace_id} style={{ alignItems: 'flex-start' }}>
              <i className="rule-no">
                {!item.ops_status ? '待处置'
                  : item.ops_status === 'RESOLVED' ? '已恢复' : '无法恢复'}
              </i>
              <div style={{ minWidth: 0, flex: 1 }}>
                <strong className="audit-question" title={item.question ?? ''}>
                  {item.question || '（无问题原文）'}
                </strong>
                <small>
                  <span className="mono">{item.trace_id}</span>
                  {' · '}{item.owner || '匿名'}
                  {' · 数据源 '}{item.source_name || item.source || '—'}
                  {item.ts ? ` · ${fmt(item.ts)}` : ''}
                </small>
                {item.ops_status && (
                  <small>
                    {item.operator ? `${item.operator} 于 ${fmt(item.decided_ts || '')} 处置` : ''}
                    {item.note ? ` —— ${item.note}` : ''}
                  </small>
                )}
                {ops?.can_resolve && !item.ops_status && (
                  <DecideRow id={item.trace_id} affirm="已恢复" deny="无法恢复"
                             busy={busy === item.trace_id}
                             onDecide={(ok, note) => resolve(item.trace_id, ok, note)} />
                )}
              </div>
            </article>
          ))}
        </section>
      )}
    </div>
  )
}
