import { useEffect, useState } from 'react'
import { fetchHealth, type Health } from './api'

export type HealthState =
  | { status: 'loading' }
  | { status: 'ready'; health: Health }
  | { status: 'error'; message: string }

/** 取一次 /api/health。失败不静默 —— 连不上后端时外壳必须说出来，
 *  而不是继续显示上一次的值或一个看着正常的空壳。 */
export function useHealth(): HealthState {
  const [state, setState] = useState<HealthState>({ status: 'loading' })

  useEffect(() => {
    let alive = true
    fetchHealth()
      .then(health => { if (alive) setState({ status: 'ready', health }) })
      .catch(error => { if (alive) setState({ status: 'error', message: String(error.message || error) }) })
    return () => { alive = false }
  }, [])

  return state
}
