import { PageHeader } from '../components/AppShell'
import { useEffect, useState } from 'react'
import {
  deleteSource, fetchIntrospect, fetchSelfCheck, fetchSources, RateLimited, scanSource,
  type Introspect, type Me, type Probe, type SelfCheck, type SourceCard, type SourceList,
} from '../api'
import { AddSourceModal, ScanTablesModal } from '../components/AddSourceModal'
import type { HealthState } from '../useHealth'
import { useCountdown } from '../useCountdown'

/** 数据源类型的短标。图标位 34px，放不下全名。 */
const TYPE_MARK: Record<string, string> = { duckdb: 'DK', postgresql: 'PG' }

/** 副标题里的引擎名。卡片副标题是「引擎 · host:port」，引擎位要给人看的写法。 */
const TYPE_NAME: Record<string, string> = { duckdb: 'DuckDB', postgresql: 'PostgreSQL' }

/** 一张运行时数据源卡的连接检查结果。状态灯、延迟、库内表数、最后检查
 *  四处都靠它 —— 本次会话刚测过就用本地这份，否则退回后端落盘的上一次。 */
type CardProbe = { ok: boolean; latency: number | null; visible: number | null; at: Date }

/** 后端落盘的上一次检查。从没检查过返回 null —— 这时候该显示「未检查」，
 *  不是假设它是好的。 */
function lastProbe(card: SourceCard): CardProbe | null {
  if (!card.last_checked_at || card.last_ok == null) return null
  return {
    ok: card.last_ok,
    latency: card.last_latency_ms,
    visible: card.last_visible_count,
    at: new Date(card.last_checked_at),
  }
}

export function DataSourcesPage({ health, me }: { health: HealthState; me: Me | null }) {
  // 写操作要不要置灰，只看一件事：登录没有。**这只是少让人白点一次** ——
  // 真正的边界在服务端那道写入中间件上，置灰拦不住任何人。
  // 判据别在这里"推"：me 是后端给的，前端只读不算。
  const canWrite = !!me?.username
  const writeHint = canWrite ? undefined : '这个操作会改动配置，需要登录后才能执行；未登录只能只读查询'
  const [introspect, setIntrospect] = useState<Introspect | null>(null)
  const [check, setCheck] = useState<SelfCheck | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [manage, setManage] = useState<SourceCard | null>(null)
  const [sources, setSources] = useState<SourceList | null>(null)
  const [reload, setReload] = useState(0)
  const [probes, setProbes] = useState<Record<string, CardProbe>>({})
  // 完整扫描结果，按数据源 id 存。卡片上的「测试连接」和配置弹窗打的是同一个
  // 接口 —— 共用一份缓存，点过一次就不必为开弹窗再连一次库。
  const [scans, setScans] = useState<Record<string, Probe>>({})
  const [testingId, setTestingId] = useState('')
  // 被限流时的解禁时刻。限流是进程内按调用方计的，一处撞上，
  // 这一页所有会发起出站建连的按钮都该一起等
  const [coolUntil, setCoolUntil] = useState(0)
  const cooldown = useCountdown(coolUntil)
  // 「刚刚」不会自己变成「1m ago」：这一页没有别的东西在跳，不给它一个心跳，
  // 页面停着不动时相对时间就永远停在写下它的那一刻
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    let alive = true
    fetchIntrospect()
      .then(value => { if (alive) setIntrospect(value) })
      .catch(e => { if (alive) setError(`读取数据源信息失败：${String(e.message || e)}`) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    let alive = true
    fetchSources()
      .then(value => {
        if (!alive) return
        setSources(value)
        // 配置指定了默认源却取不到（名字写错、源已被删）：服务端不会替它挑一个
        // 顶上，界面就只是"停在了另一个库上"—— 从卡片上看不出是配置的问题，
        // 而这一页正是改配置的人会来的地方
        if (value.default_source_error) setError(value.default_source_error)
      })
      .catch(e => { if (alive) setError(`读取数据源信息失败：${String(e.message || e)}`) })
    return () => { alive = false }
  }, [reload])

  const removeBuiltin = async () => {
    // 这一条删的是配置文件里的 datasource 段，后果和删一条运行时源完全不同：
    // 删完之后不带数据源的查询会被直接拒绝。确认文案必须把这句说出来。
    if (!window.confirm(
      '删除默认数据源？\n\n'
      + `会从 ${ready?.config ?? '配置文件'} 里移除 datasource 段。`
      + '此后不指定数据源的查询将被拒绝，需要在本页选择一个已添加的数据源。\n\n'
      + '配置文件里的其他段（护栏、租户策略、业务口径）不受影响。'
    )) return
    try {
      await deleteSource('builtin')
      setReload(n => n + 1)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  const removeSource = async (card: SourceCard) => {
    if (!window.confirm(`删除数据源「${card.name}」？白名单一并删除，历史审计记录不受影响。`)) return
    try {
      await deleteSource(card.id)
      setReload(n => n + 1)
    } catch (e) {
      setError(String((e as Error).message || e))
    }
  }

  // 运行时数据源的「测试连接」。真去建连并读一次元数据。结果服务端也会落盘，
  // 所以刷新页面不丢；这里额外存一份是为了不等列表重取就先更新卡片。
  const testCard = async (card: SourceCard) => {
    setTestingId(card.id)
    try {
      const result = await scanSource(card.id)
      setScans(current => ({ ...current, [card.id]: result }))
      setProbes(current => ({
        ...current,
        [card.id]: {
          ok: result.ok,
          latency: result.latency_ms,
          visible: result.visible_count,
          // 服务端记的时刻优先 —— 「最后检查」问的是服务端什么时候连的库，
          // 不是浏览器什么时候收到的响应
          at: result.checked_at ? new Date(result.checked_at) : new Date(),
        },
      }))
      setError(result.ok ? '' : (result.error
        ? `${result.error}${result.hint ? `｜${result.hint}` : ''}`
        : `数据源「${card.name}」连接自检未通过`))
    } catch (e) {
      if (e instanceof RateLimited) setCoolUntil(Date.now() + e.retryAfter * 1000)
      setError(String((e as Error).message || e))
    } finally {
      setTestingId('')
    }
  }

  const runCheck = async () => {
    setChecking(true)
    try {
      setCheck(await fetchSelfCheck())
      setCheckedAt(new Date())
      setError('')
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setChecking(false)
    }
  }

  const ready = health.status === 'ready' ? health.health : null
  const ds = ready?.datasource

  // 内置卡的去留以 /api/sources 为准，不看 health —— health 只在页面加载时取一次，
  // 删完之后它还会说"有默认源"，卡片就会赖在那儿不走。
  const builtinCard = sources?.items.find(item => item.builtin) ?? null

  /* 卡片切页。分页条与审计中心、任务中心、身份与权限同一套结构与类名，
     四页的操作手感必须一致。

     **切在前端，不走服务端**：/api/sources 本来就一次把所有源给全（它不连库，
     只读注册表），源的数量是运维手动加出来的、量级在几十，为它加一套
     LIMIT/OFFSET 只会多一条要对齐的口径。这与审计/成员表不同 ——
     那两处的数据是无上限增长的，不在服务端切就得把整库拉进浏览器。

     内置卡算作列表里的第一张，跟着一起翻页：它和运行时源在这一页上是同一种
     东西（同一个网格、同样的操作），钉在每一页顶上会让"每页 10 条"变成 11 张。 */
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const allCards = sources?.items ?? []
  const total = allCards.length
  const pages = Math.max(Math.ceil(total / pageSize), 1)
  // 删到页数变少时不要卡在一个空页上（停在第 3 页而只剩 2 页 = 一片空白）
  const current = Math.min(page, pages)
  const pageCards = allCards.slice((current - 1) * pageSize, current * pageSize)
  // 还没读到列表时先按"有内置源"画骨架卡，与原来一致；读到之后由这一页的
  // 切片说了算
  const showBuiltin = sources ? pageCards.some(item => item.builtin) : true
  const runtimeCards = pageCards.filter(item => !item.builtin)

  return (
    <div className="page">
      {/* 通用 PageHeader 没有 eyebrow 位，这里按原型直接写出 .page-head */}
      <PageHeader
        title="数据源管理"
        description="只连接测试库和生产只读镜像，凭证由服务端托管，不下发浏览器也不进入提示词。"
        action={
          <button
            className="primary"
            disabled={!sources?.can_add || !canWrite}
            title={sources && !sources.can_add
              ? '本实例未开启运行时添加数据源：服务端会按填入的地址主动建连，对外实例一律关闭'
              : writeHint}
            onClick={() => setShowAdd(true)}
          >＋ 添加数据源</button>
        }
      />

      {error && (
        <div className="audit-error">
          {error}
          {cooldown > 0 && <>（还需等待 {cooldown} 秒）</>}
        </div>
      )}

      {/* 配置里的默认数据源最多一个，所以内置卡最多一张；它可以被删掉，
          删掉之后本页只剩运行时数据源 */}
      <div className="source-grid">
        {showBuiltin && (
        <article className="source-card">
          <div className="source-top">
            <i className="db-icon">
              {ds ? (TYPE_MARK[ds.type] ?? ds.type.slice(0, 2).toUpperCase()) : '··'}
            </i>
            {ds
              ? <span className={`card-status ${ds.ok ? '' : 'bad'}`}>
                  {ds.ok ? <><i className="online" /> 正常</> : '● 不可用'}
                </span>
              : <span className="card-status off">● 读取中</span>}
          </div>
          <h3>{builtinCard?.name || '默认数据源'}</h3>
          {/* 原型的副标题是「引擎 · host:port」。配置文件路径挪进 title —— 它是运维信息，
              占掉副标题会把「这条连的是哪个库」挤没 */}
          <p title={ready?.config ? `配置文件：${ready.config}` : undefined}>
            {ds ? (TYPE_NAME[ds.type] ?? ds.type) : '—'} · {builtinCard?.host || ds?.detail || '—'}
            {ds && !ds.ok && ds.hint && <><br />{ds.hint}</>}
          </p>
          <div className="mini-metrics">
            <div className="mini-metric">
              <span>延迟</span>
              <strong>{check?.latency_ms == null ? '—' : `${check.latency_ms}ms`}</strong>
            </div>
            {/* 这里的 allowed_count 是「实时查库 ∩ 白名单」，和运行时卡上的
                「授权表」同义，所以用同一个名字与同一种写法：授权 / 库内 */}
            <div className="mini-metric" title={introspect
              ? `白名单 ${introspect.allowed_count} 张 · 库内可见 ${introspect.total} 张`
              : undefined}>
              <span>授权表</span>
              <strong>
                {introspect?.allowed_count ?? '—'}
                {introspect?.total != null && <span className="metric-of">/ {introspect.total}</span>}
              </strong>
            </div>
            <div className="mini-metric">
              <span>凭证</span>
              {/* 写死一个 VAULT 是假的。askdb 的真实答案只有两种：
                  口令来自某个环境变量，或者这个库根本不需要口令。 */}
              <strong>{ds ? (ds.credential ? `ENV · ${ds.credential}` : '无需口令') : '—'}</strong>
            </div>
            <div className="mini-metric">
              <span>最后检查</span>
              <strong>{checkedAt ? relative(checkedAt, now) : 'NEVER'}</strong>
            </div>
          </div>
          <div className="source-actions">
            <button className="secondary" onClick={runCheck} disabled={checking}>
              {checking ? '连接检查中…' : '测试连接'}
            </button>
            <button
              className="ghost"
              disabled={!builtinCard?.deletable}
              title={builtinCard?.deletable ? undefined
                : '内置数据源来自配置文件，页面上删不了 —— 容器里的配置随镜像发布，'
                  + '改了下次发版就会回滚。要撤掉它，删配置里的 datasource: 段并重新发布'}
              onClick={removeBuiltin}
            >删除</button>
          </div>
        </article>
        )}

        {runtimeCards.map(card => {
          // 一张表都没开放 = 原型里的「待配置」：连上了但模型什么也看不见，
          // 这时候该催的是去勾表，不是去测连接
          const pending = card.table_count === 0
          const probe = probes[card.id] ?? lastProbe(card)
          // 绿灯的判据是**最近一次检查通过**。原先判的是 table_count !== 0，
          // 那问的是"勾没勾表"，和库通不通毫无关系 —— 库挂了它照样绿。
          // 没检查过就如实说没检查过，不拿绿灯替它担保。
          // 连不上排在「待配置」前面：一个既没勾表又连不上的源，先要解决的
          // 是连不上；显示成「待配置」会把人支去勾表，而那一步根本进行不下去
          const statusClass = probe && !probe.ok ? 'bad' : pending ? 'off' : probe ? '' : 'idle'
          // 白名单里有表在库里已经不见了：库改了结构而白名单没跟上，
          // 这时候查询会撞在自检的「授权表集合」上，得在卡片上先看得见
          const drift = probe?.visible != null && probe.visible < card.table_count
          return (
            <article className="source-card" key={card.id}>
              <div className="source-top">
                <i className="db-icon">{TYPE_MARK[card.type] ?? card.type.slice(0, 2).toUpperCase()}</i>
                <span className={`card-status ${statusClass}`}>
                  {probe && !probe.ok ? '● 不可用'
                    : pending ? '● 待配置'
                    : probe ? <><i className="online" /> 正常</>
                    : '● 未检查'}
                </span>
              </div>
              <h3>{card.name}</h3>
              <p>{TYPE_NAME[card.type] ?? card.type} · {card.host || '—'} · {ENV_LABEL[card.env] ?? card.env}</p>
              <div className="mini-metrics">
                <div className="mini-metric" title="建连耗时（握手 + 认证），来自最近一次连接检查">
                  <span>延迟</span>
                  <strong>{probe?.latency == null ? '—' : `${probe.latency}ms`}</strong>
                </div>
                {/* 这一格是**授权表**，不是"库里有多少张表"。前者是白名单
                    快照，改白名单才会变；后者要连库才知道，所以只有检查过
                    才有值。两个数并排给，才看得出库改了而白名单没跟上。 */}
                <div className="mini-metric" title={probe?.visible == null
                  ? '开放给模型的表数（白名单）。点「测试连接」可核实库里此刻实际有多少张'
                  : `白名单 ${card.table_count} 张 · 库内可见 ${probe.visible} 张`}>
                  <span>授权表</span>
                  <strong className={drift ? 'metric-drift' : undefined}>
                    {card.table_count}
                    {probe?.visible != null && <span className="metric-of">/ {probe.visible}</span>}
                  </strong>
                </div>
                {/* 写死一个 VAULT 是假的。askdb 的真实答案只有两种：
                    口令来自某个环境变量，或者 askdb 这边没存任何凭证。 */}
                <div className="mini-metric" title={card.credential
                  ? '口令从这个环境变量读取，不落盘、不下发浏览器'
                  : 'askdb 没有为这个源保存任何凭证。库本身要不要口令，取决于服务端连过去时的认证方式'}>
                  <span>凭证</span>
                  <strong>{card.credential || '无需口令'}</strong>
                </div>
                <div className="mini-metric" title={probe ? probe.at.toLocaleString() : undefined}>
                  <span>最后检查</span>
                  <strong>{probe ? relative(probe.at, now) : 'NEVER'}</strong>
                </div>
              </div>
              <div className="source-actions">
                {pending
                  ? (
                    <button className="primary" disabled={!canWrite} title={writeHint}
                      onClick={() => setManage(card)}>完成配置</button>
                  )
                  : (
                    <>
                      {/* 「测试连接」走 GET /scan，是读 —— 未登录照样能自检，不置灰 */}
                      <button className="secondary"
                        disabled={testingId === card.id || cooldown > 0}
                        title={cooldown > 0 ? '出站建连限流中，稍候会自动恢复' : undefined}
                        onClick={() => testCard(card)}>
                        {testingId === card.id ? '连接检查中…'
                          : cooldown > 0 ? `测试连接（${cooldown}s）` : '测试连接'}
                      </button>
                      <button className="ghost" disabled={!canWrite} title={writeHint}
                        onClick={() => setManage(card)}>配置</button>
                    </>
                  )}
                <button className="ghost" disabled={!canWrite} title={writeHint}
                  onClick={() => removeSource(card)}>删除</button>
              </div>
            </article>
          )
        })}
      </div>

      {total > 0 && (
        <div className="audit-pager">
          <span>共 {total} 个数据源 · 第 {current} / {pages} 页</span>
          <span>
            <select value={pageSize}
                    onChange={event => { setPageSize(Number(event.target.value)); setPage(1) }}>
              {[10, 20, 50].map(size => <option key={size} value={size}>每页 {size} 条</option>)}
            </select>
            <button className="ghost" disabled={current <= 1}
                    onClick={() => setPage(current - 1)}>‹ 上一页</button>
            <button className="ghost" disabled={current >= pages}
                    onClick={() => setPage(current + 1)}>下一页 ›</button>
          </span>
        </div>
      )}

      {showAdd && sources && (
        <AddSourceModal
          meta={sources}
          onClose={() => setShowAdd(false)}
          onDone={() => { setShowAdd(false); setReload(n => n + 1) }}
        />
      )}
      {manage && (
        <ScanTablesModal
          id={manage.id}
          name={manage.name}
          cached={scans[manage.id]}
          onScanned={probe => setScans(current => ({ ...current, [manage.id]: probe }))}
          onClose={() => setManage(null)}
          onDone={() => {
            // 白名单刚改过，缓存里那份的 allowed 标记已经不作数了
            setScans(({ [manage.id]: _stale, ...rest }) => rest)
            setManage(null)
            setReload(n => n + 1)
          }}
        />
      )}
    </div>
  )
}

const ENV_LABEL: Record<string, string> = { test: '测试环境', prod_ro: '生产只读', builtin: '内置' }

/** 相对时间。`now` 由调用方传入，好让心跳 state 能推着它自己往前走 ——
 *  读 Date.now() 的话渲染完就冻住，「刚刚」会一直是「刚刚」。 */
function relative(at: Date, now: number): string {
  const seconds = Math.floor((now - at.getTime()) / 1000)
  // 负数说明服务端与浏览器时钟有偏差，按「刚刚」算，不显示未来时间
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  if (seconds < 86400 * 7) return `${Math.floor(seconds / 86400)}d ago`
  // 过了一周，「Nd ago」已经不如直接给日期好读
  return at.toLocaleDateString()
}
