import type { View } from '../types'

/* 导航分组 —— 版式与文案以原型 trusted-data-agent-prototype.html 为准。
 *
 * 分组名不带阶段序号、条目不带 LIVE / NEW 角标：那些是原型期的路线图标记，
 * 对使用者只回答"这个功能做完了没有"，而侧栏上出现的每一项本来就都能用。
 * 某一页的真实接入状态由该页顶部的 MockNotice 逐页交代，不靠角标暗示。
 */
export const navGroups: {
  label: string
  items: { view: View; icon: string; title: string; subtitle: string }[]
}[] = [
  {
    label: 'Workspace',
    items: [
      { view: 'query', icon: 'Q', title: '查询 Agent', subtitle: '自然语言安全查数' },
      { view: 'tasks', icon: 'TK', title: '任务中心', subtitle: '执行线程与断点续跑' },
      { view: 'sources', icon: 'DB', title: '数据源', subtitle: '只读库与镜像' },
    ],
  },
  {
    label: 'Governance',
    items: [
      { view: 'permissions', icon: 'ID', title: '身份与权限', subtitle: 'SSO · RBAC · ABAC' },
      { view: 'glossary', icon: 'DI', title: '业务口径', subtitle: '指标与字段词典' },
      { view: 'evaluation', icon: 'QA', title: 'Agent 质量中心', subtitle: '运行健康与持续评测' },
      { view: 'traces', icon: 'TR', title: '执行追踪', subtitle: 'Agent 链路与 Span' },
      // 「高成本审批」入口 2026-09-06 撤下（产品决定）。**页面与接口都还在**：
      // View 'approvals'、ApprovalsPage、/api/approvals 一律保留，随时把这一行
      // 加回来就恢复。别顺手把它们当残渣清掉 ——
      // 撤的是入口，不是能力：R-11 拦下超阈值查询时仍会登记待审批，
      // 只是眼下界面上没有放行的地方，只能直接调接口。
      { view: 'audit', icon: 'AU', title: '审计中心', subtitle: '查询执行全链路' },
    ],
  },
]
