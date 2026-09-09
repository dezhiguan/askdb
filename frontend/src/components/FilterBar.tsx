import { useEffect, useState } from 'react'

/* 任务中心、审计中心、成员名册三处列表共用的筛选条。
   抽出来是因为三页要回答的是同一组问题：搜什么、正在按什么筛、筛完还剩几条。
   各写各的必然漂 —— 一页能摘单个条件、另一页只能整条重来，是同一个功能的
   两种手感。样式在 styles/filterbar.css。 */

export function FilterBar({ standalone, children }: {
  /** 独立成条（不在卡片里）时自己带边框与圆角 */
  standalone?: boolean
  children: React.ReactNode
}) {
  return <div className={`filterbar${standalone ? ' standalone' : ''}`}>{children}</div>
}

/** 关键词输入。**提交是防抖的，不是每次按键都发请求**；回车立即提交。
 *
 *  受控值由页面持有：摘掉关键词 chip、点重置时输入框要跟着清空，
 *  内部自己存一份草稿而不回写的话，条件已经清了而框里还留着字。 */
export function FilterSearch({ value, onCommit, placeholder }: {
  value: string
  onCommit: (next: string) => void
  placeholder: string
}) {
  const [draft, setDraft] = useState(value)
  // 外部改了（重置 / 摘 chip / 切页签）就回写输入框
  useEffect(() => { setDraft(value) }, [value])
  useEffect(() => {
    if (draft.trim() === value) return
    const timer = window.setTimeout(() => onCommit(draft.trim()), 300)
    return () => window.clearTimeout(timer)
    // onCommit 每次渲染都是新函数，进依赖会把防抖打成"每次渲染重新计时"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, value])
  return (
    <div className="filter-search">
      <input
        value={draft}
        placeholder={placeholder}
        onChange={event => setDraft(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter') onCommit(draft.trim()) }}
      />
    </div>
  )
}

export interface FilterChip {
  /** 条件名，如「状态」「风险」 */
  label: string
  /** 当前取值的显示文案 */
  value: string
  /** 单独摘掉这一个条件 */
  onClear: () => void
}

/** 已选条件 + 命中数。一个条件都没有时整条不渲染。
 *
 *  命中数给的是「筛完 / 筛之前」两个数：只给一个的话，"筛完没有"与
 *  "本来就没有"在页面上分不开。 */
export function FilterChips({ chips, matched, total, unit = '条', standalone }: {
  chips: FilterChip[]
  matched: number
  total: number
  unit?: string
  standalone?: boolean
}) {
  if (!chips.length) return null
  return (
    <div className={`filter-chips${standalone ? ' standalone' : ''}`}>
      <span className="chips-label">已选</span>
      {chips.map(chip => (
        <span className="filter-chip" key={`${chip.label}:${chip.value}`}>
          {chip.label} <b>{chip.value}</b>
          <button type="button" aria-label={`清除${chip.label}筛选`} onClick={chip.onClear}>×</button>
        </span>
      ))}
      <span className="filter-hits">命中 {matched} / {total} {unit}</span>
    </div>
  )
}
