import { useEffect, useRef, useState } from 'react'
import { login, type Me } from '../api'

type Mode = 'choice' | 'account'

/** 全屏登录页，对齐原型 `.login-screen`。
 *
 * 左侧是这套系统凭什么可信的说明（执行路径 + 三条硬约束），右侧两段式：
 * 先选进入方式，再填账号。**两段而不是一屏铺开**，是因为大多数访客走的是
 * 「一键体验」那条路，账号框对他们是噪音。
 *
 * 两种形态，由 `dismissible` 决定，判据来自后端的 `me.required`：
 *   · 门（required=true）—— 未登录进不去，没有 Esc、点遮罩也不关。给一个
 *     关得掉却什么都做不了的门，只会让人以为页面坏了。
 *   · 浮层（required=false）—— 匿名本来就能查数，登录只是放宽可见范围，
 *     必须留退路。
 */
export function LoginScreen({ me, onClose, onDone, onSkip, notify, dismissible = true }: {
  me: Me
  onClose: () => void
  onDone: () => void
  /** 跳过登录，以未登录身份进入。仅在 me.required 为 false 时可用 */
  onSkip: () => void
  notify: (message: string) => void
  /** false = 这屏是门。关闭入口（Esc / 点遮罩）整体禁用 */
  dismissible?: boolean
}) {
  const [mode, setMode] = useState<Mode>('choice')
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [reveal, setReveal] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // 空值提交时给输入框描红。与 error 分开存 —— 一次提交可能同时点两个框
  const [invalid, setInvalid] = useState<{ account: boolean; password: boolean }>({ account: false, password: false })

  const accountRef = useRef<HTMLInputElement>(null)
  const passwordRef = useRef<HTMLInputElement>(null)
  const chooseRef = useRef<HTMLButtonElement>(null)

  const clearError = () => {
    setError('')
    setInvalid({ account: false, password: false })
  }

  const switchMode = (next: Mode) => {
    setMode(next)
    clearError()
    if (next === 'choice') {
      setPassword('')
      setReveal(false)
    }
    // 原型的 80ms：等切面板的入场动画起来再移焦点，否则焦点环会闪
    window.setTimeout(() => {
      (next === 'account' ? accountRef.current : chooseRef.current)?.focus()
    }, 80)
  }

  useEffect(() => {
    chooseRef.current?.focus()
    if (!dismissible) return                 // 门：没有退路，也就没有 Esc
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, dismissible])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    clearError()
    const name = account.trim()
    if (!name || !password) {
      setInvalid({ account: !name, password: !password })
      setError(!name && !password ? '请输入账号和密码' : (!name ? '请输入账号' : '请输入密码'))
      ;(!name ? accountRef.current : passwordRef.current)?.focus()
      return
    }

    setBusy(true)
    try {
      await login(name, password)
      setPassword('')
      onDone()
      notify('登录成功 · 身份权限与数据范围已加载')
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  // 一键体验 = **跳过登录**，不是以某个账号进入。不发任何请求、不建会话，
  // 关掉这扇门之后就是未登录身份本身：能只读查询，一切写操作被后端中间件拦下。
  const skip = () => {
    onSkip()
    notify('已以未登录身份进入 · 可以查询，但改动配置的操作需要登录')
  }

  return (
    <div
      className="login-screen show"
      onMouseDown={event => {
        if (dismissible && event.target === event.currentTarget) onClose()
      }}
    >
      <div className="login-layout">
        <section className="login-visual" aria-label="可信查询流程">
          <div className="login-brand">
            <div className="login-brand-mark">ASK</div>
            <div><strong>可信数据问答平台</strong><small>TRUSTED DATA AGENT</small></div>
          </div>
          <div className="login-manifesto">
            <span>NATURAL LANGUAGE DATABASE AGENT</span>
            <h1>不用写 SQL，<br />直接问数据库。</h1>
            <p>面向开发、测试和产品的可信问数智能体。自然语言自动生成只读 SQL，在权限、脱敏和审计约束下返回可验证结果。</p>
          </div>
          <div className="login-route">
            <div className="login-route-label"><span>TRUSTED EXECUTION PATH</span><span>ONE-SHOT</span></div>
            <div className="login-route-flow">
              <div className="login-route-node"><strong>身份认证</strong><small>AUTH</small></div>
              <div className="login-route-node"><strong>语义理解</strong><small>PLAN</small></div>
              <div className="login-route-node"><strong>SQL 护栏</strong><small>GUARD</small></div>
              <div className="login-route-node"><strong>结果证据</strong><small>PROOF</small></div>
            </div>
          </div>
        </section>

        <section className="login-panel">
          <form className="login-card" onSubmit={submit} noValidate>
            <div className={`login-mode-panel ${mode === 'choice' ? 'active' : ''}`}>
              <div className="eyebrow">Welcome</div>
              <h2>选择进入方式</h2>
              <p>用已有账号登录，或直接以内置角色进入，完整流程一样走一遍。</p>
              <div className="login-entry-list">
                <button className="login-entry" ref={chooseRef} type="button" onClick={() => switchMode('account')}>
                  <i className="login-entry-icon">ID</i>
                  <span className="login-entry-copy"><strong>账号登录</strong><small>使用已有账号和密码进入工作台</small></span>
                  <b className="login-entry-arrow">→</b>
                </button>
                {/* 这个入口只在"未登录也能查"的实例上成立。required 的实例上
                    点了也进不去（后端 401），摆着就是骗点击 */}
                {!me.required && (
                  <button className="login-entry guest" type="button" onClick={skip}>
                    <i className="login-entry-icon">TRY</i>
                    <span className="login-entry-copy">
                      <strong>一键体验</strong>
                      <small>跳过登录直接查数，改动配置的操作需要登录</small>
                    </span>
                    <b className="login-entry-arrow">↗</b>
                  </button>
                )}
              </div>
              {error && mode === 'choice' && (
                <p className="login-error show" role="alert"><i aria-hidden="true">!</i><span>{error}</span></p>
              )}
              {/* 这句随入口走。没有一键体验时还挂着解释它的话，是在讲一个不存在的功能 */}
              <div className="login-choice-note">
                <i>✓</i>
                <span>{me.required
                  ? '账号由部署方内置，没有注册与找回密码入口。每次查询都会记入审计，标明是以哪个身份发起的。'
                  : '未登录也能查数，走的是同一条执行路径、同样受护栏约束；但添加数据源、改成员这类会改动配置的操作，必须登录。'}</span>
              </div>
            </div>

            <div className={`login-mode-panel ${mode === 'account' ? 'active' : ''}`}>
              <button className="login-back" type="button" onClick={() => switchMode('choice')}>← 返回选择</button>
              <h2>账号登录</h2>
              <p>输入账号和密码进入可信数据工作台。</p>

              <div className="login-field">
                <div className="login-field-head"><label htmlFor="loginAccount">账号</label></div>
                <div className="login-input-shell">
                  <i>ID</i>
                  <input
                    id="loginAccount"
                    ref={accountRef}
                    className={invalid.account ? 'invalid' : ''}
                    type="text"
                    autoComplete="username"
                    placeholder="请输入账号"
                    value={account}
                    onChange={e => { setAccount(e.target.value); clearError() }}
                  />
                </div>
              </div>
              <div className="login-field">
                <div className="login-field-head"><label htmlFor="loginPassword">密码</label></div>
                <div className="login-input-shell">
                  <i>••</i>
                  <input
                    id="loginPassword"
                    ref={passwordRef}
                    className={invalid.password ? 'invalid' : ''}
                    type={reveal ? 'text' : 'password'}
                    autoComplete="current-password"
                    placeholder="请输入密码"
                    value={password}
                    onChange={e => { setPassword(e.target.value); clearError() }}
                  />
                  <button
                    className="login-password-toggle"
                    type="button"
                    aria-label={reveal ? '隐藏密码' : '显示密码'}
                    onClick={() => setReveal(v => !v)}
                  >
                    {reveal ? '隐藏' : '显示'}
                  </button>
                </div>
              </div>
              <p className={`login-error ${mode === 'account' && error ? 'show' : ''}`} role="alert" aria-live="polite">
                {mode === 'account' && error ? <><i aria-hidden="true">!</i><span>{error}</span></> : ''}
              </p>
              <button className="login-submit" type="submit" disabled={busy}>
                {busy ? '正在验证…' : <>登录工作台 <b>→</b></>}
              </button>
              <p className="login-note">登录行为将被记录，密码仅用于当前身份验证，不会发送给模型。</p>
            </div>
          </form>
        </section>
      </div>
    </div>
  )
}
