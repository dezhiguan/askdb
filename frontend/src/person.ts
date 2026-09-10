import type { Me } from './api'

/** 界面上显示的发起人：姓名优先（官德志），否则账号（guandezhi）。
 *
 *  与成员名册「姓名」列同一口径。后端把姓名补在 user_name 上；未登录
 *  不下发（PII）。若接口暂时没带上、但当前会话就是这个人，用 me.display_name
 *  顶上 —— 顶栏与权限页已经有这份姓名，审计/任务两页不该另写一列网关名。
 */
export function personName(
  userName: string | null | undefined,
  user: string | null | undefined,
  me?: Me | null,
): string {
  const named = (userName || '').trim()
  if (named) return named
  const account = (user || '').trim()
  if (!account) return ''
  if (
    me?.display_name
    && me.username
    && account.toLowerCase() === me.username.toLowerCase()
  ) {
    return me.display_name
  }
  return account
}
