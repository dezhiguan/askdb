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
      // 「人工处置」入口 2026-09-12 撤下（产品决定）：**三类处置全部搬进了
      // 任务中心**——审批、复核、运维处置都在任务详情弹窗里做，一条任务一个
      // 入口，不再有"同一件事两个地方点"。
      //
      // 与 2026-09-06 那次撤下**性质完全不同**，别照着那次的结论理解：那次撤的
      // 是入口而处置能力无处可去，代价是三档状态只进不出、积压到没人能动；
      // 这次是能力先搬完、确认可用，再撤重复的那一页。
      //
      // ApprovalsPage 与 /api/approvals、/api/reviews、/api/ops 一律保留。
      // 队列页解决的是"去哪找待办"（130 条在任务列表里翻确实费劲），哪天
      // 需要按队列办公，把这一行加回来即可 —— 它与任务中心共用同一批接口，
      // 不是两套实现。
      { view: 'audit', icon: 'AU', title: '审计中心', subtitle: '查询执行全链路' },
    ],
  },
]
