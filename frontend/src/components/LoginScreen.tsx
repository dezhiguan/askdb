import { useEffect, useRef, useState } from 'react'
import { enterDemo, login, type Me } from '../api'

type Mode = 'choice' | 'account'

/** 全屏登录页，对齐原型 `.login-screen`。
 *
 * 左侧是这套系统凭什么可信的说明（执行路径 + 三条硬约束），右侧两段式：
 * 先选进入方式，再填账号。**两段而不是一屏铺开**，是因为大多数访客走的是
 * 「一键体验」那条路，账号框对他们是噪音。
 *
 * 与原型的差异只有一处、且是有意的：原型里这屏是**拦在前面的门**，
 * askdb 的匿名身份本来就能查数（`me.required=false`），所以这屏由顶栏
 * 唤起，点遮罩或 Esc 可以退回去 —— 不给退路等于把匿名这条路堵死。
 */
export function LoginScreen({ me, onClose, onDone, notify }: {
  me: Me
  onClose: () => void
  onDone: () => void
  notify: (message: string) => void
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

  const demo = me.demo_accounts[0]

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

  // 匿名可用，所以这屏必须能退出去
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    chooseRef.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

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

  const runDemo = async () => {
    if (!demo) return
    clearError()
    setBusy(true)
    try {
      await enterDemo(demo.username)
      setPassword('')
      onDone()
      notify(`已切换到「${demo.display_name}」· 该角色的可见范围已生效`)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen show" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
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
          <div className="login-trust-row"><span>READ ONLY</span><span>MASKED</span><span>AUDITED</span></div>
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
                {demo && (
                  <button className="login-entry demo" type="button" disabled={busy} onClick={runDemo}>
                    <i className="login-entry-icon">DEMO</i>
                    <span className="login-entry-copy">
                      <strong>一键体验</strong>
                      <small>免口令，以「{demo.display_name}」的角色进入</small>
                    </span>
                    <b className="login-entry-arrow">↗</b>
                  </button>
                )}
              </div>
              {error && mode === 'choice' && <p className="login-error" role="alert">{error}</p>}
              <div className="login-choice-note">
                <i>✓</i>
                <span>一键体验只跳过口令、不跳过权限：可见的表与返回行数仍按该角色收窄，走的是同一条执行路径。</span>
              </div>
            </div>

            <div className={`login-mode-panel ${mode === 'account' ? 'active' : ''}`}>
              <button className="login-back" type="button" onClick={() => switchMode('choice')}>← 返回选择</button>
              <div className="eyebrow">Account Login</div>
              <h2>账号登录</h2>
              <p>输入账号和密码进入可信数据工作台。</p>

              <div className="login-field">
                <div className="login-field-head"><label htmlFor="loginAccount">账号</label><span>ACCOUNT</span></div>
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
                <div className="login-field-head"><label htmlFor="loginPassword">密码</label><span>PASSWORD</span></div>
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
              <p className="login-error" role="alert" aria-live="polite">{mode === 'account' ? error : ''}</p>
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
