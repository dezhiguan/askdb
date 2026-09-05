import { useEffect, useState } from 'react'

const remaining = (until: number) => Math.max(0, Math.ceil((until - Date.now()) / 1000))

/** 到 `until`（epoch ms）为止还剩几秒；不在冷却中时为 0。
 *
 *  参数取的是**截止时刻**而不是"还剩几秒"：连续两次被限流很可能拿到相同的
 *  秒数，用秒数当依赖项时 effect 不会重跑，倒计时会卡在上一轮的残值上。
 */
export function useCountdown(until: number): number {
  const [left, setLeft] = useState(() => remaining(until))

  useEffect(() => {
    setLeft(remaining(until))
    if (remaining(until) <= 0) return
    const timer = window.setInterval(() => setLeft(remaining(until)), 1000)
    return () => window.clearInterval(timer)
  }, [until])

  return left
}
