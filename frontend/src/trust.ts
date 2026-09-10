/** 可信度的**唯一**口径。工作台右栏那枚环、执行追踪页那枚角标，
 *  以及将来任何要显示"这次结果可不可信"的地方，都必须调这里。
 *
 *  为什么要单独一份：这两处判的本来就是同一次查询。各写各的，迟早出现
 *  侧栏 100 / 追踪页 80 的场面 —— 那时候看的人要做的第一件事不是复核结果，
 *  而是复核这两个数字谁对，可信侧栏就此作废。
 *
 *  ## 它是通过率，不是权重打分
 *  分数 = 通过项 / 适用项。每一项都是链路自己记下的事实，逐条判真假。
 *  不做"截断扣 25 分、重试扣 10 分"那种加权 —— 权重没有出处，说不清
 *  25 从哪来的数字摆在可信侧栏上，本身就是最不可信的那个。
 *
 *  ## 直查模式（mode 'sql'）判几项
 *  直查没有模型环节：不召回、不重试、超阈值走审批而不是自行收窄范围。
 *  那三项在直查上**不存在**，于是不列进分母 —— 缺席不等于通过。原来
 *  侧栏把缺省的 false 当成"判过且通过"，直查因此恒 100 分，那是虚高。
 *
 *  剩下三项（截断 / 脱敏降级 / 有结果行）判的是**执行侧**：这条 SQL 跑出来
 *  的东西是不是完整地、如实地还给了人。**SQL 问得对不对不在里面** ——
 *  直查的 SQL 是人自己写的，askdb 不为它的语义背书；agent 模式下这一维由
 *  召回与重试两项兜着，直查模式下由写 SQL 的人自己兜。这句话必须跟着分数
 *  一起出现，否则一个 100 会被读成"这个数是对的"。
 */

/** 一项检查。`why` 是这项不成立时要说给人听的那句话。 */
export type Check = { label: string; ok: boolean; why: string }

/** 判这一次结果要用到的事实。工作台从 AskResult 取，追踪页从审计流水
 *  与 /api/trace 取 —— 字段名两边不同（row_count / rows_returned），
 *  由各自的调用处对齐到这个形状，口径本身只认这一份。 */
export type ResultFacts = {
  /** 'sql' = 直查模式。取不到时按 agent 模式判（老记录没有 kind） */
  mode?: 'ask' | 'sql' | string | null
  rowCount: number
  truncated?: boolean | null
  attempts?: number | null
  maskDegraded?: boolean | null
  recallBlind?: boolean | null
  recallNote?: string | null
  scopeNarrowed?: boolean | null
  scopeNote?: string | null
}

/** 直查模式下不适用、因而不进分母的三项。 */
const AGENT_ONLY = new Set(['结果范围未被收窄', '一次生成成功', '召回不是盲选'])

/** 出结果之后的可信度检查。**全部来自链路自己记下的事实**，
 *  不问模型、不做二次判断 —— 让模型给自己的答案打分，打出来的是作文分。 */
export function resultChecks(f: ResultFacts): Check[] {
  const rows = f.rowCount ?? 0
  const all: Check[] = [
    { label: '结果范围未被收窄', ok: !f.scopeNarrowed,
      why: f.scopeNote || '原查询被扫描阈值拦下，这个数来自收窄范围后的查询，不是全量' },
    { label: '结果完整未截断', ok: !f.truncated,
      why: `结果被 R-13 截断，只看到前 ${rows} 行` },
    { label: '一次生成成功', ok: (f.attempts ?? 1) <= 1,
      why: `SQL 重试了 ${(f.attempts ?? 1) - 1} 次才跑通` },
    { label: '脱敏判定未降级', ok: !f.maskDegraded,
      why: '脱敏判定退化为整行按敏感处理，列的归属没解析出来' },
    { label: '召回不是盲选', ok: !f.recallBlind,
      why: f.recallNote || '这次召回是盲选，给模型的表不是按相关度选的' },
    { label: '有结果行', ok: rows > 0,
      why: '查询成功但一行都没返回，先确认过滤条件是不是过窄' },
  ]
  return f.mode === 'sql' ? all.filter(c => !AGENT_ONLY.has(c.label)) : all
}

/** 通过率。没有可判的项时给 null —— 0/0 既不是 0 分也不是 100 分。 */
export function scoreOf(checks: Check[]): number | null {
  if (!checks.length) return null
  return Math.round(checks.filter(c => c.ok).length / checks.length * 100)
}

/** 悬停要能说清扣在哪一项。只报一个分数而不说因为什么，
 *  与写死一个数没有区别。直查模式另附一句它不管什么。 */
export function scoreTitle(head: string, checks: Check[], mode?: string | null): string {
  const passed = checks.filter(c => c.ok).length
  const lines = [`${head} ${passed}/${checks.length}`,
    ...checks.map(c => `${c.ok ? '✓' : '✕'} ${c.label}${c.ok ? '' : `：${c.why}`}`)]
  if (mode === 'sql') lines.push('（直查只判执行侧：SQL 由你自己写，其语义正确性不在此列）')
  return lines.join('\n')
}
