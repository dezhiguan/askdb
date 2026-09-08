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
  /** 部署方指定的默认源 id（配置 datasources.default，服务端解析）。
   *  空串 = 没指定。**没选源时该停在哪个库，两处都读它**：顶栏在这里回落，
   *  工作台的选择器也必须，否则一进来就是顶栏 A 库、选择器 B 库。 */
  defaultId: string
}

/**
 * @param hasBuiltin 启动配置里有没有默认数据源。null = health 还没读到。
 *   没有内置源时，回落到部署方指定的那个源（/api/sources 的 default_source_id），
 *   没指定才取第一个运行时源。顶栏与工作台的选择器必须按同一条规则回落 ——
 *   否则第一次进来就是"顶栏说未设默认源、下面的选择器已经选中了某个库"，
 *   两处对不上。
 */
export function useSources(hasBuiltin: boolean | null): SourcesState {
  const [items, setItems] = useState<SourceCard[]>([])
  const [sourceId, setSourceId] = useState('')
  // 部署方指定的默认源（配置里的 datasources.default，服务端解析成 id）。
  // 没指定就是空串 —— 那时才退回"列表第一个"。
  const [defaultId, setDefaultId] = useState('')

  useEffect(() => {
    let alive = true
    fetchSources()
      .then(d => { if (alive) { setItems(d.items); setDefaultId(d.default_source_id) } })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  // 没有内置源时，"没选"落到哪个库：部署方指定的那个优先。取不到再按注册
  // 顺序取第一个 —— 它至少让站点可用，但**不表达任何意图**，所以只当兜底。
  // 服务端对同一件事的判断在 server._default_source，两处必须是同一个源，
  // 否则顶栏说 A 库、不带 source 的调用打在 B 库上。
  const fallback = items.find(i => i.id === defaultId && !i.builtin)
    ?? items.find(i => !i.builtin)
    ?? null

  return {
    items,
    sourceId,
    setSourceId,
    defaultId,
    // sourceId 为空串意为"用内置源"。内置源确实存在时顶栏走 health 那条分支，
    // 这里返回 null 是对的；内置源根本没配时，实际生效的是上面那个回落。
    current: items.find(i => i.id === sourceId && !i.builtin)
      ?? (sourceId === '' && hasBuiltin === false ? fallback : null),
  }
}
