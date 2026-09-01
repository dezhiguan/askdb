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
  datasource: {
    ok: boolean
    type: string
    detail: string
    hint: string
    /** 口令所在的环境变量名（不含值）。空串 = 这个库不需要口令 */
    credential: string
  }
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
  quota: {
    limit: number
    used: number
    /** 未启用配额时为 null */
    remaining: number | null
    backend: string
    multi_replica_safe: boolean
  }
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
  /** 最近秩分位：一定是真发生过的某一次耗时，不是插值。窗口内无调用时为 null。 */
  elapsed_p50_ms: number | null
  elapsed_p95_ms: number | null
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

/* ---------------- 数据源 ---------------- */

export interface SchemaColumn {
  name: string
  type: string
  desc: string
  enum: string[]
  tenant: boolean
}

export interface SchemaTable {
  name: string
  desc: string
  aliases: string[]
  tenant_column: string | null
  columns: SchemaColumn[]
}

export interface SchemaMetric {
  name: string
  aliases: string[]
  scope: string[]
  definition: string
  note: string
}

export interface Schema {
  tables: SchemaTable[]
  metrics: SchemaMetric[]
}

export interface SelfCheck {
  ok: boolean
  checks: { name: string; ok: boolean; detail: string; ms?: number }[]
  /** 建连耗时。取不到连接时为 null —— 不要在界面上拿 0 冒充「很快」 */
  latency_ms: number | null
}

/** 库里实际存在的表。**白名单之外的也会列出来** ——
 *  先看得见，才谈得上决定开不开放。 */
export interface IntrospectTable {
  name: string
  rows: number
  cols: number
  tenant: boolean
  allowed: boolean
  tenant_column: string | null
  /** column = 表上有租户列；filter = 靠谓词间接归属；exempt = 显式声明与租户无关 */
  tenant_mode: 'column' | 'filter' | 'exempt' | 'none'
  tenant_via: string
  coverage: number
  desc: string
}

export interface Introspect {
  ok: boolean
  error?: string
  hint?: string
  tables: IntrospectTable[]
  allowed_count?: number
  total?: number
}

export async function fetchSchema(): Promise<Schema> {
  const response = await fetch('/api/schema')
  if (!response.ok) throw new Error(`/api/schema ${response.status}`)
  return response.json()
}

export async function fetchSelfCheck(): Promise<SelfCheck> {
  const response = await fetch('/api/selfcheck')
  if (!response.ok) throw new Error(`/api/selfcheck ${response.status}`)
  return response.json()
}

export async function fetchIntrospect(): Promise<Introspect> {
  const response = await fetch('/api/introspect')
  if (!response.ok) throw new Error(`/api/introspect ${response.status}`)
  return response.json()
}

/* ---------------- 数据源注册表 ---------------- */

export interface SourceCard {
  id: string
  name: string
  type: string
  env: string
  host: string
  credential: string
  created_at: string
  table_count: number
  builtin: boolean
}

export interface SourceList {
  can_add: boolean
  supported_types: string[]
  /** 主密钥没配就只有「环境变量名」这一条路，表单据此禁用明文口令 */
  can_store_password: boolean
  items: SourceCard[]
}

export interface ScannedTable {
  name: string
  rows: number
  cols: number
  tenant: boolean
  allowed?: boolean
}

export interface Probe {
  ok: boolean
  checks: { name: string; ok: boolean; detail: string; ms?: number }[]
  latency_ms: number | null
  tables: ScannedTable[]
  error?: string
  hint?: string
}

export interface SourceInput {
  name: string
  type: string
  dsn: string
  env: string
  upstream?: string
  password_env?: string
  password?: string
}

/** 后端把不合规与连不上都表述成 detail 文本，原样抛给用户看 ——
 *  「操作失败」这种话对排查毫无帮助。 */
async function post<T>(url: string, body: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new Error(data?.detail || `${url} ${response.status}`)
  return data as T
}

export async function fetchSources(): Promise<SourceList> {
  const response = await fetch('/api/sources')
  if (!response.ok) throw new Error(`/api/sources ${response.status}`)
  return response.json()
}

export const testSource = (input: SourceInput) => post<Probe>('/api/sources/test', input)

export const createSource = (input: SourceInput) =>
  post<{ source: SourceCard } & Probe>('/api/sources', input)

export async function scanSource(id: string): Promise<Probe> {
  const response = await fetch(`/api/sources/${id}/scan`)
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new Error(data?.detail || `扫描失败 ${response.status}`)
  return data
}

export const setSourceTables = (id: string, tables: string[]) =>
  post<SourceCard>(`/api/sources/${id}/tables`, { tables }, 'PUT')

export const deleteSource = (id: string) =>
  post<{ ok: boolean }>(`/api/sources/${id}`, {}, 'DELETE')

/* ---------------- 查询 ---------------- */

export interface Step {
  step: string
  ms: number
  status: string
  note?: string
  tok_in?: number
  tok_out?: number
}

/** /api/ask 与 /api/sql 的返回。字段与 graph.AskResult 一一对应。
 *  直查模式（/api/sql）不经模型，只回填其中一部分，缺的字段按可选处理。 */
export interface AskResult {
  ok: boolean
  question: string
  trace_id: string
  org_id: number

  sql_raw?: string
  sql_final?: string
  reasoning?: string
  rules_fired?: string[]
  /** 护栏做过的改写（注入租户谓词、补 LIMIT、展开 SELECT *） */
  rewrites?: string[]

  columns?: string[]
  rows?: (string | number | boolean | null)[][]
  row_count?: number
  /** 触发 R-13 行数上限被截断 */
  truncated?: boolean
  /** 数据时间。不标时间的结果隔天再看会被当成当前状态 */
  as_of?: string

  rejected_by?: string | null
  error?: string
  hint?: string

  tables_hit?: string[]
  metrics_hit?: string[]
  attempts?: number
  step_count?: number
  multi_step?: boolean
  converged_early?: string
  steps?: Step[]
  /** 检查点线程。中断后靠它续跑；普通提问等于 trace_id */
  thread_id?: string
  elapsed_ms?: number
  tok_in?: number
  tok_out?: number
  cost_cny?: number
}

/** source 是运行时数据源 id；留空走启动配置里的内置源。 */
export const askQuestion = (question: string, source = '', orgId?: number) =>
  post<AskResult>('/api/ask', { question, source, org_id: orgId ?? null })

export const runSql = (sql: string, source = '', orgId?: number) =>
  post<AskResult>('/api/sql', { sql, source, org_id: orgId ?? null })

/** 从断点续跑。thread_id 非法/不存在/已跑完一律 404 —— 不提供未完成任务的枚举入口。 */
export async function resumeTask(threadId: string): Promise<AskResult | null> {
  const response = await fetch('/api/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId }),
  })
  if (response.status === 404) return null
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new Error(data?.detail || `/api/resume ${response.status}`)
  return data
}

/* ---------------- 身份与权限 ---------------- */

export interface RoleInfo {
  code: string
  name: string
  scope: string
  desc: string
  /** 系统角色只管人不看数据（职责分离），页面要把它和数据角色区分开 */
  system: boolean
  members: number
}

export interface RolesResponse {
  enabled: boolean
  /** 是否配了 ASKDB_ADMIN_TOKEN。没配则写接口整体关闭 —— fail-closed */
  writable: boolean
  roles: RoleInfo[]
}

export interface RoleMember {
  id: number
  role_code: string
  auth_user_id: number | null
  username: string
  display_name: string
  note: string
  created_at: string
  created_by: string
  /** 是否已关联到网关账号。登录接入前恒为 false，页面必须如实标注。 */
  bound: boolean
}

export async function fetchRoles(): Promise<RolesResponse> {
  const response = await fetch('/api/identity/roles')
  if (!response.ok) throw new Error(`/api/identity/roles ${response.status}`)
  return response.json()
}

export async function fetchMembers(roleCode: string): Promise<RoleMember[]> {
  const response = await fetch(`/api/identity/members?role=${encodeURIComponent(roleCode)}`)
  if (!response.ok) throw new Error(`/api/identity/members ${response.status}`)
  return (await response.json()).items
}

/** 管理员令牌只放在内存里，刷新即失效。
 *  它是部署方持有的共享口令，落进 localStorage 等于把它长期留在浏览器里。 */
async function adminWrite(url: string, token: string, init: RequestInit): Promise<void> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', 'X-Askdb-Admin-Token': token },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `${url} ${response.status}`)
  }
}

export function addMember(token: string, member: {
  role_code: string
  username: string
  display_name: string
  note: string
}): Promise<void> {
  return adminWrite('/api/identity/members', token, {
    method: 'POST',
    body: JSON.stringify(member),
  })
}

export function removeMember(token: string, id: number): Promise<void> {
  return adminWrite(`/api/identity/members/${id}`, token, { method: 'DELETE' })
}
