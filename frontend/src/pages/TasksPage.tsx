import { useCallback, useEffect, useState } from 'react'
import { fetchTasks, resumeTask, type Task, type TasksResult } from '../api'
import { PageHeader } from '../components/AppShell'
import type { View } from '../types'

function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function TasksPage({ onNavigate, notify }: {
  onNavigate: (view: View) => void
  notify: (message: string) => void
}) {
  const [result, setResult] = useState<TasksResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(() => {
    fetchTasks()
      .then(setResult)
      .catch(e => setError(String(e.message || e)))
  }, [])

  useEffect(load, [load])

  const resume = async (task: Task) => {
    setBusy(task.thread_id)
    try {
      const r = await resumeTask(task.thread_id)
      if (!r) {
        notify('这个任务已经跑完，或不属于当前账号')
      } else if (r.ok) {
        notify(`已从断点续跑完成 · 新 trace ${r.trace_id}`)
      } else {
        notify(`续跑仍未完成：${r.rejected_by ?? ''} ${r.error ?? ''}`.trim())
      }
      load()
    } catch (e) {
      notify(String((e as Error).message || e))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="Phase 1 · Governed Tasks"
        title="任务中心"
        description="中断的调用会留下可续跑的现场：判定链路存进检查点，从断点继续而不是从头重来。"
      />

      {error && <div className="audit-error">读取任务失败：{error}</div>}

      {/* 这一段是这页最要紧的东西：askdb 的「任务」到底是什么。
          原型讲的是"缺少时间范围，任务在生成 SQL 前暂停，补充输入后继续"——
          那是产品化的澄清流程，askdb 没有。这里的中断是**故障恢复**：
          进程挂了、递归超限、检查点库出问题，执行停在节点边界。
          把两者混为一谈，会让人以为能靠它做审批与人工介入。 */}
      <section className="card notice-card">
        <h3>这里的「任务」指什么</h3>
        <p>
          askdb 的提问是<b>同步执行</b>的，没有任务队列。会出现在这一页的，是
          <b>执行中断后留下可续跑现场</b>的调用 —— 进程故障、递归超限、检查点库异常
          都会让执行停在某个节点边界，此时判定现场已存入检查点，可以从断点继续，
          已完成的节点不会重跑。
        </p>
        <p>
          续跑写<b>新的 trace_id</b>，但线程不变，两条记录经 <span className="mono">thread_id</span> 关联，
          审计里能看出这是第几次执行；续跑另计一次每日配额。
        </p>
        <p className="hint-warn">
          原型里「信息缺失 → 用户补充 → 从暂停节点继续」的澄清流程、以及高风险查询的
          审批流，askdb 都还没有。它们需要先把提问异步化，属于后续阶段。
        </p>
      </section>

      {result?.status === 'need_login' && (
        <section className="card notice-card">
          <h3>任务列表需要登录</h3>
          <p>{result.detail}</p>
          <p>
            这不是懒得做匿名支持：中断的任务里带着发起人问过的问题原文，
            列出来就等于任何人都能看到、并续跑别人的任务。
            所以列表只对已登录用户开放，且<b>只列自己发起的</b>。
          </p>
        </section>
      )}

      {result?.status === 'ok' && (
        <section className="card table-scroll">
          <h3>
            可续跑任务
            <span className="section-note">
              账号 {result.user} · {result.items.length} 个
            </span>
          </h3>
          <table className="audit-table">
            <thead>
              <tr>
                <th>问题</th><th>线程</th><th>首次发起</th><th>最近中断</th>
                <th className="num">已执行</th><th>状态</th><th />
              </tr>
            </thead>
            <tbody>
              {result.items.map(task => (
                <tr key={task.thread_id}>
                  <td className="audit-question" title={task.question ?? ''}>
                    {task.question || <span className="dim">（无问题文本）</span>}
                  </td>
                  <td className="mono">{task.thread_id}</td>
                  <td className="mono dim">{fmtTime(task.first_ts)}</td>
                  <td className="mono dim">{fmtTime(task.ts)}</td>
                  <td className="num">{task.attempts_on_thread} 次</td>
                  <td><span className={`status ${STATUS_TONE[task.status]}`}>{STATUS_LABEL[task.status]}</span></td>
                  <td>
                    {/* 只有中断的线程才有断点可续。给已收尾的线程也挂按钮，
                        点了必然 404 —— 那不是"暂时不可用"，是这条路根本不存在 */}
                    {task.resumable ? (
                      <button
                        className="primary"
                        disabled={busy === task.thread_id}
                        onClick={() => resume(task)}
                      >
                        {busy === task.thread_id ? '续跑中…' : '从断点续跑'}
                      </button>
                    ) : (
                      <span className="dim" title="这条线程已经收尾，没有可续的断点">—</span>
                    )}
                  </td>
                </tr>
              ))}
              {result.items.length === 0 && (
                <tr><td colSpan={7} className="tasks-empty">
                  <strong>这个账号名下还没有执行记录。</strong>
                  <span>
                    任务由提问产生 —— 登录后到查询工作台问一次，这里就会出现对应的线程。
                    历史记录若是匿名发起的，不会归到任何账号名下。
                  </span>
                </td></tr>
              )}
            </tbody>
          </table>
          <div className="tasks-foot">
            <button className="ghost" onClick={load}>刷新</button>
            <button className="ghost" onClick={() => onNavigate('traces')}>去执行追踪看节点明细</button>
          </div>
        </section>
      )}

      {!result && !error && <section className="card notice-card"><p>读取中…</p></section>}
    </div>
  )
}


const STATUS_LABEL: Record<string, string> = {
  interrupted: '中断 · 可续跑',
  rejected: '被护栏拦下',
  done: '已完成',
}

const STATUS_TONE: Record<string, string> = {
  interrupted: 'wait',
  rejected: 'bad',
  done: '',
}
