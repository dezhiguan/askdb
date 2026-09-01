/** 后端接口的最小接入层。
 *
 * 换壳阶段只接 /api/health —— 它决定外壳上那几个身份标识（数据源、模型、
 * 租户、当前配置）显示什么。这几项是**壳的一部分**：壳如果对连着哪个库
 * 都说不准，后面接上来的数据也无从判断出处。
 *
 * 其余页面仍在样例数据上，接口逐条接回来时在这里加函数，不要在组件里直接 fetch。
 */

export interface Health {
  ok: boolean
  config: string
  datasource: { ok: boolean; type: string; detail: string; hint: string }
  llm: { ok: boolean; model: string; env: string; disabled: boolean }
  tenant: {
    enabled: boolean
    column: string
    org_id: number
    mode: string
    on_unresolved: string
    tables: string[]
  }
  guard: { max_rows: number; max_retry: number; timeout_ms: number; max_scan_rows: number; daily_quota: number }
}

export async function fetchHealth(): Promise<Health> {
  const response = await fetch('/api/health')
  if (!response.ok) throw new Error(`/api/health ${response.status}`)
  return response.json()
}
