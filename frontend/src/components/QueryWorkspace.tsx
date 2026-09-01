import { useState } from 'react'
import { sources, suggestions } from '../data/mockData'
import type { DataSource, ResultTab, View } from '../types'
import { ResultTabs } from './ResultTabs'
import { TrustSidebar } from './TrustSidebar'

export function QueryWorkspace({ source, setSource, onNavigate, onClarify, notify }: {
  source: DataSource
  setSource: (source: DataSource) => void
  onNavigate: (view: View) => void
  onClarify: () => void
  notify: (message: string) => void
}) {
  const [question, setQuestion] = useState('')
  const [resultReady, setResultReady] = useState(false)
  const [resultTab, setResultTab] = useState<ResultTab>('result')
  const [sourceOpen, setSourceOpen] = useState(false)
  const [mode, setMode] = useState<'query' | 'sql'>('query')
  const [running, setRunning] = useState(false)

  const run = () => {
    if (!question.trim()) { notify('请先输入一个数据问题'); return }
    if (question.includes('退款金额') && !/(今天|昨天|昨日|最近|近\s*\d+\s*天|本周|本月)/.test(question)) { onClarify(); return }
    setRunning(true)
    window.setTimeout(() => {
      setResultReady(true); setResultTab(mode === 'sql' ? 'sql' : 'result'); setRunning(false)
      notify(mode === 'sql' ? '只读 SQL 已生成 · 未执行' : '只读查询完成 · 临时上下文已销毁')
    }, 700)
  }

  return (
    <div className="workspace-grid">
      <section className="query-stage">
        <div className="stage-head">
          <div className={`source-selector ${sourceOpen ? 'open' : ''}`}>
            <button className="source-trigger" onClick={() => setSourceOpen(value => !value)}>
              <span className="source-code">{source.code}</span><span><strong>{source.name}</strong><small>{source.meta}</small></span><b>⌄</b>
            </button>
            {sourceOpen && <div className="source-menu">
              <div className="source-menu-label"><span>选择本次查询的数据源</span><span>2 AVAILABLE</span></div>
              {sources.map(item => <button className={`${item.status === 'setup' ? 'disabled' : ''} ${source.code === item.code ? 'active' : ''}`} key={item.code} onClick={() => {
                if (item.status === 'setup') { notify('该数据源尚未完成只读凭证配置'); return }
                setSource(item); setSourceOpen(false); setResultReady(false); notify(`已切换数据源：${item.name}`)
              }}><span className="source-code">{item.code}</span><span><strong>{item.name}</strong><small>{item.meta}</small></span><em>{item.status === 'online' ? '● ONLINE' : 'SETUP'}</em></button>)}
            </div>}
          </div>
          <div className="mode-tabs"><button className={mode === 'query' ? 'active' : ''} onClick={() => setMode('query')}>快捷查询</button><button className={mode === 'sql' ? 'active' : ''} onClick={() => setMode('sql')}>仅生成 SQL</button></div>
        </div>
        <div className="composer">
          <div><textarea value={question} onChange={event => setQuestion(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); run() } }} placeholder="例如：今天支付失败的订单有多少？" /><button onClick={run}>{running ? '…' : '↗'}</button></div>
          <small><span>Enter 查询 · Shift + Enter 换行</span><span>✓ 历史查询不会自动进入本次上下文</span></small>
        </div>
        {!resultReady ? <div className="welcome">
          <div className="welcome-mark">↯</div><h2>今天想从数据里确认什么？</h2><p>系统会自动寻找相关表、生成只读 SQL，并在权限和成本检查通过后执行。</p>
          <div className="suggestions">{suggestions.map(([title, query, description]) => <button key={title} onClick={() => setQuestion(query)}><strong>{title}</strong><small>{description}</small></button>)}</div>
        </div> : <ResultTabs active={resultTab} onChange={setResultTab} onNavigate={onNavigate} notify={notify} />}
      </section>
      <TrustSidebar source={source} evidenceReady={resultReady} onResultTab={tab => { setResultTab(tab); setResultReady(true) }} onNavigate={onNavigate} />
    </div>
  )
}
