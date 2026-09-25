# 多智能体实现与原型验收对照

对照基线：`docs/design-multi-agent-orchestration.html`。本文件记录代码已经提供的能力，
并把仍需真实环境或平台基础设施才能完成的项目单独列出，避免把设计目标当成测试结论。

## 分阶段提交

| 阶段 | 提交 | 交付 |
|---|---|---|
| 0 | `5722ad0` | 多智能体与 Skill 设计原型 |
| 1 | `5f54f92` | TaskPlan / Evidence / Review / Claim 等协作协议与单 Agent 兼容输出 |
| 2 | `c45d60f` | Skill Manifest、注册表、解析、依赖/冲突、版本和发布门禁 |
| 3 | `fe35e33` | LangGraph 动态 fan-out、并行 Worker、Verifier、局部返工和 Synthesizer |
| 4 | `ec0bcb0` | Router、Checkpoint 恢复、API、配置和运行时接入 |
| 5 | `f1be94f` | 查询协作面板、Evidence/Claim/Skill 展示和 Skill 中心 |
| 6 | 本阶段提交 | 原型差距补齐：完整 shadow、跨源契约、恢复鉴权、Skill 固定/回滚、取消闭环 |

## 原型逐项对照

| 原型能力 | 状态 | 实现位置 / 说明 |
|---|---|---|
| 单 Agent 快路径 + 复杂任务动态路由 | 已实现 | `multiagent/router.py`；`auto/single/multi` 均可用，简单问题不启动多 Agent |
| 五类职责与动态 Worker | 已实现 | Supervisor、Semantic、Query Worker、Verifier、Synthesizer；Worker 由 LangGraph `Send` 按计划创建，不常驻内存 |
| 结构化黑板，不让子 Agent 自由聊天 | 已实现 | `MultiAgentState` 和 Pydantic 协议对象；按 ID reducer 合并并行结果 |
| Worker 复用现有 SQL 安全原子 | 已实现 | `query_worker.py` 只调用现有 `search_schema` / `execute_sql`，每个源使用已收窄 Config |
| Evidence-first 与 Claim 引用 | 已实现 | Evidence 含 SQL、数据源、时间、校验和、scope；正置信 Claim 必须绑定已验证 Evidence |
| 失败子任务局部返工 | 已实现 | Verifier 生成 RepairTask，仅重派目标 Worker；旧 Evidence 由 `supersedes` 保留 |
| Checkpoint 与冷恢复 | 已实现 | 复用现有 SQLite/PostgreSQL saver；状态只保存协议数据，运行依赖在恢复时重建 |
| 恢复前重新授权 | 已实现 | 保存并比较 source scope fingerprint；权限、白名单、脱敏或 Guard 变化时拒绝旧现场 |
| Skill 注册、解析和最小权限 | 已实现 | 角色/source/trigger 发现，依赖、冲突、循环和缺失依赖 fail-closed；Tool 取权限交集 |
| Skill 生命周期 | 已实现 | draft → test → shadow/published → disabled/revoked，并支持版本回滚 |
| Skill 版本固定和 Trace 留痕 | 已实现 | Checkpoint 保存 id/version/checksum/reason；恢复/返工按固定版本重载，checksum 不一致或 revoked 拒绝 |
| 旧 YAML Skill 兼容 | 已实现 | 自动映射 `builtin.legacy_rules@1.0.0` |
| Agent shadow | 已实现 | shadow 模式后台运行完整多 Agent 图，用户仍收到单 Agent 结果；独立审计并关联 `shadow_of` |
| 跨源默认拒绝 | 已实现 | 默认关闭；开启后每一对 source 必须存在 aggregate-only `JoinContract`，否则 P12 fail-closed |
| 跨源不搬原始明细 | 已实现 | 各 Worker 只在自己的 source 内执行，跨源层只把独立 Evidence 与批准契约交给合成器，不生成跨库原始行 Join |
| 任务取消 | 已实现（单进程协作） | 发起人可从任务中心取消；Checkpoint 写入 CANCELED，未开始 Worker 不再执行，合成期取消会丢弃答案且不可续跑 |
| 前端协作可视化 | 已实现 | 执行计划、Agent 状态、Skill 版本、Review、Claim → Evidence、SQL/source/as-of 可展开查看 |
| Skill 管理页 | 已实现 | 创建、测试、shadow、发布、停用、撤销、回滚与解析预览 |

## 本轮原型比对后补齐的遗漏

1. 原 shadow 只有路由判定，没有真正运行完整图；现已改为有界后台池执行完整图，并关联主 trace。
2. 原恢复只核对 source id；现增加 scope fingerprint，权限收回后不能借旧 Checkpoint 续跑。
3. 原跨源只有开关；现增加 JoinContract 并对每个 source pair 做聚合级 fail-closed 校验。
4. 原 Skill 发布后可更新，返工时可能漂到新版本；现从 Checkpoint 重载精确版本与 checksum。
5. 原 Skill 生命周期缺少回滚；现补后端、API 和 Skill 中心入口。
6. 原图内有 CANCELED 枚举但无闭环；现补运行时信号、持久终态、归属校验、API、任务中心和回归测试。

## 仍需上线阶段完成的验收

以下不是仓库内单元测试可以替代的结论：

- “复杂黄金集显著优于单 Agent”必须用目标数据源、真实模型和固定黄金集做 A/B；当前只提供 shadow 审计数据。
- 当前 Skill 管理存储默认是原子 JSON 文件，适合单机/开发；平台多副本应实现 PostgreSQL Store 适配器后再开放多人编辑。
- 取消信号对当前进程立即生效，Checkpoint 终态可跨重启；多副本部署若请求可能落到另一副本，应把即时取消信号迁到 Redis/PostgreSQL 通知通道。
- `JoinContract` 当前由部署配置预批准；需要“每次跨源计划由数据负责人临时审批”时，还要把 P12 接入现有 approvals 人工队列。
- 大结果 Evidence 当前仍随 Checkpoint 保存；达到平台级大结果规模时，应增加对象存储并只在状态中保留引用。

## 验证命令

```bash
.venv/bin/python -m pytest --no-cov tests/test_multiagent_protocol.py \
  tests/test_skill_registry.py tests/test_supervisor_graph.py \
  tests/test_multiagent_runtime.py tests/test_tasks.py tests/test_server.py \
  tests/test_agent_runtime.py -q

cd frontend
npm run lint
npm run build
```

本机验证结果：多智能体定向测试、本地非 PostgreSQL 套件、前端 build/lint 均通过；
lint 只有仓库既有告警。完整 `pytest` 已执行到结束，其中需要真实 PostgreSQL 的
集成用例因未设置 `ASKDB_TEST_SOURCES_DSN` 按项目约定主动报错，不能在本机宣称通过。
真实模型黄金集与多副本取消同样属于部署验收，不伪造通过结果。
