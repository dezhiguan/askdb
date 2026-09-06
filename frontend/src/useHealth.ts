import { useEffect, useRef, useState } from 'react'
import { fetchHealth, type Health } from './api'

export type HealthState =
  | { status: 'loading' }
  | { status: 'ready'; health: Health }
  | { status: 'error'; message: string }

/** 失败后的重试间隔（毫秒），逐级退避，末项之后一直用末项。
 *  头两次给得密是因为最常见的失败就是后端正在重启（本地开发、滚动更新）——
 *  那种情况下几秒就恢复了，等 30 秒纯属白等。 */
const RETRY_MS = [2_000, 4_000, 8_000, 15_000, 30_000]

/** 正常时的复查间隔。/api/health 里的数据源状态会自己变（隧道断了、库重启），
 *  只在挂载时取一次意味着那一刻之后页面顶栏说的就都是历史。 */
const POLL_MS = 30_000

/** 取 /api/health，并**持续**跟着它变。
 *
 *  失败不静默 —— 连不上后端时外壳必须说出来，而不是继续显示上一次的值
 *  或一个看着正常的空壳。
 *
 *  但"说出来"只是一半：原来这个 hook 的依赖数组是空的，只取一次、失败之后
 *  既不重试也不轮询，于是后端恢复了它也不知道，红条一直挂到用户自己想起来
 *  刷新。本地开发每重启一次后端就要手动刷一次页面；生产滚动更新期间
 *  （maxUnavailable: 1）恰好打到正在重建那个副本的用户，会拿到一个**永久**
 *  红条。所以这里改成：失败退避重试、正常定期复查，两条路都会把状态改回来。
 */
export function useHealth(): HealthState {
  const [state, setState] = useState<HealthState>({ status: 'loading' })
  // 连续失败次数只用来选退避档位，不进 state —— 它变一次就重渲染一次，
  // 而界面上没有任何东西依赖"这是第几次重试"
  const failures = useRef(0)

  useEffect(() => {
    let alive = true
    let timer: number | undefined

    const tick = () => {
      fetchHealth()
        .then(health => {
          if (!alive) return
          failures.current = 0
          setState({ status: 'ready', health })
          schedule(POLL_MS)
        })
        .catch(error => {
          if (!alive) return
          setState({ status: 'error', message: String(error.message || error) })
          const i = Math.min(failures.current, RETRY_MS.length - 1)
          failures.current += 1
          schedule(RETRY_MS[i])
        })
    }

    const schedule = (ms: number) => {
      if (!alive) return
      timer = window.setTimeout(tick, ms)
    }

    tick()
    return () => {
      alive = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  return state
}
