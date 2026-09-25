import { useEffect, useMemo, useState } from 'react'
import {
  createSkill, fetchSkills, previewSkills, publishSkill, rollbackSkill, setSkillStatus, testSkill,
  type Me, type SkillManifest, type SkillResolutionPreview,
} from '../api'
import { writeGuard } from '../writeGuard'
import { PageHeader } from '../components/AppShell'

const STATUS: Record<string, string> = {
  draft: '草稿', shadow: '影子', published: '已发布', disabled: '已停用', revoked: '已撤销',
}

export function SkillsPage({ me, notify }: { me: Me | null; notify: (message: string) => void }) {
  const [items, setItems] = useState<SkillManifest[]>([])
  const [selected, setSelected] = useState<SkillManifest | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [query, setQuery] = useState('')
  const guard = writeGuard(me, '管理 Skill')

  const load = () => {
    setLoading(true)
    fetchSkills().then(data => {
      setItems(data.items)
      setSelected(current => data.items.find(item => item.id === current?.id && item.version === current.version)
        ?? data.items[0] ?? null)
      setError('')
    }).catch(e => setError(String((e as Error).message || e))).finally(() => setLoading(false))
  }
  useEffect(() => {
    let alive = true
    fetchSkills().then(data => {
      if (!alive) return
      setItems(data.items)
      setSelected(data.items[0] ?? null)
      setError('')
    }).catch(e => { if (alive) setError(String((e as Error).message || e)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return needle ? items.filter(item => `${item.id} ${item.owner} ${item.description || ''}`.toLowerCase().includes(needle)) : items
  }, [items, query])
  const counts = useMemo(() => Object.fromEntries(
    ['published', 'shadow', 'draft', 'disabled', 'revoked'].map(status =>
      [status, items.filter(item => item.status === status).length])), [items])

  const action = async (run: () => Promise<unknown>, success: string) => {
    setError('')
    try { await run(); notify(success); load() }
    catch (e) { setError(String((e as Error).message || e)) }
  }

  return (
    <div className="page skills-page">
      <PageHeader title="Skill 中心" description="管理 Agent 的方法包：先测试与影子验证，再固定版本发布。"
        action={<button className="primary" {...guard.props} onClick={() => setCreateOpen(true)}>新建 Skill</button>} />

      <div className="skill-overview">
        <div><span>全部版本</span><strong>{items.length}</strong><small>版本不可原地覆盖</small></div>
        <div><span>线上发布</span><strong>{counts.published || 0}</strong><small>Runtime 可绑定</small></div>
        <div><span>影子验证</span><strong>{counts.shadow || 0}</strong><small>不改变线上 Prompt</small></div>
        <div><span>草稿</span><strong>{counts.draft || 0}</strong><small>等待测试门禁</small></div>
      </div>

      {error && <div className="audit-error">{error}</div>}
      <div className="skills-layout">
        <section className="skill-catalog">
          <div className="skill-toolbar">
            <div><b>能力包版本</b><span>{loading ? '读取中…' : `${visible.length} / ${items.length}`}</span></div>
            <input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索 id、负责人或说明" />
          </div>
          <div className="skill-table-wrap"><table className="skill-table">
            <thead><tr><th>Skill</th><th>类型</th><th>Agent</th><th>负责人</th><th>状态</th></tr></thead>
            <tbody>{visible.map(item => (
              <tr key={`${item.id}@${item.version}`} className={selected?.id === item.id && selected.version === item.version ? 'active' : ''}
                  onClick={() => setSelected(item)}>
                <td><strong>{item.id}</strong><code>@{item.version}</code></td>
                <td>{item.kind}</td><td>{item.agent_roles.join(' · ')}</td><td>{item.owner}</td>
                <td><span className={`skill-status status-${item.status}`}>{STATUS[item.status]}</span></td>
              </tr>
            ))}</tbody>
          </table></div>
          {!loading && !visible.length && <div className="skill-empty">还没有匹配的 Skill 版本。</div>}
        </section>

        <aside className="skill-detail">
          {selected ? <>
            <div className="skill-detail-head"><span>{selected.kind.toUpperCase()}</span><h2>{selected.id}</h2><code>@{selected.version}</code></div>
            <p>{selected.description || '未填写说明。方法、约束与适用范围以 Manifest 为准。'}</p>
            <dl>
              <div><dt>负责人</dt><dd>{selected.owner}</dd></div>
              <div><dt>适用 Agent</dt><dd>{selected.agent_roles.join('、')}</dd></div>
              <div><dt>数据源范围</dt><dd>{selected.source_scopes.join('、')}</dd></div>
              <div><dt>触发词</dt><dd>{selected.triggers.terms?.join('、') || '默认装载'}</dd></div>
              <div><dt>依赖</dt><dd>{selected.requires.join('、') || '无'}</dd></div>
              <div><dt>冲突</dt><dd>{selected.conflicts_with.join('、') || '无'}</dd></div>
              <div><dt>请求 Tool</dt><dd>{selected.requested_tools.join('、') || '无'}</dd></div>
            </dl>
            <section className="skill-instructions"><b>Instructions</b>
              <ol>{selected.instructions.map((line, i) => <li key={i}>{line}</li>)}</ol>
            </section>
            <div className="skill-checksum"><span>内容校验和</span><code>{selected.checksum}</code></div>
            <div className="skill-actions">
              <button className="secondary" {...guard.props}
                onClick={() => action(() => testSkill(selected.id, selected.version), 'Skill 测试通过')}>运行测试</button>
              {selected.status === 'draft' && <button className="primary" {...guard.props}
                onClick={() => action(() => publishSkill(selected.id, selected.version), 'Skill 已发布')}>发布版本</button>}
              {selected.status === 'published' && <button className="secondary danger" {...guard.props}
                onClick={() => action(() => setSkillStatus(selected.id, selected.version, 'disabled'), 'Skill 已停用')}>停用</button>}
              {selected.status === 'disabled' && <button className="secondary" {...guard.props}
                onClick={() => action(() => setSkillStatus(selected.id, selected.version, 'shadow'), 'Skill 已进入影子验证')}>转影子</button>}
              {selected.status === 'disabled' && <button className="secondary" {...guard.props}
                onClick={() => action(() => rollbackSkill(selected.id, selected.version), `已回滚到 ${selected.version}`)}>回滚到此版本</button>}
            </div>
          </> : <div className="skill-empty">选择一个 Skill 查看 Manifest。</div>}
        </aside>
      </div>

      <ResolutionLab guard={guard} />
      {createOpen && <CreateSkill onClose={() => setCreateOpen(false)} onCreated={() => {
        setCreateOpen(false); notify('Skill 草稿已创建'); load()
      }} />}
    </div>
  )
}

function ResolutionLab({ guard }: { guard: ReturnType<typeof writeGuard> }) {
  const [question, setQuestion] = useState('分析本月订单环比')
  const [role, setRole] = useState('query_worker')
  const [report, setReport] = useState<SkillResolutionPreview | null>(null)
  const [error, setError] = useState('')
  const run = async () => {
    try {
      setReport(await previewSkills({ agent_role: role, question, source_id: 'builtin',
        runtime_allowed_tools: ['search_schema', 'get_table_schema', 'execute_sql'] }))
      setError('')
    } catch (e) { setError(String((e as Error).message || e)) }
  }
  return <section className="resolution-lab">
    <div><span>RESOLUTION LAB</span><h2>解析预览</h2><p>输入本次 Agent 上下文，查看会固定哪些 Skill、为什么选择，以及最终 Tool 交集。</p></div>
    <div className="resolution-form"><select value={role} onChange={e => setRole(e.target.value)}>
      <option value="semantic">Semantic</option><option value="query_worker">Query Worker</option>
      <option value="verifier">Verifier</option><option value="synthesizer">Synthesizer</option>
    </select><input value={question} onChange={e => setQuestion(e.target.value)} />
      <button className="secondary" {...guard.props} onClick={run}>预览绑定</button></div>
    {error && <p className="resolution-error">{error}</p>}
    {report && <div className="resolution-output">
      {report.bindings.length ? report.bindings.map(item => <span key={`${item.skill_id}@${item.version}`}><b>{item.skill_id}</b>@{item.version}<small>{item.selection_reason}</small></span>)
        : <em>没有匹配的已发布 Skill</em>}
      <p>Effective Tools：{report.effective_tools.join('、') || '无（Skill 不授予权限）'}</p>
    </div>}
  </section>
}

function CreateSkill({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({ id: '', version: '1.0.0', owner: '', kind: 'domain',
    roles: 'semantic,query_worker,verifier', scopes: '*', term: '', instructions: '' })
  const [error, setError] = useState('')
  const create = async () => {
    try {
      await createSkill({
        id: form.id, version: form.version, owner: form.owner, kind: form.kind as SkillManifest['kind'],
        agent_roles: form.roles.split(',').map(s => s.trim()).filter(Boolean),
        source_scopes: form.scopes.split(',').map(s => s.trim()).filter(Boolean),
        triggers: { terms: form.term ? [form.term] : [] },
        instructions: form.instructions.split('\n').map(s => s.trim()).filter(Boolean),
        requested_tools: [], requires: [], conflicts_with: [], priority: 100,
        constraints: {}, examples: [],
        tests: [{ name: '基础触发', question: form.term || '通用数据问题', should_match: true }],
      })
      onCreated()
    } catch (e) { setError(String((e as Error).message || e)) }
  }
  return <div className="skill-modal-backdrop" onMouseDown={onClose}><div className="skill-modal" onMouseDown={e => e.stopPropagation()}>
    <div className="skill-modal-head"><div><span>NEW VERSIONED PACKAGE</span><h2>创建 Skill 草稿</h2></div><button onClick={onClose}>×</button></div>
    <div className="skill-form-grid">
      <label><span>Skill ID</span><input value={form.id} placeholder="orders.month_over_month" onChange={e => setForm({...form, id:e.target.value})} /></label>
      <label><span>版本</span><input value={form.version} onChange={e => setForm({...form, version:e.target.value})} /></label>
      <label><span>负责人</span><input value={form.owner} placeholder="data-commerce" onChange={e => setForm({...form, owner:e.target.value})} /></label>
      <label><span>类型</span><select value={form.kind} onChange={e => setForm({...form, kind:e.target.value})}>
        <option value="domain">领域语义</option><option value="source">数据源</option><option value="analysis">分析模式</option>
        <option value="verification">验证</option><option value="presentation">表达</option><option value="general">通用</option>
      </select></label>
      <label><span>适用 Agent（逗号）</span><input value={form.roles} onChange={e => setForm({...form, roles:e.target.value})} /></label>
      <label><span>Source Scope（逗号）</span><input value={form.scopes} onChange={e => setForm({...form, scopes:e.target.value})} /></label>
      <label className="span-2"><span>触发词</span><input value={form.term} placeholder="环比" onChange={e => setForm({...form, term:e.target.value})} /></label>
      <label className="span-2"><span>Instructions（每行一条）</span><textarea value={form.instructions} onChange={e => setForm({...form, instructions:e.target.value})} /></label>
    </div>
    {error && <div className="audit-error">{error}</div>}
    <div className="skill-modal-foot"><button className="secondary" onClick={onClose}>取消</button><button className="primary" disabled={!form.id || !form.owner || !form.instructions} onClick={create}>保存草稿</button></div>
  </div></div>
}
