import { useEffect, useState } from 'react'
import { fetchSources, type SourceCard } from './api'

/** 当前选中的数据源 —— 顶栏与查询工作台**共用同一份状态**。
 *
 *  原来 sourceId 只活在 QueryWorkspace 内部，顶栏拿的是 /api/health（永远是
 *  内置源）。于是切到别的库之后，顶栏仍写着内置库的名字，而它头上顶着
 *  "当前空间" 四个字 —— 正在查 A 库、界面说你在 B 库，是最坏的一种错。
 */
export interface SourcesState {
  items: SourceCard[]
  /** 空串 = 启动配置里的内置源 */
  sourceId: string
  setSourceId: (id: string) => void
  /** 选中的运行时数据源；内置源时为 null */
  current: SourceCard | null
}

export function useSources(): SourcesState {
  const [items, setItems] = useState<SourceCard[]>([])
  const [sourceId, setSourceId] = useState('')

  useEffect(() => {
    let alive = true
    fetchSources()
      .then(d => { if (alive) setItems(d.items) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  return {
    items,
    sourceId,
    setSourceId,
    current: items.find(i => i.id === sourceId && !i.builtin) ?? null,
  }
}
