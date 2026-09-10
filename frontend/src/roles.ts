/** 角色码 → 中文名。**与 askdb/identity.py 的 ROLES 是同一份口径**，
 *  tests/test_frontend.py 扫那边的定义钉住这里不缺项。
 *
 *  为什么不去调 /api/identity/roles：角色码出现在顶栏、审计流水每一行、
 *  追踪列表每一行 —— 这些地方都不该为了一个显示名去等一个接口，更不该在
 *  接口没回来时先显示一遍英文码再跳成中文。角色是固定的、不开放自定义
 *  （identity.py 开头写明），这份表因此可以是静态的。
 *
 *  未知码原样显示，不吞掉也不改写 —— 后端新增了角色而这里漏了的时候，
 *  页面上出现一个英文码正是要看见的那个信号（测试也会先红）。
 */
export const ROLE_NAMES: Record<string, string> = {
  PRODUCT: '产品',
  DEV: '开发',
  QA: '测试',
  DESIGN: '设计',
  DATA_OWNER: '数据',
  OPERATIONS: '运营',
  SALES: '销售',
  MARKETING: '市场',
  SUPPORT: '客服',
  FINANCE: '财务',
  HR: '人力',
  LEGAL: '法务合规',
  MANAGEMENT: '管理',
  OTHER: '其他',
  SYS_ADMIN: '系统管理员',
  ANONYMOUS: '匿名',
}

/** 一个角色码的显示名。 */
export function roleLabel(code: string): string {
  const key = (code || '').trim()
  return ROLE_NAMES[key] ?? ROLE_NAMES[key.toUpperCase()] ?? key
}

/** 一串角色的显示名。审计记录里的 role 是**多个角色用 + 拼出来的一个串**
 *  （身份在服务端就是这么落库的），所以这里按 + 拆开逐个翻译再拼回去。
 *
 *  分隔符仍用 `+`：中文名之间用顿号会和「法务合规」这种本身带词的名字糊在
 *  一起，而 + 明确表示"这几个角色他都有"。
 *
 *  空串在审计里是有含义的（那次调用本来就没记角色），交给调用处去说，
 *  这里如实返回空串，不替它编一个「未记录」。 */
export function rolesLabel(role: string | null | undefined): string {
  if (!role) return ''
  return role.split('+').map(r => roleLabel(r)).join('+')
}
