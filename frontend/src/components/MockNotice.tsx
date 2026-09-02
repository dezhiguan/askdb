import type { View } from '../types'

/** 每一页顶部的真实性声明。
 *
 * 换壳先行、接口后接，中间必然有一段时间页面显示的是样例数据。
 * 这段时间里最危险的不是数据假，而是**看不出它是假的** ——
 * 截图会被拿去汇报，数字会被当成结论。所以：只要这一页还没接后端，
 * 就必须有一条去不掉的说明，并写清楚后端到底到了哪一步。
 *
 * 接线规则：某一页接上真实接口后，从 NOTICES 里删掉对应条目即可，
 * 组件自动不再渲染。漏删会留下一条显眼的假声明，比漏加安全。
 */
interface Notice {
  phase: string
  backend: string
  legacy?: boolean
}

// 现在是空的：剩下的页要么已接真实后端，要么已经移除。
// 机制留着 —— 下一个页面接后端之前先在这里加一条，比"先上线再补声明"安全。
const NOTICES: Partial<Record<View, Notice>> = {}

export function MockNotice({ view }: { view: View }) {
  const notice = NOTICES[view]
  if (!notice) return null

  return (
    <div className="mock-notice">
      <span className="mock-tag">样例数据</span>
      <div className="mock-copy">
        <strong>本页显示的不是真实数据，不能作为判断依据。</strong>
        <small>{notice.backend}</small>
      </div>
      <div className="mock-side">
        <span className="mock-phase">{notice.phase}</span>
        {notice.legacy && <a className="mock-link" href="/legacy">去旧界面查真实数据 ↗</a>}
      </div>
    </div>
  )
}
