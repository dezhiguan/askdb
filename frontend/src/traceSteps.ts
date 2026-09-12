/** 图节点的展示口径。
 *
 * 单独成模块，是因为审计中心的复放和执行追踪页都要用它。各存一份必然漂移，
 * 而「后端加了节点、前端没跟上」这类漂移不报错，只是在页面上显示成原始 id。
 * tests/test_frontend.py 会扫 askdb/*.py 的全部 tracer.add 来钉住这份映射。
 */

export const STEP_NAMES: Record<string, string> = {
  // 唯一不由 tracer.add 产生的节点：命中应答缓存时，服务端手写这一条当整条链路
  cache: '应答缓存',
  quota: '配额检查',
  clarify: '问句主体判定',
  intent: '意图 / 可答性预检',
  schema_recall: 'Schema 召回',
  plan: '单步/多步判定',
  decide: 'Agent 决策',
  tool_call: '工具调用',
  generate_sql: 'SQL 生成',
  guard: '静态校验',
  scrub: '推理文案核对',
  grounding: '数字接地校验',
  hedge: '不确定性标记',
  dry_run: 'EXPLAIN 干跑',
  execute: '只读执行',
  assess: '结果自检',
  reflect: '反思重试',
  finalize: '结果与溯源',
  interrupted: '执行中断',
  // 只在直查建连失败时出现（server 手写那一条）。此前漏登记，界面上显示成
  // 英文原名 connect —— 而这恰恰是最需要一眼看懂的一格：库连不上。
  connect: '数据源建连',
  // 续跑前置校验：权限收窄 / 库连不上 / 表结构变了。同样漏登记过。
  resume_precheck: '续跑前校验',
}

/** 节点归类。这是对**图节点身份**的静态分类，不是运行时探测出来的 span kind ——
 *  某一步实际花没花 token，看它自己的 token 列，不要从这一列反推。 */
export const STEP_TYPE: Record<string, string> = {
  cache: 'SYS',
  quota: 'GUARD',
  clarify: 'GUARD',
  guard: 'GUARD',
  // 这两步判的是模型**说了什么**，不是数据 —— 与静态校验同属护栏一档：
  // 一个拦 SQL，一个拦文案里没有事实依据的陈述。
  scrub: 'GUARD',
  hedge: 'GUARD',
  // **只有 tool_call 是工具调用。**
  //
  // 2026-09-12 之前 schema_recall 也标 TOOL，而它是一个确定性召回节点：
  // 两条链路（agent 与老管道）进门都跑它，模型既选不了也跳不过。把它叫工具，
  // 界面上就会出现"这条链路调了 1 次工具"，而模型一次工具都没选过 ——
  // 老管道的 trace 里根本没有 tool_call 这一步。
  //
  // 判据是**谁决定要不要调它**：模型自己挑的（tools.REGISTRY 里那几个）才叫
  // 工具，流程写死的不叫。干跑与执行同理，它们是 execute_sql 这一个工具内部
  // 的阶段（tools.py 里一个 tracer 都没有，agent 只落一条 tool_call），
  // 单独出现时来自直查 /api/sql —— 那条路连模型都不过。
  schema_recall: 'RAG',
  intent: 'MODEL',
  plan: 'MODEL',
  decide: 'MODEL',
  tool_call: 'TOOL',
  generate_sql: 'MODEL',
  assess: 'MODEL',
  reflect: 'MODEL',
  dry_run: 'DB',
  execute: 'DB',
  // 直查建连失败时落这一条。是一次真实的库访问尝试，归 DB ——
  // 落到 SYS 兜底的话，"库连不上"在界面上看起来像一次系统内务。
  connect: 'DB',
  // 数字接地校验：纯函数，不碰 IO，判的是模型**说了什么**（结论里的数追不
  // 追得到查询结果）。与 scrub / hedge 同属护栏一档 —— 一个拦 SQL，
  // 这几个拦文案里没有事实依据的陈述。
  grounding: 'GUARD',
  // 续跑前置校验：权限、连接、表结构三项，都不过才放行。判的是能不能接着跑。
  resume_precheck: 'GUARD',
  finalize: 'SYS',
  interrupted: 'SYS',
}

/** 有产出、但不是主路径的产出 —— 与后端 trace.SOFT_STATUSES 同一份口径。
 *
 *  这三个值是这一版加的。在此之前状态列实际只有一个取值 `ok`，于是两类
 *  信息一起丢了：主模型超时切备选、被救回来的那条链路，和一次就成的干净
 *  链路长得一模一样；向量召回回落关键词这种"跑成了但能力降级了"，也只能
 *  算成成功。它们既不是 ok，也不该算失败，所以自成一档。 */
const SOFT_STATUSES: ReadonlySet<string> = new Set(['fallback', 'degraded', 'empty'])

/** 这一步是不是**硬失败**。
 *
 *  **不能拿 `status === 'ok'` 反推失败**：命中应答缓存那一步的 status 是 hit，
 *  它是一次正常收尾。按等于 ok 判，链路上唯一那个节点会渲染成红框、状态列
 *  标红 —— 一次零耗时的成功命中，在页面上长得像一次挂掉的调用。
 *  SOFT 档同理：把 fallback 算失败，模型被备选救回来时成功率反而下跌；
 *  把 empty 算失败，一次如实返回零行的查询会变成故障。
 *  以后再加别的非 ok 成功态，也只改这一处。 */
export const stepFailed = (status: string): boolean =>
  status !== 'ok' && status !== 'hit' && !SOFT_STATUSES.has(status)

/** 这一步有产出但不走主路径 —— 页面上给橙色，不给红色也不给绿色。 */
export const stepSoft = (status: string): boolean => SOFT_STATUSES.has(status)

/** 状态列的中文解释。表格里显示的仍是大写英文（照原型），这句进 title。 */
export const STATUS_HINT: Record<string, string> = {
  ok: '按主路径完成',
  hit: '命中应答缓存，未跑模型',
  fallback: '由备选模型或重试救回来的产出',
  degraded: '有产出，但能力低于主路径',
  empty: '执行成功但返回零行',
  failed: '这一步没有产出',
  blocked: '被规则拦下',
  skipped: '本次未执行',
}

export const KIND_NAMES: Record<string, string> = { ask: '提问', sql: '直查', resume: '续跑' }
