import type { Me } from './api'

/** 未登录时禁用「会创建、修改或导出东西」的入口。
 *
 * **置灰只是少让人白点一次，它不是安全边界。** 真正的边界在服务端：
 * server.py 的写入中间件默认拒绝一切 POST/PUT/PATCH/DELETE，审计原文与回放
 * 另有各自的判据。这里做的只是把结论提前告诉用户，而不是等他点完才知道。
 *
 * 集中一处，是因为这句提示要出现在七个入口上。各写各的必然漂成七种说法，
 * 而用户看到的是同一件事。
 */
export interface WriteGuard {
  /** 允许执行 —— 已登录 */
  can: boolean
  /** 直接摊到 button 上：disabled + title */
  props: { disabled: boolean; title: string | undefined }
}

export function writeGuard(me: Me | null, what = '这个操作'): WriteGuard {
  const can = !!me?.username
  // 「未登录还能做什么」跟着实例配置走：查询也要登录的实例上，
  // 这句话里不能再许诺"可以查询"。
  const left = me?.can_query ? '未登录可以浏览与查询' : '未登录可以浏览'
  return {
    can,
    props: {
      disabled: !can,
      title: can ? undefined : `${what}需要登录后才能执行；${left}`,
    },
  }
}
