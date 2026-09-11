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
      // 2026-09-11 恢复，并从「高成本审批」扩成三条队列（审批 / 复核 / 运维）。
      //
      // 这一行 2026-09-06 被撤下，当时的判断是"界面上先不放行"。代价在
      // 2026-09-11 盘出来了：三档需要人工介入的状态**只进不出** —— 线上积压
      // 61 条待审批、25 条待复核、1 条待处置，最早的挂了两天多，没有任何人
      // 有地方点一下。撤入口不等于撤能力，但没有入口的能力等于没有。
      //
      // 队列页解决的是"去哪找待办"；具体处置在任务中心也能做（那里是按任务
      // 进来的）。两条路通向同一批接口，不是两套实现。
      { view: 'approvals', icon: 'AP', title: '人工处置', subtitle: '审批 · 复核 · 运维' },
      { view: 'audit', icon: 'AU', title: '审计中心', subtitle: '查询执行全链路' },
    ],
  },
]
