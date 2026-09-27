# askdb 生产环境多智能体测试报告

| 项目 | 结果 |
|---|---|
| 目标 | `https://askdb.ragforge.net/` |
| 执行时间 | 2026-09-25 21:24–21:38（Asia/Shanghai） |
| 工具 | Playwright + Headless Chrome |
| 范围 | 生产安全子集：只读查询、门禁、页面、协议暴露面与成本采集 |
| 汇总 | **14 PASS / 1 FAIL / 2 BLOCKED** |
| 结论 | **不满足多智能体原型验收条件：生产实例未启用多智能体编排** |

## 1. 关键结论

生产站基础浏览、认证、数据源范围、SQL Guard、移动端表格和单智能体查询均可用。显式提交 `mode=multi` 时，服务端返回：

```text
HTTP 409: 当前实例未开启多智能体编排，请在 multi_agent 配置中启用。
```

因此以下三项不能在该生产实例形成有效测试结论：

1. 多智能体执行路径与任务完成率；
2. 多智能体 Claim → Review → Evidence 引用完整性；
3. 多智能体 token、Worker、SQL、返工与成本预算。

这不是模型偶发失败，而是确定性的部署配置阻断。

## 2. 已执行结果

| ID | 场景 | 结果 | 证据摘要 |
|---|---|---|---|
| P-HTTP-01 | 首页与安全响应头 | PASS | HTTP 200；HSTS、nosniff、DENY 均存在 |
| P-UI-01 | 登录落地页与 favicon | PASS | 登录页正常；favicon 为内嵌 SVG，无 favicon 404 |
| A-ANON-01 | 匿名查询门禁 | PASS | `required=false`、`can_query=false`；匿名 `/api/ask` 返回 401 |
| P-UI-02 | 一键体验权限边界 | PASS | 可浏览；查询输入框禁用并提示登录 |
| P-AUTH-01 | 仿真 QA 账号 UI 登录 | PASS | 会话建立；角色 QA；生效行上限 200 |
| P-API-01 | 核心只读 API | PASS | health、sources、skills、audit、stats、tasks 全部 200 |
| P-DATA-01 | 数据源与 Schema | PASS | 选定源可读取 33 张开放表 |
| P-UI-03 | 主要页面导航 | PASS | 9 个主页面均可激活并渲染标题 |
| P-MOBILE-01 | 390px 任务/审计表 | PASS | 任务表 `364/556px`、审计表 `364/667px`，`overflow-x:auto` |
| D-FAST-01 | 直查 SQL Enter 提交 | PASS | 从默认源 Schema 动态取表/字段；HTTP 200，返回 1 行 |
| G-GUARD-01 | 写 SQL 拦截 | PASS | `DELETE` 被 R-02 拒绝，未执行 |
| G-SCOPE-01 | 跨当前源表访问 | PASS | 引用另一数据源表时被 R-03 拒绝 |
| D-SINGLE-01 | 显式单智能体问数 | PASS | `execution_mode=single`；1 Evidence、1 Review、1 Claim、1 SkillBinding |
| L-TRACE-01 | 审计可追溯 | PASS | 新查询 trace 可从审计列表检索，任务接口可用 |
| D-MULTI-01 | 显式多智能体复杂分析 | **FAIL** | HTTP 409：生产实例未开启多智能体编排 |
| B-EVIDENCE-01 | 多智能体 Claim/Review/Evidence | BLOCKED | 无 multi 结果可核验 |
| K-COST-01 | 多智能体预算与成本 | BLOCKED | 无 multi 计划及预算对象可核验 |

## 3. 单智能体成本基线

本轮只执行一次有效的自然语言单智能体基线，避免在生产重复消耗：

| 指标 | 实测值 |
|---|---:|
| 执行模式 | single |
| 耗时 | 4,985 ms |
| 输入 token | 12,901 |
| 输出 token | 134 |
| 总 token | 13,035 |
| 成本 | ¥0.008475 |
| SQL 结果行 | 1 |
| Attempts | 1 |
| Evidence / Review / Claim | 1 / 1 / 1 |

当前无法计算 multi/single 调用放大系数、复杂任务准确率提升、严格完成率或单位正确答案增量成本。

## 4. 生产环境观察

- 已登记 13 个运行时数据源，其中 12 个最近检查成功，1 个最近检查失败。
- Skill 列表当前只有 1 个条目。
- 测试时审计统计为 7,026 次调用，累计约 26,288,167 输入 token、918,016 输出 token、¥13.005212；这是站点窗口统计，不是本轮测试消耗。
- 浏览器没有 page error 或网络级 request failure。控制台出现的 401/409 与本轮匿名门禁和 multi 未启用测试一致。

## 5. 未在生产执行的用例

187 条设计用例不能直接全部打到生产。以下类型保留为未执行，而不是记为通过：

- 杀进程、断数据库、损坏 Checkpoint、跨副本取消等 Chaos 用例；
- 权限撤销、审批票过期、Skill 发布/停用/撤销/回滚等状态写入；
- 100 并发、24 小时 soak、超大 Evidence 和连接池压测；
- 未授权原始明细跨源组合和提示注入攻击；
- 450 题黄金集及每题 3～5 次真实模型重复评测；
- 需要 Fake LLM 精确注入非法协议、预算临界值和并发覆盖的单元测试。

这些用例应在隔离的候选发布环境执行。生产站在启用 multi 前，即使执行黄金问题也只能测到 single 或稳定的 409，不能代表多智能体准确性和完成率。

## 6. 发布建议

当前不应宣称多智能体功能已通过生产验收。后续顺序应为：

1. 确认生产期望模式，至少开启 `multi_agent.enabled` 并先使用 `shadow`；
2. 验证 shadow 后台确实产生 multi trace，并与 single trace 通过 `shadow_of` 关联；
3. 运行复杂单源小黄金集，先核验 Evidence、完成率和 30,000 token 硬上限；
4. 再执行完整 450 题 A/B、故障恢复和成本评测；
5. 全部 P0 门禁通过后，才从 shadow 逐步进入 assist/enforce。

