import { useEffect, useState } from 'react'
import {
  addMember, fetchMembers, fetchRoles, removeMember,
  type RoleInfo, type RoleMember, type RolesResponse,
} from '../api'
import { PageHeader } from '../components/AppShell'

function fmtDate(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export function PermissionsPage({ notify }: { notify: (message: string) => void }) {
  const [data, setData] = useState<RolesResponse | null>(null)
  const [active, setActive] = useState<string>('PRODUCT')
  const [members, setMembers] = useState<RoleMember[] | null>(null)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  // 管理员令牌只在内存里。它是部署方持有的共享口令，
  // 落进 localStorage 等于把它长期留在浏览器里
  const [token, setToken] = useState('')
  const [form, setForm] = useState({ username: '', display_name: '', note: '' })
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    fetchRoles()
      .then(value => { if (alive) { setData(value); setError('') } })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [reload])

  useEffect(() => {
    if (!data?.enabled) return
    let alive = true
    fetchMembers(active)
      .then(value => { if (alive) setMembers(value) })
      .catch(e => { if (alive) setError(String(e.message || e)) })
    return () => { alive = false }
  }, [active, data?.enabled, reload])

  const role = data?.roles.find(r => r.code === active)

  const submit = async () => {
    if (!form.username.trim()) { notify('请填写网关用户名'); return }
    setBusy(true)
    try {
      await addMember(token, { role_code: active, ...form })
      setForm({ username: '', display_name: '', note: '' })
      setReload(n => n + 1)
      notify(`已把 ${form.username} 加入「${role?.name}」`)
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  const drop = async (member: RoleMember) => {
    setBusy(true)
    try {
      await removeMember(token, member.id)
      setReload(n => n + 1)
      notify(`已把 ${member.username} 移出「${role?.name}」`)
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="身份与权限"
        description="角色定义与成员名单。认证交给企业网关，askdb 只负责「谁属于哪个角色」以及角色的数据边界含义。"
      />

      {error && <div className="audit-error">读取失败：{error}</div>}

      {data && !data.enabled && (
        <section className="card notice-card">
          <h3>本实例未启用身份与权限</h3>
          <p>
            未配置 <span className="mono">identity.dsn</span>。角色定义写在源码里因此照常可见，
            但没有成员名单可读写。对外开放实例应当保持这个状态。
          </p>
        </section>
      )}

      <div className="policy-layout">
        <section className="card role-list">
          <div className="card-head"><strong>角色</strong></div>
          {data?.roles.map(item => (
            <button
              className={active === item.code ? 'active' : ''}
              key={item.code}
              onClick={() => { setActive(item.code); setMembers(null) }}
            >
              <span><strong>{item.name}</strong><small>{item.scope}</small></span>
              <b>{item.members}</b>
            </button>
          ))}
          {!data && <div className="audit-empty">读取中…</div>}
        </section>

        <div className="policy-stack">
        <section className="card permission-detail">
          {role ? <RoleDetail role={role} /> : <p className="drawer-note">读取中…</p>}
        </section>

        {/* 原型这一页只有角色与策略两块，没有成员区。成员是真能用的功能
            （读写都走接口），所以不删 —— 单独成卡放在策略卡下面，
            策略卡本身与设计稿保持一致。 */}
        <section className="card member-card">
          <h4 className="member-head">
            成员
            <span className="section-note">
              {data?.enabled ? `${members?.length ?? 0} 人` : '未启用'}
            </span>
          </h4>

          {data?.enabled && (
            <div className="table-scroll">
              <table className="audit-table">
                <thead>
                  <tr><th>网关用户名</th><th>姓名</th><th>备注</th><th>关联状态</th><th>加入</th><th /></tr>
                </thead>
                <tbody>
                  {members?.map(member => (
                    <tr key={member.id}>
                      <td className="mono">{member.username}</td>
                      <td>{member.display_name || <span className="dim">—</span>}</td>
                      <td className="audit-question" title={member.note}>
                        {member.note || <span className="dim">—</span>}
                      </td>
                      <td>
                        {member.bound
                          ? <span className="status">已绑定 #{member.auth_user_id}</span>
                          : <span className="status wait">未绑定网关用户</span>}
                      </td>
                      <td className="mono dim">{fmtDate(member.created_at)}</td>
                      <td>
                        {data.writable
                          ? <button className="link-button" disabled={busy} onClick={() => drop(member)}>移除</button>
                          : <span className="link-disabled" title="未配置 ASKDB_ADMIN_TOKEN">移除</span>}
                      </td>
                    </tr>
                  ))}
                  {members?.length === 0 && (
                    <tr><td colSpan={6} className="audit-empty">这个角色还没有成员</td></tr>
                  )}
                  {!members && <tr><td colSpan={6} className="audit-empty">读取中…</td></tr>}
                </tbody>
              </table>
            </div>
          )}

          {data?.enabled && (
            data.writable
              ? (
                <div className="member-form">
                  <div className="form-note">
                    成员写接口要按网关身份授权，而 auth-gateway 对接尚未落地，暂由部署方
                    持有的管理员令牌兜底。令牌只留在内存里，刷新页面即失效。
                  </div>
                  <div className="form-row">
                    <input type="password" placeholder="管理员令牌" value={token}
                           onChange={e => setToken(e.target.value)} />
                    <input placeholder="网关用户名（必填）" value={form.username}
                           onChange={e => setForm({ ...form, username: e.target.value })} />
                    <input placeholder="姓名" value={form.display_name}
                           onChange={e => setForm({ ...form, display_name: e.target.value })} />
                    <input placeholder="备注" value={form.note}
                           onChange={e => setForm({ ...form, note: e.target.value })} />
                    <button className="primary" disabled={busy || !token} onClick={submit}>
                      加入「{role?.name}」
                    </button>
                  </div>
                </div>
              )
              : (
                <div className="form-note">
                  未配置 <span className="mono">ASKDB_ADMIN_TOKEN</span>，成员写入整体关闭 —— 本页只读。
                  这是有意的默认值：登录未接入前，写接口没有任何请求方身份可依据，
                  开着就等于任何能打开页面的人都能给自己加角色。
                </div>
              )
          )}
        </section>
        </div>
      </div>
    </div>
  )
}

function RoleDetail({ role }: { role: RoleInfo }) {
  return (
    <>
      <div className="eyebrow">{role.system ? 'SYSTEM ROLE' : 'ROLE POLICY'}</div>
      <h2>{role.name} · {role.code}</h2>
      <p>{role.desc}</p>
      {/* 四个维度照设计稿。环境范围是真值（来自角色定义）；
          数据期限 / 敏感字段 / 导出权限后端还没有这三个维度，
          按「先对齐页面」占位成 —— 不编 365 DAYS 这类看着像真的值。 */}
      <div className="permission-grid">
        <div><span>环境范围</span><strong>{role.scope}</strong></div>
        <div><span>数据期限</span><strong title="后端尚无该维度">—</strong></div>
        <div><span>敏感字段</span><strong title="后端尚无该维度">—</strong></div>
        <div><span>导出权限</span><strong title="后端尚无该维度">—</strong></div>
      </div>
      {role.system && (
        <p className="drawer-note">
          职责分离：系统角色只管成员，<b>不因此获得任何数据访问权</b>。
          管理员本人要查数，须另行加入某个数据角色，且这一动作同样留痕。
        </p>
      )}
      <RolePolicyRules />
    </>
  )
}

/** 角色策略开关。
 *
 *  只有 P01 是真的：只读事务 + 护栏拦截写操作，askdb 的每一条连接都如此。
 *  它**不可关闭** —— 这不是懒得做开关，是这一条一旦可关，整个产品的前提
 *  就没了；给它一个能拨到 OFF 的开关，等于宣称存在一种"可写模式"。
 *
 *  另外三条后端尚无存储也无执行，先按设计稿占位并置灰。宁可页面上少一个
 *  能拨的开关，也不要一个拨了什么都不会发生的开关 —— 后者会让人以为
 *  脱敏已经生效。
 */
const POLICY_RULES: { code: string; title: string; desc: string; on: boolean; live: boolean }[] = [
  {
    code: 'P01', title: '生产环境强制只读',
    desc: '拦截 INSERT、UPDATE、DELETE、DDL 和存储过程。',
    on: true, live: true,
  },
  {
    code: 'P03', title: '个人信息默认脱敏',
    desc: '手机号、姓名、证件号、地址必须经过列级脱敏。',
    on: true, live: false,
  },
  {
    code: 'P07', title: '高成本查询二次确认',
    desc: '预计扫描超过 100,000 行时进入数据负责人审批。',
    on: true, live: false,
  },
  {
    code: 'P11', title: '查询结果禁止用于模型训练',
    desc: '结果仅在任务生命周期内处理，禁止进入训练数据。',
    on: true, live: false,
  },
]

function RolePolicyRules() {
  return (
    <div className="policy-rules">
      {POLICY_RULES.map(rule => (
        <div className="rule" key={rule.code}>
          <i className="rule-no">{rule.code}</i>
          <div>
            <strong>{rule.title}</strong>
            <small>{rule.live ? rule.desc : `${rule.desc}（策略尚未接入，开关不可用）`}</small>
          </div>
          <button
            className={`toggle ${rule.on ? 'on' : ''}`}
            disabled
            aria-pressed={rule.on}
            title={rule.live
              ? '只读是 askdb 的前提，不提供关闭 —— 每条连接都以只读事务打开，写操作在引擎层即被拒绝'
              : '该策略后端尚未实现，页面先按设计稿占位'}
          ><i /></button>
        </div>
      ))}
    </div>
  )
}
