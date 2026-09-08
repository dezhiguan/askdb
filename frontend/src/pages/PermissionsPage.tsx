import { PageHeader } from '../components/AppShell'
import { useEffect, useState } from 'react'
import {
  addMember, fetchMembers, fetchRoles, removeMember, Forbidden,
  type MembersPage, type RoleInfo, type RoleMember, type RolesResponse,
 type Me,
} from '../api'
import { writeGuard, type WriteGuard } from '../writeGuard'

function fmtDate(ts: string): string {
  /* 空值必须先挡掉。**new Date(null) 不是 NaN，是纪元 0** —— 只判 NaN 的话，
     没有 ts 的老审计记录会被格式化成「1970-01-01 08:00」，即凭空编出一个
     看起来合理的时间。宁可显示占位，也不要显示一个假的。 */
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 原型（trusted-data-agent-prototype.html 第 2274–2277 行）左栏角色副标题。
 *  后端 Role 只有 scope 一个短字段，副标题是设计稿文案，按角色码对齐；
 *  设计稿未覆盖的角色（如系统管理员）退回真实 scope。 */
const ROLE_SUBTITLE: Record<string, string> = {
  PRODUCT: '与其他角色同一可见面',
  DEV: '与其他角色同一可见面',
  QA: '与其他角色同一可见面',
  DESIGN: '与其他角色同一可见面',
  DATA_OWNER: '与其他角色同一可见面',
  OPERATIONS: '与其他角色同一可见面',
  SALES: '与其他角色同一可见面',
  MARKETING: '与其他角色同一可见面',
  SUPPORT: '与其他角色同一可见面',
  FINANCE: '与其他角色同一可见面',
  HR: '与其他角色同一可见面',
  LEGAL: '与其他角色同一可见面',
  MANAGEMENT: '与其他角色同一可见面',
  OTHER: '与其他角色同一可见面',
  SYS_ADMIN: '同一可见面 + 审批',
}

/** 原型 permissionData 的标题（第 3921–3924 行）。 */
const ROLE_TITLE: Record<string, string> = {
  PRODUCT: '产品角色 · Product',
  DEV: '开发角色 · Developer',
  QA: '测试角色 · QA',
  DESIGN: '设计角色 · Design',
  DATA_OWNER: '数据角色 · Data',
  OPERATIONS: '运营角色 · Operations',
  SALES: '销售角色 · Sales',
  MARKETING: '市场角色 · Marketing',
  SUPPORT: '客服角色 · Support',
  FINANCE: '财务角色 · Finance',
  HR: '人力角色 · HR',
  LEGAL: '法务合规角色 · Legal & Compliance',
  MANAGEMENT: '管理角色 · Management',
  OTHER: '其他 · Unassigned',
}

/** 四个维度全部取后端真值。
 *
 *  此前这三格是照设计稿写死的（'90 DAYS' / 'MASKED' / 'AGG ONLY'），
 *  而后端当时根本没有对应字段 —— 页面显示了一个从未生效过的值，
 *  比"看不出有没有生效"更糟。现在环境范围、数据期限、敏感字段都由
 *  identity.Policy 算出来，改角色策略这里立刻跟着变。
 *
 *  「导出权限」没有列进来：askdb 没有后端导出接口，审计与追踪的导出都是
 *  浏览器端把已拉取的数据拼字符串下载。在那种架构下这一维度**没有任何
 *  可施加的位置**，写一个值上去就是在承诺一件做不到的事。 */
function ageLabel(role: RoleInfo): string {
  return role.max_age_days == null ? '不限' : `${role.max_age_days} DAYS`
}

export function PermissionsPage({ notify, me }: {
  notify: (message: string) => void
  me: Me | null
}) {
  const guard = writeGuard(me, '同步企业组织')
  // 未登录（匿名，无网关用户名）时给成员数据行加模糊蒙版：隐约可见、看不清。
  // 注意这是视觉层：明文仍在接口响应里，要真脱敏须后端对匿名收窄字段。
  const masked = !me?.username
  const [data, setData] = useState<RolesResponse | null>(null)
  const [active, setActive] = useState<string>('PRODUCT')
  const [members, setMembers] = useState<MembersPage | null>(null)
  // 名册取不到有两种：没权限看（按设计）和真出错。合成一个 error 会把
  // 权限边界渲染成红色的「读取失败」，看的人去查一个不存在的故障
  const [membersDenied, setMembersDenied] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  // 管理员令牌只在内存里。它是部署方持有的共享口令，
  // 落进 localStorage 等于把它长期留在浏览器里
  const [token, setToken] = useState('')
  const [form, setForm] = useState({ username: '', display_name: '', note: '' })
  const [busy, setBusy] = useState(false)

  /* 成员表切页。页码与每页条数传给 /api/identity/members，库里 LIMIT/OFFSET
     取这一页 —— 一个角色几百人时，出网的与读出来的都只有这十行。
     分页条与任务中心、审计中心同一套结构与类名，三页的操作手感必须一致。 */
  const [memberPage, setMemberPage] = useState(1)
  const [memberPageSize, setMemberPageSize] = useState(10)

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
    setMembersDenied(false)
    fetchMembers(active, memberPage, memberPageSize)
      .then(value => { if (alive) { setMembers(value); setMembersDenied(false) } })
      .catch(e => {
        if (!alive) return
        // 完整名册只对数据负责人与系统管理员开放，其余角色只看得到自己那一档。
        // 这是既定边界，不该报错 —— 在表里说清楚谁能看就够了
        if (e instanceof Forbidden) { setMembersDenied(true); return }
        setError(String((e as Error).message || e))
      })
    return () => { alive = false }
  }, [active, data?.enabled, reload, memberPage, memberPageSize])

  const role = data?.roles.find(r => r.code === active)

  // total 是整个角色的人数，由服务端给 —— 这一页只有十条，拿它算页码会永远只有一页
  const memberTotal = members?.total ?? 0
  const memberPages = Math.max(Math.ceil(memberTotal / memberPageSize), 1)
  const memberCurrent = Math.min(members?.page ?? memberPage, memberPages)
  const visibleMembers = members?.items

  /* 换角色、换每页条数都在各自的入口处一并把页码设回 1（停在第 3 页而新角色
     只有 2 个人，会看到一片空白）。这里只管重新读取那一路：同步组织之后
     名单可能变短，页码留在原处会指到空页上。 */
  useEffect(() => { setMemberPage(1) }, [reload])

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
      {/* 「同步企业组织」：企业目录同步尚未接入，这个按钮做它当下唯一能诚实做的事
          —— 重读角色与成员 */}
      <PageHeader
        title="身份与权限中心"
        description="企业 SSO 提供身份，RBAC 定义角色，ABAC 根据环境和数据属性动态收敛权限。"
        action={<button className="primary" {...guard.props} onClick={syncOrg}>同步企业组织</button>}
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
        <section className="card">
          <div className="card-head">
            <div><strong>角色</strong><p>共 {data?.roles.length ?? 0} 个内置角色</p></div>
          </div>
          <div className="role-list">
            {data?.roles.map(item => (
              <button
                className={`role-item${active === item.code ? ' active' : ''}`}
                key={item.code}
                onClick={() => { setActive(item.code); setMembers(null); setMemberPage(1) }}
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
            ? <RoleDetail notify={notify} role={role} guard={guard} />
            : <p className="drawer-note policy-note">读取中…</p>}
        </section>

        {/* 原型这一页只有角色与策略两块，没有成员区。成员是真能用的功能
            （读写都走接口），所以不删 —— 单独成卡放在策略卡下面，
            策略卡本身与设计稿保持一致。 */}
        <section className="card member-card">
          <h4 className="member-head">
            成员
            <span className="section-note">
              {!data?.enabled ? '未启用'
                : membersDenied ? '不可见'
                : members ? `${members.total} 人` : '读取中'}
            </span>
          </h4>

          {data?.enabled && (
            <div className="table-scroll">
              <table className="audit-table">
                <thead>
                  <tr><th>网关用户名</th><th>姓名</th><th>备注</th><th>关联状态</th><th>加入</th><th /></tr>
                </thead>
                <tbody>
                  {/* 内置条目 id 恒为 0，拿 id 当 key 会撞车 —— 角色内用户名唯一 */}
                  {/* 脱敏后 username 会变成相同的圆点，不能再当 key —— 用页内索引 */}
                  {visibleMembers?.map((member, i) => (
                    <tr key={`${member.role_code}:${i}`}
                        className={masked ? 'member-masked' : undefined}>
                      <td className="mono">{member.username}</td>
                      <td>{member.display_name || <span className="dim">—</span>}</td>
                      <td className="audit-question" title={masked ? undefined : member.note}>
                        {member.note || <span className="dim">—</span>}
                      </td>
                      <td>
                        {member.builtin
                          ? <span className="status">配置内置</span>
                          : member.bound
                            ? <span className="status">已绑定 #{member.auth_user_id}</span>
                            : <span className="status wait">未绑定网关用户</span>}
                      </td>
                      <td className="mono dim">
                        {member.builtin ? '—' : fmtDate(member.created_at)}
                      </td>
                      <td>
                        {member.builtin
                          ? <span className="link-disabled" title="由 config 里的 auth.accounts 管理，改配置文件">移除</span>
                          : data.writable
                            ? <button className="link-button" disabled={busy} onClick={() => drop(member)}>移除</button>
                            : <span className="link-disabled" title="未配置 ASKDB_ADMIN_TOKEN">移除</span>}
                      </td>
                    </tr>
                  ))}
                  {members?.items.length === 0 && (
                    <tr><td colSpan={6} className="audit-empty">这个角色还没有成员</td></tr>
                  )}
                  {membersDenied && (
                    <tr><td colSpan={6} className="audit-empty">
                      完整成员名册只对数据负责人与系统管理员开放。其余角色看得到的是
                      自己所属的那一档 —— 登录后按当前角色重新判定。
                    </td></tr>
                  )}
                  {!members && !membersDenied && (
                    <tr><td colSpan={6} className="audit-empty">读取中…</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          {data?.enabled && memberTotal > 0 && (
            <div className="audit-pager">
              <span>共 {memberTotal} 人 · 第 {memberCurrent} / {memberPages} 页</span>
              <span>
                <select value={memberPageSize}
                        onChange={event => { setMemberPageSize(Number(event.target.value)); setMemberPage(1) }}>
                  {[10, 20, 50].map(size => <option key={size} value={size}>每页 {size} 条</option>)}
                </select>
                <button className="ghost" disabled={memberCurrent <= 1}
                        onClick={() => setMemberPage(p => p - 1)}>‹ 上一页</button>
                <button className="ghost" disabled={memberCurrent >= memberPages}
                        onClick={() => setMemberPage(p => p + 1)}>下一页 ›</button>
              </span>
            </div>
          )}

          {data?.enabled && data.writable && (
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
          )}
        </section>
        </div>
      </div>
    </div>
  )
}

function RoleDetail({ role, notify, guard }: {
  role: RoleInfo
  notify: (message: string) => void
  guard: WriteGuard
}) {
  return (
    <>
      <div className="permission-head">
        <div className="eyebrow">{role.system ? 'SYSTEM ROLE' : 'ROLE POLICY'}</div>
        <h3>{ROLE_TITLE[role.code] ?? `${role.name} · ${role.code}`}</h3>
        <p>{role.desc}</p>
      </div>
      {/* 剩下的几格**全部是真值**，由 identity.Policy 算出来。
          此前它们是照设计稿写死的，而后端根本没有对应字段 —— 那比
          "看不出有没有生效"更糟：它显示了一个从未生效过的值。
          撤过两格，都是同一个理由（不再据此拦截就不能继续挂在页面上）：
            · 环境范围 —— 2026-09-06 角色与数据源解绑
            · 敏感字段 —— 2026-09-06 脱敏改为对所有人无条件生效，
              这一格从此对每个角色都是同一个值，留着只会让人以为它是可调的 */}
      <div className="permission-grid three">
        <div className="permission-cell">
          <span>数据期限</span>
          <strong title="护栏 R-19 按此注入时间窗口谓词">{ageLabel(role)}</strong>
        </div>
        <div className="permission-cell">
          <span>敏感字段</span>
          <strong title="个人信息列一律脱敏，没有角色能关掉">一律脱敏</strong>
        </div>
        <div className="permission-cell">
          <span>成员</span><strong>{role.members} 人</strong>
        </div>
      </div>
      <p className="drawer-note policy-note">
        {role.system
          ? <>系统管理员是<b>唯一多出权限的角色</b>：多出来的只有审批。
              可见面与其他角色完全相同，且不能审批自己发起的查询。</>
          : <>可见面与其他角色<b>完全相同</b>。角色之间唯一的差别是审批，
              只有系统管理员有；未登录可以浏览与查询，但不能写。</>}
      </p>
      <RolePolicyRules notify={notify} guard={guard} />
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
    // 2026-09-06 起这条是真的：脱敏在执行层无条件生效，没有角色能关掉。
    live: true,
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

function RolePolicyRules({ notify, guard }: {
  notify: (message: string) => void
  guard: WriteGuard
}) {
  // 原型四条默认全开，点击即翻转
  const [on, setOn] = useState<Record<string, boolean>>(
    () => Object.fromEntries(POLICY_RULES.map(rule => [rule.code, true] as const)),
  )

  const flip = (rule: { code: string; title: string; live: boolean }) => {
    const next = !on[rule.code]
    setOn(state => ({ ...state, [rule.code]: next }))
    if (next) { notify('策略已启用'); return }
    notify(rule.live
      ? `${rule.title}由执行层强制，页面开关关不掉它`
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
            {...guard.props}
            onClick={() => flip(rule)}
          ><i /></button>
        </div>
      ))}
    </div>
  )
}
