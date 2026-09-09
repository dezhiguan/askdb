/** 后端接口的最小接入层。
 *
 * 换壳阶段只接 /api/health —— 它决定外壳上那几个身份标识（数据源、模型、
 * 租户、当前配置）显示什么。这几项是**壳的一部分**：壳如果对连着哪个库
 * 都说不准，后面接上来的数据也无从判断出处。
 *
 * 其余页面仍在样例数据上，接口逐条接回来时在这里加函数，不要在组件里直接 fetch。
 */

/** 会话整体不作数了。
 *
 *  后端两道门（server.py 的 _gate_reads / _gate_writes）统一用
 *  `code: login_required` 说这件事，接口本身的 401 里没有 code —— 那类是
 *  「这个动作要登录」（扫描数据源、创建任务），页面照常把话渲染出来即可。
 *  这里说的是另一回事：**当前这张会话票整体不作数了**（票过期、换了签名
 *  密钥，或者实例把 auth.required 打开了）。这时候画一条红色的「读取失败」
 *  是把一扇门说成一场故障 —— 看的人会去查一个不存在的事故。 */
export class LoginRequired extends Error {
  readonly code: string
  constructor(code: string, message: string) {
    super(message)
    this.name = 'LoginRequired'
    this.code = code
  }
}

/** 会话失效时通知外壳去重取身份、把登录页摆出来。
 *
 *  挂在模块上而不是每个调用点各接一次：二十多个接口撞的是同一件事，
 *  逐个接等于给「将来新增一个忘了接」留位置，而漏掉的那个会以红色故障条
 *  的样子出现，没有任何信号说它其实只是没登录。 */
let onLoginRequired: (() => void) | null = null

export function setLoginRequiredHandler(handler: (() => void) | null): void {
  onLoginRequired = handler
}

/** 带会话失效识别的 fetch。
 *
 *  本文件里除 /api/auth/me 与登录/退出自己那几条之外，一律走它 ——
 *  /api/auth/me 是读门白名单，永远不会返回 login_required；真让它也走这里，
 *  一旦哪天它 401，处理器又去重取它，就是一个自己喂自己的死循环。 */
async function request(input: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init)
  if (response.status === 401) {
    // clone 之后再读：正文只能消费一次，调用方还要拿它取 detail
    const body = await response.clone().json().catch(() => null) as
      { code?: string; detail?: string } | null
    if (body?.code === 'login_required' || body?.code === 'login_unavailable') {
      onLoginRequired?.()
      throw new LoginRequired(body.code, body.detail || '会话已失效，请重新登录。')
    }
  }
  return response
}

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
    /** 配置里是否声明了默认数据源。false 时 ok 仍为 true —— 没配不是故障 */
    configured: boolean
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
  const response = await request('/api/health')
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
  /** 这条调用出自谁的可见范围。老记录没有该字段，后端如实给「（未记录）」 */
  role: string
  /** 发起人账号。空串有两种来路，页面上要分开讲：
   *  未登录时后端把它抹成空串（与 question 同一道边界，见 text_visible）；
   *  已登录时的空串是真的没有发起人 —— 那次调用本来就是匿名发的。 */
  user: string
  /** 未登录时为 null —— 是"看不到"，不是"没有" */
  question: string | null
  rejected_by: string | null
  attempts: number | null
  rows_returned: number | null
  elapsed_ms: number
  cost_cny: number | null
  step_count: number | null
  multi_step: boolean | null
  /** 这次调用打在哪个数据源上。builtin 表示配置里的默认源 */
  source: string | null
  source_name: string | null
  ok: boolean
}

export interface AuditList {
  total: number
  /** 筛之前有多少条（可见范围之内）。"命中 N / M"的 M，
   *  也是"筛完没有"与"本来就没有"两句不同提示的判据 */
  total_all: number
  page: number
  page_size: number
  items: AuditItem[]
  /** 问题原文是否可见。未登录时后端不返回原文，且搜索只匹配 trace_id ——
   *  页面据此显示遮蔽提示，而不是让人以为这些记录本来就没问过问题。 */
  text_visible: boolean
  /** 可见记录里真出现过的数据源（在其余筛选之前算），供下拉直接用。
   *  空 id 是"未记录数据源"那一档，不是"全部"。 */
  sources?: { id: string; name: string }[]
  /** 同上，可见记录里真出现过的发起人。空 id 是"匿名发起"那一档。
   *  text_visible 为 false 时后端给空表 —— 那份名单本身就是内容。 */
  users?: { id: string; name: string }[]
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
  /** 模型调用的成败按**节点**算（判定/生成/自检/反思），不是按整次调用算：
   *  失败后重试成功的那次，整次调用是成功的，但模型确实失败过。
   *  窗口内没有模型节点时 model_success 为 null —— 0/0 既不是 0% 也不是 100%。 */
  model_calls: number
  model_failed: number
  model_success: number | null
  /** 最近秩分位：一定是真发生过的某一次耗时，不是插值。窗口内无调用时为 null。 */
  elapsed_p50_ms: number | null
  elapsed_p95_ms: number | null
  daily: { date: string; calls: number; cost_cny: number }[]
  by_kind: Record<string, number>
  by_rule: Record<string, number>
  by_model: Record<string, { calls: number; cost_cny: number }>
  /** 审批汇总。来自审批流水（另一条流水），不在审计记录里 —— 服务端合进来的。
   *
   *  pending 是**当前**未决数、不受 days 约束，与任务中心「等待审批」同源；
   *  decided / avg_decide_ms 按决策时刻落在 days 窗口内计。两个口径不同是
   *  有意的，渲染时别把它们并成一句"近 N 天"。
   *
   *  整块字段为 null = 审批存储这次没读出来，不是"没有待审批"。 */
  approval: {
    pending: number | null
    decided: number | null
    avg_decide_ms: number | null
  }
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
  /** ok / rejected / interrupted，空串是不筛。非法值后端直接 400 */
  status?: string
  /** 数据源 id。'' 是合法取值（未记录数据源），所以"不筛"用 undefined 表示 */
  source?: string
  /** 发起人。'' 是合法取值（匿名发起），"不筛"同样用 undefined 表示。
   *  看不到原文的身份传了这个参数后端 403 —— 页面不该给出这个入口 */
  user?: string
  /** all / today / 7d / 30d。非法值后端 400 */
  since?: string
}): Promise<AuditList> {
  const query = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.pageSize),
    q: params.q,
    kind: params.kind,
  })
  if (params.status) query.set('status', params.status)
  if (params.source !== undefined) query.set('source', params.source)
  if (params.user !== undefined) query.set('user', params.user)
  if (params.since && params.since !== 'all') query.set('since', params.since)
  const response = await request(`/api/audit?${query}`)
  if (!response.ok) throw new Error(`/api/audit ${response.status}`)
  return response.json()
}

export async function fetchAuditStats(days = 30): Promise<AuditStats> {
  const response = await request(`/api/audit/stats?days=${days}`)
  if (!response.ok) throw new Error(`/api/audit/stats ${response.status}`)
  return response.json()
}

/** 执行追踪页的节点链（/api/trace）。
 *
 *  刻意与 Replay 分开：回放要登录 + 开关，返回 SQL 全文与问题原文；
 *  这里只有节点链与计量，SQL 只以哈希出现，未登录照样看得到。 */
export interface TraceChain {
  trace_id: string
  ts: string
  kind: string
  thread_id: string | null
  role: string | null
  model: string | null
  tok_in: number | null
  tok_out: number | null
  step_count: number | null
  multi_step: boolean | null
  attempts: number | null
  elapsed_ms: number | null
  cost_cny: number | null
  rejected_by: string | null
  source: string | null
  source_name: string | null
  steps: ReplayStep[]
  sql_hash: string | null
}

/** 节点链的三种结局。**取不到不能再collapse成 null**：
 *
 *  这里原来失败一律返回 null，而页面把 null 与"steps 为空"渲染成同一个样子 ——
 *  一张只有表头的空表。2026-09-07 撞上一次：`/api/trace` 因为拿启动配置判可见性，
 *  把运行时数据源上的记录全判成 404，界面上就是"列得出来、点进去空白"，
 *  读起来完全像"这条调用本来就没有节点"，排查从界面出发找不到任何线索。
 *
 *  `unavailable` 与 `failed` 分开：前者是后端的既定约定（记录不存在与无权同为
 *  404，区分本身就是信息泄露 —— 前端也**不**替它区分，只说"当前看不到"），
 *  后者是这次调用坏了（5xx、网络断），两句话对应的下一步动作完全不同。 */
export type TraceChainResult =
  | { status: 'ok'; data: TraceChain }
  | { status: 'unavailable' }
  | { status: 'failed'; message: string }

export async function fetchTraceChain(traceId: string): Promise<TraceChainResult> {
  let response: Response
  try {
    response = await request(`/api/trace?trace_id=${encodeURIComponent(traceId)}`)
  } catch (e) {
    // 网络层的失败也要说出来。吞掉它就又回到"空白等于没有数据"。
    return { status: 'failed', message: String((e as Error).message || e) }
  }
  if (response.status === 404) return { status: 'unavailable' }
  if (!response.ok) return { status: 'failed', message: `HTTP ${response.status}` }
  return { status: 'ok', data: await response.json() }
}

export async function fetchReplay(traceId: string): Promise<ReplayResult> {
  const response = await request(`/api/replay?trace_id=${encodeURIComponent(traceId)}`)
  if (response.status === 429) return { status: 'rate_limited' }
  if (!response.ok) return { status: 'not_found' }
  return { status: 'ok', data: await response.json() }
}

/**
 * 观测后端的地址是否对**当前访问者**可达。
 *
 * 自托管的 Langfuse 只在内网活着，公开实例上这个字段就是
 * `http://localhost:3000` —— 那是给部署方挂了 SSH 隧道之后用的。
 * 直接渲染成外链的话，访客点下去打的是**他自己机器的 3000 端口**，
 * 而 3000 是 Next.js / Grafana 这类的默认端口，运气不好会打开他本机
 * 碰巧在跑的东西。比"没反应"更糟。
 *
 * 判据只看 host：localhost / 环回 / 私网段一律视为访客不可达。
 */
export function tracingReachable(tracing: Tracing): boolean {
  const raw = tracing.url || tracing.host
  if (!raw) return false
  let host: string
  try {
    host = new URL(raw).hostname
  } catch {
    return false                       // 连 URL 都解析不了，更不该当链接给出去
  }
  if (host === 'localhost' || host === '127.0.0.1' || host === '::1') return false
  // RFC1918 私网段：10/8、172.16-31/12、192.168/16
  if (/^10\./.test(host)) return false
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(host)) return false
  if (/^192\.168\./.test(host)) return false
  return true
}

/** 观测后端里这条 trace 的深链。未接入**或访客不可达**时返回 null —— 由调用方渲染禁用态。 */
export function tracingLink(tracing: Tracing, traceId: string): string | null {
  if (!tracing.enabled || !tracing.backend) return null
  if (!tracingReachable(tracing)) return null
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
  owner: string
  /** expr 直接进 SELECT 列表，predicate 进 WHERE —— 用法不同，页面要分清 */
  kind: 'expr' | 'predicate' | ''
  /** 这条表达式只在什么聚合语境下成立。expr 是片段注入，
   *  保证得了表达式本身，保证不了它被放进什么查询里 */
  grain: string
  /** 这条口径在**当前角色**下能不能真的用 —— 它引用的表得都可见。
   *  口径本身一律给（这一页是给人读的词典），不可查的标出来，
   *  免得有人问了才发现问不出数。 */
  queryable: boolean
}

/** 一条口径的区分度核对结果。
 *  status=ok 时 value/naive 才有值；differs=false 说明这条口径当前
 *  检验不出模型有没有真的用它。 */
export interface MetricCheck {
  name: string
  status: 'ok' | 'skipped' | 'blocked' | 'error' | ''
  detail: string
  value?: string | number | boolean | null
  naive?: string | number | boolean | null
  differs?: boolean
}

/** 数据源不可用时，接口返回的是 200 + ok:false + 原因，不是 HTTP 错误。
 *  拼成一句能直接显示给人看的话 —— hint 里往往才是可执行的那半句。 */
function dataSourceReason(data: { error?: string; hint?: string }): string {
  return [data.error, data.hint].filter(Boolean).join(' · ')
}

export async function checkMetrics(source?: string): Promise<{ checked_at: string; items: MetricCheck[] }> {
  // 必须带上当前数据源：核对是**按真实数据跑**的，不带就落到启动配置上，
  // 而这套部署的启动配置里没有 datasource 段（两个库都在运行时注册表里），
  // 结果是整页一条红字而一条口径都没核对到。
  const query = source ? `?source=${encodeURIComponent(source)}` : ''
  const response = await request(`/api/metrics/check${query}`)
  if (!response.ok) throw new Error(`/api/metrics/check ${response.status}`)
  const data = await response.json()
  // 连不上库时 items 是空的。不拦下来就会显示成"一条口径都没有"，
  // 把"库连不上"讲成"没配口径"，排查方向直接错掉
  if (data.error) throw new Error(dataSourceReason(data))
  return data
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

export interface ReviewItem {
  trace_id?: string
  thread_id?: string
  question?: string | null
  review_status: 'REQUESTED' | 'ACCEPTED' | 'RETURNED'
  review_why?: string[]
  reviewer?: string
  note?: string
  decided_ts?: string
  owner?: string
  user?: string
  ts?: string
}

export interface ReviewQueue {
  can_review: boolean
  items: ReviewItem[]
  pending: number
}

/** 结果复核队列。**与审批是两件事**：审批是事前"这条该不该去跑"，
 *  复核是事后"跑出来的数字算不算数"。 */
export async function fetchReviews(): Promise<ReviewQueue> {
  const response = await fetch('/api/reviews')
  if (!response.ok) throw new Error(`/api/reviews ${response.status}`)
  return response.json()
}

export async function decideReview(
  traceId: string, accepted: boolean, note: string,
): Promise<void> {
  const response = await fetch(`/api/reviews/${encodeURIComponent(traceId)}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accepted, note }),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail || `/api/reviews/decide ${response.status}`)
  }
}

export async function fetchSchema(source?: string): Promise<Schema> {
  // 不带 source 时取内置配置 —— 但查询页**必须**带上：推荐问题、示例 SQL
  // 都从这份 schema 生成，取错源就会把另一个库的表名推给用户。
  const query = source ? `?source=${encodeURIComponent(source)}` : ''
  const response = await request(`/api/schema${query}`)
  if (!response.ok) throw new Error(`/api/schema ${response.status}`)
  return response.json()
}

export async function fetchSelfCheck(): Promise<SelfCheck> {
  const response = await request('/api/selfcheck')
  if (!response.ok) throw new Error(`/api/selfcheck ${response.status}`)
  const data = await response.json()
  // 这里判 error 而不是判 ok：自检项没过同样是 ok:false，那是正常结果，
  // 要照常渲染成一张检查表；只有取不到连接才是没有结果可言
  if (data.error) throw new Error(dataSourceReason(data))
  return data
}

export async function fetchIntrospect(): Promise<Introspect> {
  const response = await request('/api/introspect')
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
  /** **白名单张数，不是库里的表数。** 列表接口不连库（一个库挂了会拖住整页），
   *  库内实际可见多少张看 last_visible_count —— 它来自最近一次连接检查。 */
  table_count: number
  /** 最近一次连接检查。服务端落盘，刷新页面不丢；从未检查过时为空/null */
  last_checked_at: string
  last_ok: boolean | null
  last_latency_ms: number | null
  last_visible_count: number | null
  builtin: boolean
  /** 仅内置源有意义：删除会改配置文件，需开关允许且已有别的源接手 */
  deletable?: boolean
}

export interface SourceList {
  can_add: boolean
  supported_types: string[]
  /** 主密钥没配就只有「环境变量名」这一条路，表单据此禁用明文口令 */
  can_store_password: boolean
  /** 部署方在配置里指定的默认数据源（datasources.default）。空串 = 没指定，
   *  或本实例有内置源（那时默认就是它）。界面据此决定一进来停在哪个库 ——
   *  没有它就只能取列表第一个，而注册顺序不表达任何意图。 */
  default_source_id: string
  /** 指定了默认源却取不到的原因（名字写错、源已删、注册表连不上）。
   *  空串 = 没有这个问题。**取不到时服务端不会替它挑一个源**。 */
  default_source_error: string
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
  /** 建连耗时（握手 + 认证）。连不上时为 null —— 不要在界面上拿 0 冒充「很快」 */
  latency_ms: number | null
  /** 检查那一刻库里实际可见的表数。与 SourceCard.table_count 比对即可看出漂移 */
  visible_count: number | null
  tables: ScannedTable[]
  /** 服务端记下这次检查的时刻。以它为准，不要用浏览器本地时钟 */
  checked_at?: string
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

/** 被限流了。单拎一个类型，是因为界面对它的处置和别的错不同：别的错要人
 *  去改点什么，这个只要等 —— 等多久是可以说出来的，就别让人猜。 */
export class RateLimited extends Error {
  readonly retryAfter: number
  constructor(message: string, retryAfter: number) {
    super(message)
    this.name = 'RateLimited'
    this.retryAfter = retryAfter
  }
}

/** 429 → RateLimited。取不到 Retry-After 就退回窗口长度，宁可多等一会儿，
 *  也不要给出一个比实际短的倒计时 —— 那会让人到点再点一次又被弹回来。 */
function rateLimited(response: Response, detail: string): RateLimited {
  const header = Number(response.headers.get('Retry-After'))
  return new RateLimited(detail || '操作过于频繁',
                         Number.isFinite(header) && header > 0 ? header : 60)
}

/** 后端把不合规与连不上都表述成 detail 文本，原样抛给用户看 ——
 *  「操作失败」这种话对排查毫无帮助。 */
async function post<T>(url: string, body: unknown, method = 'POST'): Promise<T> {
  const response = await request(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await response.json().catch(() => null)
  if (response.status === 429) throw rateLimited(response, data?.detail)
  if (!response.ok) throw new Error(data?.detail || `${url} ${response.status}`)
  return data as T
}

export async function fetchSources(): Promise<SourceList> {
  const response = await request('/api/sources')
  if (!response.ok) throw new Error(`/api/sources ${response.status}`)
  return response.json()
}

export const testSource = (input: SourceInput) => post<Probe>('/api/sources/test', input)

export const createSource = (input: SourceInput) =>
  post<{ source: SourceCard } & Probe>('/api/sources', input)

export async function scanSource(id: string): Promise<Probe> {
  const response = await request(`/api/sources/${id}/scan`)
  const data = await response.json().catch(() => null)
  if (response.status === 429) throw rateLimited(response, data?.detail)
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
  /** EXPLAIN 估算的扫描行数。返回 3 行是从多少行里筛出来的 —— 判断这条查询
   *  贵不贵、结果可不可信的关键一维 */
  explain_rows?: number | null

  rejected_by?: string | null
  error?: string
  hint?: string

  tables_hit?: string[]
  metrics_hit?: string[]
  /** 这次召回是盲选：给模型的表不是按相关度选出来的，答案可能答非所问。
   *  必须显示 —— 盲选下的答案与正常答案在页面上长得一模一样。 */
  recall_blind?: boolean
  /** 召回过程中要告知用户的话（盲选、全库兜底、向量回落）。 */
  recall_note?: string
  /** 被脱敏的返回列。星号得有个出处，否则看的人以为库里就是这样。 */
  masked_columns?: string[]
  /** 脱敏判定退化过：SQL 解析不出投影来源，整行按敏感返回。 */
  mask_degraded?: boolean
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

/** source 是运行时数据源 id；留空走启动配置里的内置源。
 *
 *  asTask=true 表示这次提问来自「创建任务」。后端据此要求登录 —— 任务与普通
 *  提问走同一条链路，后端分辨不出来，只能由调用方声明。 */
export const askQuestion = (question: string, source = '', orgId?: number, asTask = false) =>
  post<AskResult>('/api/ask', { question, source, org_id: orgId ?? null, as_task: asTask })

export const runSql = (sql: string, source = '', orgId?: number) =>
  post<AskResult>('/api/sql', { sql, source, org_id: orgId ?? null })

/** 从断点续跑。thread_id 非法/不存在/已跑完/不属于当前账号，一律 404 且响应一致。
 *  枚举入口只对**已登录用户**开放，且只列自己的（见 /api/tasks）。 */
export async function resumeTask(threadId: string): Promise<AskResult | null> {
  const response = await request('/api/resume', {
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
  /** 系统角色额外持有审批权，页面要把它和其他角色区分开。
   *  它**不再**意味着"只管人不看数据"—— 2026-09-06 起系统管理员照样能查数。 */
  system: boolean
  members: number
  /** 数据期限（天）。null = 不限。内置默认对所有角色都是 null；
   *  只有部署方在 role_policies 里手工收窄时才不是。 */
  max_age_days: number | null
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
  /** 出自配置内置名册（auth.accounts）而非管理员登记。
   *  内置条目 id 恒为 0、删不掉 —— 它们由配置文件管理，页面据此禁用「移除」。 */
  builtin: boolean
}

export async function fetchRoles(): Promise<RolesResponse> {
  const response = await request('/api/identity/roles')
  if (!response.ok) throw new Error(`/api/identity/roles ${response.status}`)
  return response.json()
}

/** "按设计不给你看"，不是故障。
 *
 *  完整成员名册只对数据负责人与系统管理员开放（设计文档 I-02），别的角色
 *  只看得到自己所属的那一档。这种 401/403 与"服务坏了"必须分开渲染 ——
 *  把权限边界画成一条红色的「读取失败」，看的人会去查一个不存在的故障。 */
export class Forbidden extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'Forbidden'
  }
}

/** 名册一页。total 是**整个角色**的人数，不是这一页的条数 ——
 *  卡片标题上的「N 人」和页码都靠它。 */
export interface MembersPage {
  items: RoleMember[]
  total: number
  /** 筛之前这个角色有多少人。"命中 N / M 人"的 M ——
   *  只给筛完的数字，"筛完没有"与"这个角色本来就没人"在页面上分不开 */
  total_all: number
  page: number
  page_size: number
}

export interface MemberQuery {
  /** 关键词：网关用户名 / 姓名 / 备注 */
  q?: string
  /** all / bound / unbound / builtin。非法值后端 400 */
  bound?: string
  /** all / today / 7d / 30d。非法值后端 400。选了任何一档时间，
   *  配置内置的成员整体不参与 —— 它们没有加入时间 */
  since?: string
}

export async function fetchMembers(
  roleCode: string, page = 1, pageSize = 10, filters: MemberQuery = {},
): Promise<MembersPage> {
  const query = new URLSearchParams({
    role: roleCode, page: String(page), page_size: String(pageSize),
    q: filters.q ?? '', bound: filters.bound ?? 'all', since: filters.since ?? 'all',
  })
  const response = await request(`/api/identity/members?${query}`)
  if (response.status === 401 || response.status === 403) {
    throw new Forbidden(`/api/identity/members ${response.status}`)
  }
  if (!response.ok) throw new Error(`/api/identity/members ${response.status}`)
  const body = await response.json()
  return {
    items: body.items ?? [], total: body.total ?? 0,
    total_all: body.total_all ?? body.total ?? 0,
    page: body.page ?? page, page_size: body.page_size ?? pageSize,
  }
}

/** 管理员令牌只放在内存里，刷新即失效。
 *  它是部署方持有的共享口令，落进 localStorage 等于把它长期留在浏览器里。 */
async function adminWrite(url: string, token: string, init: RequestInit): Promise<void> {
  const response = await request(url, {
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

/* ---------------- 登录与会话 ---------------- */

export interface Me {
  enabled: boolean
  /** false = 匿名可用，登录是可选的能力展示而不是门 */
  required: boolean
  username: string | null
  display_name: string
  roles: string[]
  /** 当前身份的**生效边界**。权限体系最怕「配了但看不出有没有生效」。
   *  max_age_days = 数据期限（天），null = 不限。取的是**生效值**：
   *  运行时源上时间窗口落不了地，后端在那种情况下一律给 null。 */
  scope: { tables: string[]; max_rows: number; max_age_days: number | null }
}

export async function fetchMe(): Promise<Me> {
  const response = await fetch('/api/auth/me')
  if (!response.ok) throw new Error(`/api/auth/me ${response.status}`)
  return response.json()
}

/** 登录 / 退出自己不走 request()：口令不对时后端返回的 401 里没有 code，
 *  本来也不会被当成"会话失效"，但这条路径是**恢复会话的那条路**，
 *  让它去触发"会话失效"处理器只会绕回它自己。 */
async function authPost(url: string, body: unknown): Promise<void> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail || `登录失败（${response.status}）`)
  }
}

export function login(username: string, password: string): Promise<void> {
  return authPost('/api/auth/login', { username, password })
}

export function logout(): Promise<void> {
  return authPost('/api/auth/logout', {})
}

/* ---------------- 任务（可续跑的中断调用） ---------------- */

export interface Task {
  thread_id: string
  trace_id: string
  ts: string
  first_ts: string
  question: string | null
  kind: string
  role: string
  user: string
  /** 这条线程上已经跑过几次（首次 + 每次续跑各算一次） */
  attempts_on_thread: number
  /** 这条线程跑在哪个数据源上（审计 summary 字段）。老记录可能没有，
   *  筛选按空串归到"未记录"那一档，不假装它属于默认源。 */
  source?: string | null
  source_name?: string | null
  elapsed_ms: number
  cost_cny: number | null
  /** 线程当前状态，看它最后一条记录。**每一档都对应一个"下一步该谁动手"**，
   *  这是分档的全部意义 —— 2026-09-07 之前四种结局压在同一个 rejected 里，
   *  页面一律显示「已拦截」，而其中三种其实还有下一步：
   *    running          系统正在执行（只落了发起记录，还没收尾）
   *    done             正常收尾
   *    rejected         安全红线：护栏拦下，改写法也过不去
   *    waiting_input    等用户补充：模型没产出 SQL，把问题说具体些
   *    waiting_approval 等负责人放行：审批通过后凭票重跑
   *    needs_operator   等运维：数据源连不上或执行期故障，恢复后可重试
   *    interrupted      断点在，可续跑 */
  status: 'running' | 'done' | 'rejected' | 'waiting_input'
        | 'waiting_approval' | 'waiting_review' | 'review_returned'
        | 'needs_operator' | 'interrupted'
  /** 下一步该谁动手，后端给的原话。页面直接显示，别在前端再写一遍 if/else。 */
  next_actor?: string
  /** 这条结果**为什么**值得复核（盲选召回、脱敏退化、反复重试、触顶收敛）。
   *  复核人要判断的正是这几句；让他自己猜"这条为什么进队列"，队列就没人用。 */
  review_why?: string[]
  /** 被哪条护栏规则拦下（R-03 / R-11 / EXEC …）。未被拦下为 null。 */
  rejected_by?: string | null
  /** 风险档与理由。审计里没有这个字段，是后端按已记录事实**折算**出来的
   *  （见 askdb/audit.py 的 _risk）—— 所以理由必须跟着走，页面要能解释。 */
  risk?: 'HIGH' | 'MEDIUM' | 'LOW' | null
  risk_why?: string | null
  /** 只有 interrupted 的线程能续跑。列表列全部线程，续跑入口只对它们开放 ——
   *  给已收尾的线程也挂一个续跑按钮，点了必然失败 */
  resumable: boolean
  /** 发起人。空串 = 匿名发起，不是"没记录"。
   *  续跑只有主人能做（服务端校验），页面据此置灰入口 —— 列表列全部线程，
   *  能不能动是另一回事。 */
  owner: string
}

/** 任务中心的统计卡。**算在筛选之前**（服务端 audit.paginate_tasks）——
 *  这四个数讲的是系统当下的处境，跟着筛选变就不是这回事了。
 *  success_rate 为 null = 还没有收尾记录，不是 0%。 */
export interface TaskStats {
  running: number
  waiting_input: number
  waiting_approval: number
  waiting_review: number
  review_returned: number
  needs_operator: number
  interrupted: number
  rejected: number
  done: number
  done_today: number
  success_rate: number | null
}

export interface TaskFilterOptionDto { value: string; label: string }

/** 任务列表的筛选取值。`all` 是不筛，空串是**合法的一档**
 *  （未记录数据源 / 匿名发起）—— 用空串当"不筛"，那两档就永远选不中。 */
export interface TaskQuery {
  page?: number
  pageSize?: number
  status?: string
  source?: string
  risk?: string
  user?: string
  since?: string
  /** 关键词：问题原文 / 线程 id / trace id。**筛选在服务端做** ——
   *  在浏览器里过滤当前这一页，搜的就只是十行，搜不到的看起来像不存在 */
  q?: string
}

/** 任务列表列**全部**线程（2026-09-06 起不再按发起人收窄），
 *  但一次只出一页（2026-09-08 起分页在服务端）。
 *
 *  items 是当前这一页；total 是筛完的条数（页码按它算）；total_all 是筛之前
 *  的条数（卡片标题上的「共 N 条」、以及"一条都没有"与"筛完没有"两句不同
 *  提示的判据）。stats / sources / users 都算在筛选之前，由服务端给。
 *
 *  user 是当前账号，不是过滤条件：页面拿它与每条的 owner 比，决定续跑入口
 *  对谁开。空串即匿名。 */
export interface TasksResult {
  items: Task[]
  total: number
  total_all: number
  page: number
  page_size: number
  stats: TaskStats
  sources: TaskFilterOptionDto[]
  users: TaskFilterOptionDto[]
  user: string
}

const EMPTY_TASK_STATS: TaskStats = {
  running: 0, waiting_input: 0, waiting_approval: 0, waiting_review: 0,
  review_returned: 0, needs_operator: 0, interrupted: 0, rejected: 0,
  done: 0, done_today: 0, success_rate: null,
}

export async function fetchTasks(query: TaskQuery = {}): Promise<TasksResult> {
  const params = new URLSearchParams({
    page: String(query.page ?? 1),
    page_size: String(query.pageSize ?? 10),
    status: query.status ?? 'all',
    source: query.source ?? 'all',
    risk: query.risk ?? 'all',
    user: query.user ?? 'all',
    since: query.since ?? 'all',
    q: query.q ?? '',
  })
  const response = await request(`/api/tasks?${params}`)
  if (!response.ok) throw new Error(`/api/tasks ${response.status}`)
  const body = await response.json()
  return {
    items: body.items ?? [],
    total: body.total ?? 0,
    total_all: body.total_all ?? 0,
    page: body.page ?? 1,
    page_size: body.page_size ?? 10,
    stats: body.stats ?? EMPTY_TASK_STATS,
    sources: body.sources ?? [],
    users: body.users ?? [],
    user: body.user || '',
  }
}

/* ---------------- Agent 质量中心 ---------------- */

/** 一个执行节点的聚合。数据来自审计记录里的 steps —— 端到端慢在哪一段，
 *  只能靠它回答。 */
export interface QualityNode {
  step: string
  calls: number
  success_rate: number | null
  p50_ms: number | null
  p95_ms: number | null
  tok: number
  /** 这个节点失败时最常见的那条 note。没失败过就是空串 —— 不编一个理由出来 */
  fail_reason?: string
  fails?: number
}

export interface LiveQuality {
  days: number
  runs: number
  ok: number
  /** 被护栏拦下的次数。**与 failed 分开看** —— 拦截是护栏在做对事，不是故障 */
  blocked: number
  /** 执行类失败（数据源异常、模型调用失败） */
  failed: number
  success_rate: number | null
  block_rate: number | null
  p50_ms: number | null
  p95_ms: number | null
  avg_tok: number | null
  cost_cny: number
  avg_cost_cny: number | null
  by_rule: Record<string, number>
  /** 护栏规则 R-xx 命中次数。**不等于 blocked** —— NO_SQL 这类是链路结果，
   *  混进来会让"安全事件"永远不为 0，也就失去了它唯一的用处 */
  security_events: number
  /** 有审计记录以来最早的一条。用来说"跑了多久"——不是部署时间，措辞要写清 */
  first_ts: string | null
  /** 本进程此刻的事实，不是从审计算出来的 */
  service?: { version: string; model: string; config: string }
  /** 上一个等长窗口的同口径值，供页面算环比。runs 少时涨跌没有意义 —— 页面据此决定报不报 */
  prev?: {
    runs: number
    p95_ms: number | null
    avg_tok: number | null
    avg_cost_cny: number | null
    nodes: Record<string, { calls: number; p95_ms: number | null }>
  }
  nodes: QualityNode[]
  /** 工具（链路节点）调用总量与失败集中在哪 —— 设计稿「工具调用成功率」那张卡 */
  tools?: { calls: number; ok: number; fails: number; top_fail_step: string; top_fail_share: number | null }
  /** 只读执行节点的成败。**不等于结果准确率** */
  sql?: { calls: number; ok: number }
  /** attempts>1 的任务里最终没被拒的占比 */
  retry?: { retried: number; recovered: number; rate: number | null }
  /** 断过的线程里最终正常收尾的占比。rate 为 null = 窗口内没断过，不是 0% */
  resume?: { interrupted: number; recovered: number; rate: number | null }
  /** 同一个人在 window_min 分钟内又提交一次的比例。匿名会被并成一个人 —— 会高估 */
  repeat?: { n: number; rate: number | null; window_min: number }
  /** 窗口等分七段的趋势，供 sparkline。空段照样在，不跳过 */
  series?: { runs: number; tool_rate: number | null; sql_rate: number | null; p95_ms: number | null }[]
  /** 走到人工审批的次数与占比 —— 来自审批流水，不在审计里 */
  intervention?: { n: number; rate: number | null }
}

export async function fetchLiveQuality(days: number): Promise<LiveQuality> {
  const response = await request(`/api/quality/live?days=${days}`)
  if (!response.ok) throw new Error(`/api/quality/live ${response.status}`)
  return response.json()
}

/** 离线回归结果。available:false 时页面显示「尚未运行」，不编数字。 */
export interface OfflineQuality {
  available: boolean
  /** 这组成绩出自哪个数据源、是不是当前这个。matches_current 为 false 时
   *  必须显眼提示 —— 否则会拿别的库的成绩当本实例的。 */
  provenance?: {
    config?: string
    datasource?: string
    model?: string
    golden?: string
    n_cases?: number
    current_datasource?: string
    matches_current?: boolean
  }
  blind?: {
    n: number
    accuracy: number
    false_reject: number
    block_rate: number
    multi_misuse: number
    /** 安全三项。**可能为 null** —— 那是"这一轮没有这类用例"，不是 0：
     *  0% 泄漏是测出来的结论，没考过是没有结论，页面上必须分开显示。 */
    danger_block_rate?: number | null
    escalation_rate?: number | null
    leak_rate?: number | null
    /** 安全场景覆盖 {场景: [守住, 总数]}。老结果文件里没有这个键。 */
    scenes?: Record<string, [number, number]>
    /** 业务口径命中率。null = 本轮没有判得动的题（或结果文件早于这项判定） */
    metric_hit_rate: number | null
    /** 参与口径命中判定的题数 —— 分母必须跟着率一起给 */
    metric_graded_n: number | null
    /** 结果完整度：结果集可直接作答的比例。null = 本轮没有判得动的题 */
    completeness: number | null
    complete_graded_n: number | null
    p95_ms: number
    cost_cny: number
    /** 每题平均 token（输入 + 输出）。老结果文件由后端按逐题记录现算 */
    avg_tok: number | null
    failure_kinds: Record<string, number>
    /** 上一轮同源回归的成绩，用来出环比。出处不一致时后端不给这个字段 */
    prev?: {
      n: number
      p95_ms: number | null
      cost_cny: number | null
      avg_tok: number | null
    }
  }
  /** 消融分组 A–F：同一黄金集下逐层加能力的对照 */
  groups?: {
    key: string
    label: string
    n: number
    accuracy: number
    false_reject: number
    cost_cny: number
    p95_ms: number
    rerun: boolean
    vs_base: { delta: number; n: number } | null
  }[]
  /** 黄金集构成：全集与本次盲测实跑数一起给 */
  golden?: {
    path: string
    total: number
    blind_n: number
    by_category: Record<string, number>
    /** 评测集文件的 mtime。没有版本号、也没人记"谁改了考题"，这是唯一能说的真话 */
    updated_at?: string
    /** 有标准答案的题数。齐了才算这套题判得动 */
    answered?: number
  }
  failures?: {
    id: string
    category: string
    reason: string
    detail: string
    trace_id: string
    question: string
  }[]
  /** 故障注入结果（evals/chaos.py）。没跑过就没有这个字段 —— 页面据此显示
   *  「未测量」，不拿 0 顶替。rate 为 null = 这一类一次都没注入成功。 */
  chaos?: {
    /** 这一轮跑在哪个库上 */
    datasource: string
    /** 出处与当前连接是否一致。false 时页面必须标出来，不能当本实例的成绩读 */
    matches_current: boolean
    n_cases: number
    /** 基线就没跑通、被排除在分母外的题数 */
    skipped: number
    ran_at: string
    faults: { key: string; label: string; injected: number; recovered: number
              rate: number | null }[]
  }
  /** 发布门禁评分。四个维度由真实结果算，
   *  但 weight 与 gate 是**项目策略、不是测量值** —— policy_note 必须原样显示 */
  score?: {
    overall: number
    gate: number
    pass: boolean
    dimensions: { key: string; label: string; weight: number; value: number; source: string }[]
    policy_note: string
  }
  /** 评测集清单。passed 为 null = 本轮没跑到 —— 不是通过 */
  cases?: {
    id: string
    category: string
    /** 安全场景（非安全题为空）：write_ddl | escalation | sensitive | injection */
    scene?: string
    question: string
    in_blind: boolean
    /** 标准答案：判分实际拿什么对（标准 SQL + 约束，或应拒规则） */
    expect: string
    /** 这条题有没有标准答案。汇总那格「标准答案 X / Y」按它算 */
    has_answer: boolean
    passed: boolean | null
    /** 跑到了但判不动（安全题遇上没有该维度的数据源）。false 时不显示 PASS。 */
    graded?: boolean
    reason: string
    trace_id: string
  }[]
  replay_config?: string
  /** 当前默认配置对应的消融组 */
  shipped?: string
  /** 本轮结果文件的最后写入时间（评测报告里没有开跑/收工时间戳） */
  ran_at?: string
  /** 同一数据源下跑过的历次盲测，新的在前。**没有 Agent 版本号这回事** ——
   *  结果文件不记版本，所以行标题是结果文件名，不是 v2.4 这种编出来的版本 */
  runs?: {
    file: string
    n: number
    ran_at: string
    overall: number
    pass: boolean
    /** 是不是当前页面上这一轮 */
    current: boolean
  }[]
}

/** 一轮回归的实时状态。字段与后端 evalrun.RunState 一一对应。 */
export interface EvalRunState {
  status: 'idle' | 'running' | 'done' | 'failed'
  started_at: string
  finished_at: string
  done: number
  total: number
  group: string
  /** 跑在哪个数据源上 —— 成绩离开数据源没有意义，所以它和分数一起回 */
  datasource: string
  error: string
  accuracy: number | null
  passed: number
}

export async function fetchEvalRun(): Promise<EvalRunState> {
  const response = await request('/api/eval/run')
  if (!response.ok) throw new Error(`/api/eval/run ${response.status}`)
  return response.json()
}

/** 触发一轮回归。已在跑（409）与本部署不含评测套件（501）都要把后端的
 *  说明原样带出来 —— 这两种情况页面上的处置完全不同。 */
export async function startEvalRun(): Promise<EvalRunState> {
  const response = await request('/api/eval/run', { method: 'POST' })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body?.detail || `/api/eval/run ${response.status}`)
  return body
}

export async function fetchOfflineQuality(): Promise<OfflineQuality> {
  const response = await request('/api/eval')
  if (!response.ok) throw new Error(`/api/eval ${response.status}`)
  return response.json()
}

// ---------- 高成本查询审批（P07 / 设计文档 Q-08） ----------

export interface Approval {
  id: string
  status: 'REQUESTED' | 'APPROVED' | 'REJECTED' | 'CONSUMED'
  ts: string
  user: string
  roles: string[]
  kind: 'ask' | 'sql'
  question: string
  sql: string
  est_rows: number | null
  threshold: number
  source: string
  approver?: string
  note?: string
  decided_ts?: string
}

export interface ApprovalsResult {
  /** 有 APPROVE 能力位（系统管理员）。没有的人只看得到自己提的 */
  can_approve: boolean
  items: Approval[]
}

export async function fetchApprovals(): Promise<ApprovalsResult> {
  const response = await request('/api/approvals')
  if (!response.ok) throw new Error(`/api/approvals ${response.status}`)
  return response.json()
}

export async function decideApproval(
  id: string, approved: boolean, note: string,
): Promise<Approval> {
  const response = await request(`/api/approvals/${encodeURIComponent(id)}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, note }),
  })
  if (!response.ok) {
    // 409 = 已经有过结论。把后端那句话原样带出去 —— 它说明的是
    // "别人已经批过了"，与"你没权限"完全不同，含糊会让人反复重试。
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `/api/approvals ${response.status}`)
  }
  return response.json()
}
