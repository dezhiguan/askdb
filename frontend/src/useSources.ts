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

/**
 * @param hasBuiltin 启动配置里有没有默认数据源。null = health 还没读到。
 *   没有内置源时，工作台的选择器会回落到第一个运行时源（options[0]），
 *   顶栏必须按同一条规则回落 —— 否则第一次进来就是"顶栏说未设默认源、
 *   下面的选择器已经选中了某个库"，两处对不上。
 */
export function useSources(hasBuiltin: boolean | null): SourcesState {
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
    // sourceId 为空串意为"用内置源"。内置源确实存在时顶栏走 health 那条分支，
    // 这里返回 null 是对的；内置源根本没配时，实际生效的是第一个运行时源。
    current: items.find(i => i.id === sourceId && !i.builtin)
      ?? (sourceId === '' && hasBuiltin === false
        ? items.find(i => !i.builtin) ?? null
        : null),
  }
}
