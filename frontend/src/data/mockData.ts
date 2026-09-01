import type { DataSource, View } from '../types'

export const sources: DataSource[] = [
  { code: 'PG', shortName: '订单中心', name: '订单中心只读镜像', meta: 'PostgreSQL 15 · PROD-RO · 42 张表', status: 'online' },
  { code: 'CK', shortName: '用户行为分析', name: '用户行为分析', meta: 'ClickHouse · PROD-RO · 18 张表', status: 'online' },
  { code: 'MY', shortName: '仓储测试库', name: '仓储测试库', meta: 'MySQL 8 · 凭证未配置', status: 'setup' },
]

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
      { view: 'query', icon: 'Q', title: '查询工作台', subtitle: '自然语言安全查数' },
      { view: 'tasks', icon: 'TK', title: '任务中心', subtitle: '复杂查询与审批' },
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
  {
    label: 'Scale',
    items: [
      { view: 'connectors', icon: 'CN', title: 'Connector 节点', subtitle: '跨网络数据接入' },
      { view: 'developer', icon: '>_', title: '开发者工具', subtitle: 'CLI · IDE · API' },
      { view: 'roadmap', icon: 'RM', title: '产品落地路线', subtitle: '分阶段能力规划' },
    ],
  },
]

export const suggestions = [
  ['支付失败订单', '今天支付失败的订单有多少？', '统计今天失败订单数量和主要原因'],
  ['高额退款排查', '昨天退款金额最高的5个订单', '查看 TOP 5 并自动脱敏用户字段'],
  ['仓库积压情况', '上海仓还有多少订单没有发货？', '按仓库统计未发货订单'],
  ['新客首单转化', '本周新客首单转化率是多少？', '使用已确认业务口径计算'],
]
