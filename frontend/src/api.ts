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

/* ---------------- 审计中心 ---------------- */

/** 流水列表项。字段与后端 audit.SUMMARY_FIELDS 一一对应。
 *  列表**有意不含 SQL 文本与结果行** —— 细节只经 /api/replay 的白名单出去。 */
export interface AuditItem {
  trace_id: string
  ts: string
  kind: string
  thread_id: string | null
  org_id: number | null
  question: string | null
  rejected_by: string | null
  attempts: number | null
  rows_returned: number | null
  elapsed_ms: number
  cost_cny: number | null
  step_count: number | null
  multi_step: boolean | null
  ok: boolean
}

export interface AuditList {
  total: number
  page: number
  page_size: number
  items: AuditItem[]
}

export interface Tracing {
  backend: 'langfuse' | 'langsmith' | null
  enabled: boolean
  project: string | null
  host: string
  url: string
}

export interface AuditStats {
  days: number
  calls: number
  blocked: number
  block_rate: number
  cost_cny: number
  tok_in: number
  tok_out: number
  /** 没有调用时为 null。后端如实算占比，不写死 100% —— 这格数字要经得起对账。 */
  trace_complete: number | null
  daily: { date: string; calls: number; cost_cny: number }[]
  by_kind: Record<string, number>
  by_rule: Record<string, number>
  by_model: Record<string, { calls: number; cost_cny: number }>
  replay_api: boolean
  tracing: Tracing
}

export interface ReplayStep {
  step: string
  status: string
  ms: number
  tok_in?: number
  tok_out?: number
  note?: string
}

export interface ReplaySnapshot {
  attempt?: number
  next?: string[]
  rejected_by?: string
  error?: string
}

export interface Replay {
  trace_id: string
  ts: string
  kind: string
  thread_id: string | null
  org_id: number | null
  question: string | null
  tables_hit: string[] | null
  metrics_hit: string[] | null
  sql_raw: string | null
  sql_final: string | null
  rules_fired: string[] | null
  rejected_by: string | null
  attempts: number | null
  explain_rows: number | null
  step_count: number | null
  multi_step: boolean | null
  converged_early: boolean | null
  rows_returned: number | null
  elapsed_ms: number | null
  tok_in: number | null
  tok_out: number | null
  cost_cny: number | null
  steps: ReplayStep[] | null
  snapshots: ReplaySnapshot[]
}

/** 回放的三种结局都要能区分地告诉用户，不能一律报“出错了”：
 *  开关没开和记录不存在后端**同为 404**（区分本身就是信息泄露），
 *  被限流是 429 —— 那是“等一会儿再试”，不是“查不到”。 */
export type ReplayResult =
  | { status: 'ok'; data: Replay }
  | { status: 'not_found' }
  | { status: 'rate_limited' }

export async function fetchAudit(params: {
  page: number
  pageSize: number
  q: string
  kind: string
}): Promise<AuditList> {
  const query = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.pageSize),
    q: params.q,
    kind: params.kind,
  })
  const response = await fetch(`/api/audit?${query}`)
  if (!response.ok) throw new Error(`/api/audit ${response.status}`)
  return response.json()
}

export async function fetchAuditStats(days = 30): Promise<AuditStats> {
  const response = await fetch(`/api/audit/stats?days=${days}`)
  if (!response.ok) throw new Error(`/api/audit/stats ${response.status}`)
  return response.json()
}

export async function fetchReplay(traceId: string): Promise<ReplayResult> {
  const response = await fetch(`/api/replay?trace_id=${encodeURIComponent(traceId)}`)
  if (response.status === 429) return { status: 'rate_limited' }
  if (!response.ok) return { status: 'not_found' }
  return { status: 'ok', data: await response.json() }
}

/** 观测后端里这条 trace 的深链。未接入时返回 null —— 由调用方渲染禁用态。 */
export function tracingLink(tracing: Tracing, traceId: string): string | null {
  if (!tracing.enabled || !tracing.backend) return null
  const base = tracing.url || tracing.host
  if (tracing.backend === 'langfuse') {
    return `${base}/project/${tracing.project}/traces/${traceId}`
  }
  return base || 'https://smith.langchain.com'
}
