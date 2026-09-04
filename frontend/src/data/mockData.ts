import type { View } from '../types'

/* 导航分组 —— 版式与文案以原型 trusted-data-agent-prototype.html 为准。
 *
 * badge 目前是原型上的占位标记（LIVE / 数字 / NEW），页面接真数据后
 * 由各页自行回填；每页顶部的 MockNotice 仍逐页交代真实接入状态。
 */
export const navGroups: {
  label: string
  items: { view: View; icon: string; title: string; subtitle: string; badge?: string }[]
}[] = [
  {
    label: 'Workspace · 阶段一',
    items: [
      { view: 'query', icon: 'Q', title: '查询工作台', subtitle: '自然语言安全查数', badge: 'LIVE' },
      { view: 'tasks', icon: 'TK', title: '任务中心', subtitle: '执行线程与断点续跑' },
      { view: 'sources', icon: 'DB', title: '数据源', subtitle: '只读库与镜像' },
    ],
  },
  {
    label: 'Governance · 阶段二',
    items: [
      { view: 'permissions', icon: 'ID', title: '身份与权限', subtitle: 'SSO · RBAC · ABAC' },
      { view: 'glossary', icon: 'DI', title: '业务口径', subtitle: '指标与字段词典' },
      { view: 'traces', icon: 'TR', title: '执行追踪', subtitle: 'Agent 链路与 Span', badge: 'NEW' },
      { view: 'audit', icon: 'AU', title: '审计中心', subtitle: '查询执行全链路' },
    ],
  },
]
