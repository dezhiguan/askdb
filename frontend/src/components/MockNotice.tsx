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

const NOTICES: Partial<Record<View, Notice>> = {
  glossary: {
    phase: '接后端：阶段 B',
    backend: '业务口径已经在跑：定义在 config/*-metrics.yaml，命中后强制注入提示词并参与召回，'
      + '只是还没有页面。本页等 schema registry 落地后接线。',
  },
}

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
