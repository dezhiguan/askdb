import type { View } from '../types'

/* 导航分组。
 *
 * 这里**不放 LIVE / NEW / 数字角标**：整站目前跑在样例数据上，
 * 在导航上标 LIVE 等于对能力状态说谎。真实接入状态由每页顶部的
 * MockNotice 逐页交代，接一页去一条，不会漏。
 */
export const navGroups: { label: string; items: { view: View; icon: string; title: string; subtitle: string }[] }[] = [
  {
    label: 'Workspace',
    items: [
      { view: 'query', icon: 'Q', title: '查询 Agent', subtitle: '自然语言安全查数' },
      { view: 'tasks', icon: 'TK', title: '任务中心', subtitle: '执行线程与断点续跑' },
      { view: 'sources', icon: 'DB', title: '数据源', subtitle: '只读库与表白名单' },
    ],
  },
  {
    label: 'Governance',
    items: [
      { view: 'permissions', icon: 'ID', title: '身份与权限', subtitle: 'SSO · RBAC · ABAC' },
      { view: 'glossary', icon: 'DI', title: '业务口径', subtitle: '指标与字段词典' },
      { view: 'traces', icon: 'TR', title: '执行追踪', subtitle: 'Agent 链路与 Span' },
      { view: 'audit', icon: 'AU', title: '审计中心', subtitle: '调用流水与复放' },
    ],
  },
]

