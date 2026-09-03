import { useEffect, useState } from 'react'
import {
  addMember, fetchMembers, fetchRoles, removeMember,
  type RoleInfo, type RoleMember, type RolesResponse,
} from '../api'

function fmtDate(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 原型（trusted-data-agent-prototype.html 第 2274–2277 行）左栏角色副标题。
 *  后端 Role 只有 scope 一个短字段，副标题是设计稿文案，按角色码对齐；
 *  设计稿未覆盖的角色（如系统管理员）退回真实 scope。 */
const ROLE_SUBTITLE: Record<string, string> = {
  PRODUCT: '生产只读、默认脱敏',
  DEV: '开发及测试环境',
  QA: '测试环境与模拟数据',
  DATA_OWNER: '策略配置与审批',
}

/** 原型 permissionData 的标题（第 3921–3924 行）。 */
const ROLE_TITLE: Record<string, string> = {
  PRODUCT: '产品角色 · Product',
  DEV: '开发角色 · Developer',
  QA: '测试角色 · QA',
  DATA_OWNER: '数据负责人 · Data Owner',
}

/** 原型 permissionData 的后三个维度：数据期限 / 敏感字段 / 导出权限。
 *  这三项后端没有对应字段，按设计稿占位（第一维「环境范围」用真实 role.scope）。
 *  设计稿未覆盖的角色留 —— ，不编数值。 */
const ROLE_LIMITS: Record<string, [string, string, string]> = {
  PRODUCT: ['90 DAYS', 'MASKED', 'AGG ONLY'],
  DEV: ['ALL TEST', 'PARTIAL', 'ALLOWED'],
  QA: ['180 DAYS', 'MASKED', '≤ 10K'],
  DATA_OWNER: ['365 DAYS', 'ON DEMAND', 'APPROVAL'],
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

  /** 原型上这个按钮没有行为。真实实例里企业目录同步还没接入，
   *  所以它只做当下唯一诚实的动作：重新读取角色与成员。 */
  const syncOrg = () => {
    setMembers(null)
    setReload(n => n + 1)
    notify('企业目录同步尚未接入，已重新读取角色与成员')
  }

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
      {/* 原型的 page-head 带 eyebrow，AppShell 的 PageHeader 没有这个槽位，
          且 AppShell 不在本次改动范围内，因此这里直接照原型写结构。 */}
      <div className="page-head">
        <div>
          <div className="eyebrow">Phase 2 · Identity &amp; Access</div>
          <h1>身份与权限中心</h1>
          <p>企业 SSO 提供身份，RBAC 定义角色，ABAC 根据环境和数据属性动态收敛权限。</p>
        </div>
        {/* 企业目录同步尚未接入，这个按钮做它当下唯一能诚实做的事：重读角色与成员 */}
        <button className="primary" onClick={syncOrg}>同步企业组织</button>
      </div>

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
        <section className="card">
          <div className="card-head">
            <div><strong>角色</strong><p>共 {data?.roles.length ?? 0} 个内置角色</p></div>
          </div>
          <div className="role-list">
            {data?.roles.map(item => (
              <button
                className={`role-item${active === item.code ? ' active' : ''}`}
                key={item.code}
                onClick={() => { setActive(item.code); setMembers(null) }}
              >
                <span><strong>{item.name}</strong><small>{ROLE_SUBTITLE[item.code] ?? item.scope}</small></span>
                <span className="role-count">{item.members}</span>
              </button>
            ))}
            {!data && <div className="audit-empty">读取中…</div>}
          </div>
        </section>

        <div className="policy-stack">
        <section className="card">
          {role
            ? <RoleDetail notify={notify} role={role} />
            : <p className="drawer-note policy-note">读取中…</p>}
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

function RoleDetail({ role, notify }: { role: RoleInfo; notify: (message: string) => void }) {
  const limits = ROLE_LIMITS[role.code]
  const placeholder = '设计稿占位：后端尚无该维度'
  return (
    <>
      <div className="permission-head">
        <div className="eyebrow">{role.system ? 'SYSTEM ROLE' : 'ROLE POLICY'}</div>
        <h3>{ROLE_TITLE[role.code] ?? `${role.name} · ${role.code}`}</h3>
        <p>{role.desc}</p>
      </div>
      {/* 四个维度照设计稿。环境范围是真值（来自角色定义）；
          后三项后端还没有这三个维度，按设计稿取值占位并在 title 里标明。 */}
      <div className="permission-grid">
        <div className="permission-cell"><span>环境范围</span><strong>{role.scope}</strong></div>
        <div className="permission-cell">
          <span>数据期限</span><strong title={placeholder}>{limits?.[0] ?? '—'}</strong>
        </div>
        <div className="permission-cell">
          <span>敏感字段</span><strong title={placeholder}>{limits?.[1] ?? '—'}</strong>
        </div>
        <div className="permission-cell">
          <span>导出权限</span><strong title={placeholder}>{limits?.[2] ?? '—'}</strong>
        </div>
      </div>
      {role.system && (
        <p className="drawer-note policy-note">
          职责分离：系统角色只管成员，<b>不因此获得任何数据访问权</b>。
          管理员本人要查数，须另行加入某个数据角色，且这一动作同样留痕。
        </p>
      )}
      <RolePolicyRules notify={notify} />
    </>
  )
}

/** 角色策略开关，照原型第 2288–2291 行的四条规则。
 *
 *  只有 P01 是真的：只读事务 + 护栏拦截写操作，askdb 的每一条连接都如此，
 *  页面上把它拨到 OFF 不会放开写操作 —— 关掉时的提示会如实说明这一点。
 *  另外三条后端尚无存储也无执行，开关状态只存在于本页。
 */
const POLICY_RULES: { code: string; title: string; desc: string; live: boolean }[] = [
  {
    code: 'P01', title: '生产环境强制只读',
    desc: '拦截 INSERT、UPDATE、DELETE、DDL 和存储过程。',
    live: true,
  },
  {
    code: 'P03', title: '个人信息默认脱敏',
    desc: '手机号、姓名、证件号、地址必须经过列级脱敏。',
    live: false,
  },
  {
    code: 'P07', title: '高成本查询二次确认',
    desc: '预计扫描超过 100,000 行时进入数据负责人审批。',
    live: false,
  },
  {
    code: 'P11', title: '查询结果禁止用于模型训练',
    desc: '结果仅在任务生命周期内处理，禁止进入训练数据。',
    live: false,
  },
]

function RolePolicyRules({ notify }: { notify: (message: string) => void }) {
  // 原型四条默认全开，点击即翻转
  const [on, setOn] = useState<Record<string, boolean>>(
    () => Object.fromEntries(POLICY_RULES.map(rule => [rule.code, true] as const)),
  )

  const flip = (rule: { code: string; live: boolean }) => {
    const next = !on[rule.code]
    setOn(state => ({ ...state, [rule.code]: next }))
    if (next) { notify('策略已启用'); return }
    notify(rule.live
      ? '生产只读由执行层强制，页面开关不会放开写操作'
      : '策略已停用（该策略尚未接入后端执行）')
  }

  return (
    <div className="policy-rules">
      {POLICY_RULES.map(rule => (
        <div className="rule" key={rule.code}>
          <i className="rule-no">{rule.code}</i>
          <div>
            <strong>{rule.title}</strong>
            <small>{rule.desc}</small>
          </div>
          <button
            aria-label={rule.title}
            aria-pressed={on[rule.code]}
            className={`toggle ${on[rule.code] ? 'on' : ''}`}
            onClick={() => flip(rule)}
          ><i /></button>
        </div>
      ))}
    </div>
  )
}
