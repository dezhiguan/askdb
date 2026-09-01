/** 图节点的展示口径。
 *
 * 单独成模块，是因为审计中心的复放和执行追踪页都要用它。各存一份必然漂移，
 * 而「后端加了节点、前端没跟上」这类漂移不报错，只是在页面上显示成原始 id。
 * tests/test_frontend.py 会扫 askdb/*.py 的全部 tracer.add 来钉住这份映射。
 */

export const STEP_NAMES: Record<string, string> = {
  quota: '配额检查',
  schema_recall: 'Schema 召回',
  plan: '单步/多步判定',
  generate_sql: 'SQL 生成',
  guard: '静态校验',
  dry_run: 'EXPLAIN 干跑',
  execute: '只读执行',
  assess: '结果自检',
  reflect: '反思重试',
  finalize: '结果与溯源',
  interrupted: '执行中断',
}

/** 节点归类。这是对**图节点身份**的静态分类，不是运行时探测出来的 span kind ——
 *  某一步实际花没花 token，看它自己的 token 列，不要从这一列反推。 */
export const STEP_TYPE: Record<string, string> = {
  quota: 'GUARD',
  guard: 'GUARD',
  schema_recall: 'TOOL',
  plan: 'MODEL',
  generate_sql: 'MODEL',
  assess: 'MODEL',
  reflect: 'MODEL',
  dry_run: 'DB',
  execute: 'DB',
  finalize: 'SYS',
  interrupted: 'SYS',
}

export const KIND_NAMES: Record<string, string> = { ask: '提问', sql: '直查', resume: '续跑' }
