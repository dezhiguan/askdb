import type { AskResult, Health, Me } from '../api'
import type { ResultTab, View } from '../types'
import type { HealthState } from '../useHealth'
import type { Check } from '../trust'
import { resultChecks, scoreOf, scoreTitle } from '../trust'
import { roleLabel } from '../roles'
import { useSqlDigest } from './ResultTabs'

/** 右栏三块：能不能执行、按什么策略执行、执行完拿什么复核。
 *
 *  版式、标签与文案一律照原型（trusted-data-agent-prototype.html 第 2196-2216 行）。
 *  格子里的值一律取真实数据 —— 原型上的 90 DAYS / 15 SEC 是稿上的示意值，
 *  照抄等于在可信侧栏上宣称一条并未执行的策略。
 *
 *  评分环原来固定显示 96（原型展示值），2026-09-07 换成真值。**它是一个通过率，
 *  不是一个权重打分**：每一项都是链路自己记下的事实，逐条判真假，分数 =
 *  通过项 / 总项。这样定的理由是，可信度一旦变成"截断扣 25 分、重试扣 10 分"
 *  那种加权公式，权重就没有出处 —— 说不清 25 从哪来的数字，摆在"可信"侧栏上
 *  本身就是最不可信的那个。通过率至少每一项都能指着说"这条成立/不成立"。
 *
 *  两态，与卡片标题一起切：
 *    · 还没结果 → **安全准入**：数据源、模型、三条护栏阈值、配额，6 项
 *    · 有结果且执行成功 → **本次结果可信度**：截断、重试、脱敏降级、召回盲选、
 *      有无结果行，5 项
 *  执行被拒时不切 —— 那次根本没跑出结果，给它算一个"结果可信度"是无中生有。
 */
export function TrustSidebar({ health, source, result, mode, me, onResultTab, onNavigate }: {
  health: HealthState
  /** 工作台当前选中的数据源。切源时「本次执行策略」要跟着变 —— 护栏与
   *  租户是实例级、切源不变（原型亦如此），真正随源变的只有数据源身份这一项。 */
  /** 工作台的 SourceOption：`tables` 是这个源开放给模型的表数（内置源为配置里那份） */
  source?: { id?: string; name: string; dialect: string; env?: string; tables?: number }
  result: AskResult | null
  /** 工作台当前在哪个模式。直查模式少判三项（见 trust.ts）—— 原来一律按
   *  agent 模式判，缺省的 false 被当成"判过且通过"，直查恒 100 分。 */
  mode?: 'ask' | 'sql'
  /** 「身份」一格要的当前登录态 */
  me?: Me | null
  onResultTab: (tab: ResultTab) => void
  /** 第二个参数是要定位的 trace_id */
  onNavigate: (view: View, focusTrace?: string) => void
}) {
  const ready = health.status === 'ready' ? health.health : null
  const canExecute = !!ready?.datasource.ok
  const canAsk = canExecute && !!ready?.llm.ok
  // 与结论卡上的摘要同源同算法：两处显示同一条 SQL 的哈希，不能各算各的
  const hash = useSqlDigest(result?.sql_final || result?.sql_raw || '')

  // 数据期限。运行时源上时间窗口无法落地（sources.derive_config 显式关闭），
  // 而 me.scope 算的是内置源 —— 选了运行时源就不能拿它去宣称一条窗口。
  const windowDays = source?.id ? null : (me?.scope.max_age_days ?? null)
  const windowLabel = windowDays == null ? '不限' : `最近 ${windowDays} DAYS`

  const admission = !ready ? { label: '读取中', tone: 'wait' }
    : !ready.datasource.ok ? { label: '数据源不可用', tone: 'bad' }
    : !ready.llm.ok ? { label: '仅直查 SQL', tone: 'wait' }
    : { label: 'READY', tone: '' }

  // 开放给模型的表数。工作台的 SourceOption 已经算好了（运行时源取
  // table_count，内置源取 schema 表数），直接用；取不到时退回 me.scope，
  // 再取不到就给 null —— 这一项直接不进准入清单，不拿一个猜的数去判真假。
  const whitelist = source?.tables ?? (source?.id ? null : me?.scope.tables.length ?? null)

  // 有结果且真的执行成功了才切到「本次结果」；被护栏拒掉的那次没有结果可评。
  // 判据走 trust.ts —— 执行追踪页那枚角标调的是同一份，两处不能各算各的
  const scored = result?.ok
    ? resultChecks({
        mode, rowCount: result.row_count ?? 0, truncated: result.truncated,
        attempts: result.attempts, maskDegraded: result.mask_degraded,
        recallBlind: result.recall_blind, recallNote: result.recall_note,
        scopeNarrowed: result.scope_narrowed, scopeNote: result.scope_note,
        hedgeTerms: result.hedge_terms, derivedColumns: result.derived_columns,
        anaphoric: result.anaphoric,
      })
    : admissionChecks(ready, !!source, whitelist)
  const score = scoreOf(scored)
  const failed = scored.filter(c => !c.ok)
  const ringTitle = !scored.length ? '读取中'
    : scoreTitle(result?.ok ? '本次结果可信度' : '安全准入', scored,
                 result?.ok ? mode : undefined)

  return (
    // side-stack 是原型的类名；trust-sidebar 保留，窄屏断点按它排版
    <aside className="trust-sidebar side-stack">
      <div className="assurance-card">
        <div className="assurance-hero">
          {/* 环上的弧长跟着分数走。弧是假的而数字是真的，等于换了个地方写死 */}
          <div className={`score-ring ${score != null && score < 100 ? 'partial' : ''}`}
               style={{ ['--pct' as string]: score ?? 0 }} title={ringTitle}>
            <span>{score ?? '—'}</span>
          </div>
          <div className="assurance-hero-copy">
            <strong>
              {result?.ok ? (mode === 'sql' ? '本次执行可信度' : '本次结果可信度')
                : canAsk ? '安全准入已通过'
                : canExecute ? '只读执行可用' : '暂不可执行'}
            </strong>
            <small title={ringTitle}>
              {/* 失败项直接写在脸上。可信度掉了却要人去别处找原因，
                  等于把一个数字换成了另一个说不清的数字 */}
              {result?.ok
                ? (failed.length ? failed.map(c => c.why).join(' · ')
                   : mode === 'sql'
                     ? `${scored.length} 项执行侧检查全部通过 · SQL 语义由你自己复核`
                     : `${scored.length} 项检查全部通过`)
                : canAsk ? (failed.length ? failed.map(c => c.why).join(' · ')
                            : '当前身份可在只读边界内执行查询')
                : canExecute ? '未配模型密钥，自然语言提问不可用，直查 SQL 仍可用'
                : ready?.datasource.hint || '数据源连接不可用'}
            </small>
          </div>
          <span className={`ready-badge ${admission.tone}`}>{admission.label}</span>
        </div>
        {/* 三格标签照原型：身份 / 数据库角色 / 数据保护 */}
        <div className="assurance-grid">
          <div className="assurance-item" title={me?.username ? `可见表 ${me.scope.tables.length} 张 · 行上限 ${me.scope.max_rows}` : '未登录，按匿名可见范围执行'}>
            <span>身份</span><strong>{identityLabel(me)}</strong>
          </div>
          {/* askdb 的每一条连接都是只读（duckdb read_only / postgres 只读事务）；
              运行时源声明了环境就报环境 */}
          <div className="assurance-item">
            <span>数据库角色</span><strong>{dbRoleLabel(source?.env)}</strong>
          </div>
          <div className="assurance-item" title={ready?.tenant.enabled ? `租户隔离 ${ready.tenant.mode} · ${ready.tenant.column}=${ready.tenant.org_id}` : '单租户实例'}>
            <span>数据保护</span>
            <strong>{ready ? (ready.tenant.enabled ? 'RLS · AUDIT' : 'AUDIT') : '—'}</strong>
          </div>
        </div>
      </div>

      <div className="side-section-card">
        <div className="side-section-head">
          <div><strong>本次执行策略</strong><small>SQL 执行前强制应用</small></div>
          <button onClick={() => onNavigate('permissions')}>查看策略 →</button>
        </div>
        <div className="policy-rail">
          <div className="policy-chip">
            <span>数据源 / 环境</span>
            <strong>{source ? `${source.name} · 只读` : (ready?.datasource.detail ?? '—')}</strong>
          </div>
          {/* 数据期限取真值，口径与权限页 ageLabel 逐字一致（未收窄即「不限」）——
              同一件事在两页显示成两个样子，比显示得不好看更糟 */}
          <div className="policy-chip" title="护栏 R-19 按此注入时间窗口谓词">
            <span>允许数据范围</span><strong>{windowLabel}</strong>
          </div>
          <div className="policy-chip">
            <span>返回上限</span><strong>{ready ? `${ready.guard.max_rows} ROWS` : '—'}</strong>
          </div>
          {/* 原型用 SEC，不用 MS */}
          <div className="policy-chip">
            <span>执行超时</span><strong>{ready ? `${Math.round(ready.guard.timeout_ms / 1000)} SEC` : '—'}</strong>
          </div>
        </div>
      </div>

      <div className="side-section-card">
        <div className="side-section-head">
          <div><strong>可验证输出</strong><small>查询完成后生成复核证据</small></div>
          <span className={`status ${result ? '' : 'wait'}`}>{result ? '已生成' : '等待执行'}</span>
        </div>
        <div className="evidence-actions">
          <button className="evidence-action" disabled={!result} onClick={() => onResultTab('sql')}>
            <i>SQL</i><span><strong>原生 SQL</strong><small>查看、复制并在客户端复核</small></span><b>→</b>
          </button>
          {/* 这一格给的是「模型、工具和策略节点」，那是执行追踪页的内容，
              页内的执行链路页签只有节点概览 —— 所以直接送到那一页去，
              并带上本次的 trace_id 定位到这条查询而不是最近一条。 */}
          <button className="evidence-action open-trace" disabled={!result}
                  onClick={() => onNavigate('traces', result?.trace_id || undefined)}>
            <i>TR</i><span><strong>Agent 执行链路</strong><small>查看模型、工具和策略节点</small></span><b>→</b>
          </button>
        </div>
        <div className="evidence-footer">
          <div><span>QUERY ID</span><code>{result?.trace_id || '尚未生成'}</code></div>
          <div>
            <span>SQL SHA-256</span>
            <code>{!result ? '尚未生成' : hash ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : '—'}</code>
          </div>
          {/* 原型只有上面两格。耗时与成本是这套实现真实产出的账，本页没有第二处
              能看到它 —— 补成通栏第三格，不占原型两格的版位。 */}
          <div className="evidence-footer-wide">
            <span>耗时 / 成本</span>
            <code>{result ? `${result.elapsed_ms ?? 0}ms · ¥${(result.cost_cny ?? 0).toFixed(4)}` : '尚未生成'}</code>
          </div>
        </div>
      </div>

      <button className="audit-link side-audit-link" onClick={() => onNavigate('audit')}>
        <span>历史查询与审计记录</span><span>打开审计中心 →</span>
      </button>
    </aside>
  )
}

/** 提问前的准入检查。**只取实例级、与选哪个源无关的事实** ——
 *  表白名单是按源走的（运行时源各有各的白名单），这里拿不到，
 *  与其报一个可能不对的数，不如不列这一项。 */
function admissionChecks(ready: Health | null, hasSource: boolean,
                         whitelist: number | null): Check[] {
  if (!ready) return []
  const g = ready.guard
  return [
    // 没有内置源的实例上 health.datasource.ok 恒为真（它报的是"配置里那条"，
    // 而配置里压根没有），拿它当"连得上"会让这一项永远白送分。
    // 那种实例上真正的条件是**当前选没选到一个运行时源**。
    { label: '数据源可用', ok: ready.datasource.configured ? ready.datasource.ok : hasSource,
      why: ready.datasource.configured
        ? (ready.datasource.hint || '数据源连接不可用')
        : '还没选数据源，到「数据源」页选一个再提问' },
    { label: '模型可用', ok: ready.llm.ok,
      why: '未配模型密钥，只能直查 SQL' },
    { label: '返回行上限已设 · R-13', ok: g.max_rows > 0,
      why: '没有返回行上限，一次查询可能拉回整表' },
    { label: '扫描行上限已设 · R-11', ok: g.max_scan_rows > 0,
      why: '没有扫描行上限，全表扫描不会被拦' },
    { label: '语句超时已设 · R-12', ok: g.timeout_ms > 0,
      why: '没有语句超时，慢查询会一直占着连接' },
    { label: '每日配额已设', ok: g.daily_quota > 0,
      why: '没有每日配额，模型开销没有上限' },
    // 白名单张数取不到时整项不列 —— 少一项分母，不假装判过
    ...(whitelist == null ? [] : [{
      label: '表白名单已生效', ok: whitelist > 0,
      why: '这个源一张表都没开放，模型什么也看不见 —— 到「数据源」页勾选要开放的表',
    }]),
  ]
}

/** 「身份」一格。原型写死 `SSO · PRODUCT`；这里报真实登录态与角色。 */
function identityLabel(me?: Me | null): string {
  if (!me) return '—'
  if (!me.username) return '匿名 · GUEST'
  // 与顶栏那枚身份角标同一套显示名（roles.ts），两处不能一个中文一个英文码
  const role = roleLabel(me.roles[0] ?? '') || '无角色'
  return `${me.display_name || me.username} · ${role}`
}

/** 「数据库角色」一格。askdb 的连接一律只读，运行时源声明了环境就报环境。 */
function dbRoleLabel(env?: string): string {
  if (env === 'prod_ro') return 'PROD-RO'
  if (env === 'test') return 'TEST-RO'
  return 'READ-ONLY'
}
