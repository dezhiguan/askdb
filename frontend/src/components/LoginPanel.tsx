import { useState } from 'react'
import { enterDemo, login, type Me } from '../api'

/** 登录面板。
 *
 * 做成顶栏里点开的浮层，**不是拦在前面的门**。这个站要给人看的是护栏、
 * 审计与角色收窄；登录页是访客流失最大的一处，挡在前面等于把要展示的
 * 东西全挡住了。匿名照样能查数，登录只是把可见范围放宽。
 */
export function LoginPanel({ me, onClose, onDone, notify }: {
  me: Me
  onClose: () => void
  onDone: () => void
  notify: (message: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const run = async (action: () => Promise<void>, ok: string) => {
    setBusy(true)
    setError('')
    try {
      await action()
      onDone()
      notify(ok)
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <div className="login-panel">
        <div className="login-panel-head">
          <div>
            <div className="eyebrow">SIGN IN</div>
            <h3>登录以放宽可见范围</h3>
          </div>
          <button onClick={onClose} aria-label="关闭">×</button>
        </div>

        <p className="drawer-note">
          匿名也能查数，只是可见的表更少、返回行更少。登录不改变护栏，
          只改变<b>这个身份能看到哪些表</b>。
        </p>

        {me.demo_accounts.length > 0 && (
          <>
            <h4>一键体验</h4>
            <div className="demo-list">
              {me.demo_accounts.map(account => (
                <button
                  key={account.username}
                  className="demo-account"
                  disabled={busy}
                  onClick={() => run(() => enterDemo(account.username), `已切换到「${account.display_name}」`)}
                >
                  <span>
                    <strong>{account.display_name}</strong>
                    <small>{account.note || account.roles.join(' · ')}</small>
                  </span>
                  <em>{account.roles.join('+')}</em>
                </button>
              ))}
            </div>
            <p className="drawer-note">
              一键体验只跳过口令，<b>不跳过权限</b> —— 拿到的仍是该角色的可见范围，
              与口令登录走完全同一条路径。
            </p>
          </>
        )}

        <h4>账号口令</h4>
        <div className="login-form-rows">
          <input
            placeholder="账号"
            value={username}
            onChange={e => setUsername(e.target.value)}
            autoComplete="username"
          />
          <input
            type="password"
            placeholder="口令"
            value={password}
            onChange={e => setPassword(e.target.value)}
            autoComplete="current-password"
            onKeyDown={e => {
              if (e.key === 'Enter' && username && password) {
                run(() => login(username, password), `已登录：${username}`)
              }
            }}
          />
          <button
            className="primary"
            disabled={busy || !username || !password}
            onClick={() => run(() => login(username, password), `已登录：${username}`)}
          >
            登录
          </button>
        </div>

        {error && <div className="login-error">{error}</div>}

        <p className="drawer-note">
          没有注册入口，也没有找回密码 —— 账号是实例内置的固定几个。
          每次查询都会写入审计日志，记录是以哪个角色发起的。
        </p>
      </div>
    </>
  )
}
