# 2026-10-25 AskDB / Keel 接入验收

已完成接入并发布 **staging v2**，底座状态 **ONLINE**，受管实例 **2/2 Ready**。最终正式平台测评 **24/24 通过、得分 1.0、失败条目为空**；12 个提示词均有真实 v1 调用证据，无本地提示词兜底。AskDB 最新主分支应用、前端、镜像、部署和原站点冒烟 CI 全部通过。

入口：[底座 AskDB 调试窗口](https://keel.ragforge.net/agents/askdb/debug)。原站点已使用同一个受管运行服务，旧 deployment 缩容保留用于回滚。完整业务审计保留在 AskDB PostgreSQL，底座记录调用审计与哈希链；底座集中审计当前不保存完整业务载荷。

最终结构化证据见 `2026-10-25-keel-acceptance.json`。

## 已执行

- Keel Java 23 项：独立内部凭据、用户名转发、候选配置、自检认证、护栏运行接口；通过。
- SDK 73 项：模型客户端、实际 token、结构化输出证据、桥接请求、评测与上下文预算；通过。
- AskDB 在不含开发 `.env` 的干净工作树运行完整回归，1349 通过 / 14 跳过，退出码 0，覆盖率 81.86%，超过 81% 门禁。首次在开发目录运行暴露出 .env 重新注入真实元库的干扰，已改为专用 UTF-8 PostgreSQL 测试库与干净工作树；没有把开发库混进测试。另修正已有 telemetry 测试在 FastAPI 不提供该接口时的兼容断言。
- 后续提示词/工具审计/模型/健康配置等定向 41 项通过，运行入口与多智能体回归 135 项通过。完整回归基线为 f5a87bb；后续小修单独定向验证。
- linux/amd64 治理镜像构建通过。容器内实际只读 SQL 返回 94976；租户护栏、受保护的 SDK invoke、自检 final、未认证 401 冒烟通过。
- 集群中以临时独立进程运行新代码，调用真实 keel-llm 与合成 DuckDB，移除进程的厂商 Key：14/14 用例通过。它没有替换正在服务的 askdb Deployment，没有向生产数据源发查询。诊断使用 KEEL_LOCAL_PROMPTS=1，所有有模型调用的结果都标记 prompt_fallback，不作为正式发布门禁。
- Langfuse v2 observations 读取同一 trace 返回 28 个 observation，含 AGENT / GENERATION / GUARDRAIL / TOOL；账单接口返回 46 个 SETTLED 尝试、14 个 trace，诊断总计 0.055442 元。详情见 `2026-10-25-keel-runtime.json`。
- keel-audit 返回该调用的 ask、ask.result、tool.call 等元事件。现有服务会将 payload 清空，控制台到该服务的查询及持久化尚未验收；业务完整审计仍由 AskDB 原审计保留。不能声称底座集中审计 payload 和持久化已完成验收。

## 修复与环境变更

1. 网关有虚拟 Key 时不再先要求旧厂商 Key；严格模式的模型统一经过 ctx.llm。
2. 修复线程桥接遇到真实 TimeoutError 时持续等待的问题；加入模型时限与步骤限制。
3. 把方法论追加块纳入命名提示词模板，保留完整模板和动态变量；相邻 SQL 规则变量合并以避免歧义。
4. 真实 Qwen 思考模式返回额外括号导致工具参数 JSON 无效。已把 askdb-staging / askdb-prod 的关闭思考配置合并到网关 request-defaults，并成功滚动更新两副本网关；保留其他智能体条目。原配置备份于节点 `/tmp/askdb-request-defaults-before.json`。结构化格式错误最多重试一次，授权/预算等错误不按格式错误重试。
5. 评分器逐行核对实际表格，允许额外核对列；SQL 或叙述里出现答案不计通过，调换分组计数也不计通过。
6. 使用实际 StepTrace 对象补测调试 SSE，准确保留节点名称与 ok/fallback/failed 状态；原生调用的 SDK 网关与语义追踪共用实际 OTel trace_id，旧业务 ID 留作关联。
7. 早期诊断结束后测试 Key 均已 block。正式注册随后由底座重新开通并轮换 `askdb-staging`，预算 30 元/日；早期 `askdb-verify-20261025-staging` 保持禁用。未将凭据写入仓库。

## 验收顺序（已闭环）

部署正式治理运行实例 → 导入数据集 → 平台 Job 完整评测及门禁 → 页面真实调试/追问/拒绝/链路/审计/账单验证 → 受控发布记录与回滚配置核验。原生功能和历史审计保留，旧运行实例缩容留作回滚，未删除旧实现。

### 正式验收范围

SDK 使用正式发布 `sdk-v1.0.21`，来源主仓合并提交 `aa107ee5b23e939cb3bbdac5e2cbdd26f31ea071`。运行时统一在 `agents/askdb`，现有站点与底座协议共享提示词、网关和链路。业务数据库、角色、数据源、会话签名保持原配置；旧 deployment 在健康切换后缩容保留用于回滚。

24 条评测由 14 条完整问数用例和 10 条领域结构化契约用例组成。后者仅在经过平台凭证鉴权且含 eval run id 的隔离评测路径启用，使用原始领域 Pydantic schema、对应受管提示词和真实网关。SQL/fast 输出经过原有 SDK 工具及 SQL 安全护栏，在合成租户 65 上执行并核对 94976；其余验证规划、已完成重规划、证据充分性、最小任务拆分、完成决策、统一口径、核验与证据合成。每个提示词的发布证据必须来源实际模型调用。

### 首次正式在线结果与修复

- 应用主分支 `e56091e79db24c7b383261fc952ea4cfc4a1a10c` 的 CI 全部通过：应用、前端、镜像构建、部署、原站点冒烟。受管实例 2/2 Ready，原 `askdb/askdb` 缩容为 0 保留。节点 BusyBox 镜像拉取阻塞动态卷创建，已为 AskDB 的独立 2Gi 卷建立绑定，回收策略 Retain；未改变其他应用的卷。
- 底座自检全部通过。独立 Chrome 从调试页真实发问，HTTP SSE 200，无页面错误，返回 26 张开放表的说明和步骤；链路 `051a8b94095b7dd7955834d6ab69573b`。
- 同一平台会话两轮实际查询知识库表，总行数与非空主键计数均为 206；第二轮未重述表名，沿用上下文成功。两轮均 `prompt_fallback=false`，实际使用 intent/query-agent v1。注入输入被 `GUARD_INJECTION_BLOCKED` 拦截，会话保存正常问答。
- 底座审计验证：27 条记录，`intact=true`。首次读取链路遭遇 Langfuse 读限流 429，稍后同一控制台接口 200 恢复；有实际节点数据。
- 首次正式平台评测 `ev_87ac2f8e29c84bfdbecdc24167bbdfe4` 完整运行 25 条，12 个提示词实际版本均为 1，但门禁失败，未发布。失败包括：合成回答的正确数字 `94,976` 被字符串 `94976` 判错；自动创建的 `keel-builtin` 协议探针 `probe / probe ok` 混入问数集，成为额外未标记用例。业务问数、安全和其他领域契约均通过。
- 修复仅增加正确的数字格式匹配（测试仍拒绝 194976、949760、-94976、94976.1），并提供 `python -m evals.prepare_dataset`：导入本仓 24 条用例，只归档元数据明确为 `keel-builtin/protocol` 且输入输出完全匹配的自动探针。用户用例与历史保留，不降低门禁。

### SDK 评分器发现协议补测

第二轮 `ev_4b1272203aac4b888a5db10d8db66a1a` 在门禁阶段报 `KEEL_EVAL_ERROR ValueError`，未发布。新增的数字辅助函数是公开函数，SDK 会将评分文件中的所有公开函数作为三参数评分器加载，造成调用契约不匹配。改为私有 `_contains`，新增通过真实 `keel.eval.run.load_scorers` 加载并执行的回归测试，确保只加载 answer_correctness，格式化正确数字得 1、错误数字得 0。16 项接入测试通过，24 条数据集评分契约逐条可调用。最终 CI 和真实平台测评仍须通过后才发布。

### 问数质量与采样参数

第三轮 `ev_c4f8af60cac04c46815a873ba4b3c587` 完整运行 24 条，得分 0.983333，12 个提示词均为 v1，达到原有 0.95 门槛并发布 staging v1（镜像 f5b1b1b2a265）。结果仍有一条 stage 分组问题：模型未调用 SQL 就生成了估算数字，被 NO_EVIDENCE 拦截，没有把错数交给用户。继续检查发现 SDK 迁移遗漏原 LlmClient 的 temperature 参数，未配置远端 sampling 时改用了模型默认值。修复为保留原 AskDB temperature，并优先使用底座提示词显式参数；复制提示词引用，避免修改缓存的版本化配置。增加 0、非零配置和远端覆盖三组实际 SDK 桥接断言。


## 最终验收

- 发布：staging v2，实际镜像 `askdb:bcdc05439903`，源码 `bcdc05439903680363a645f151e4ec2e0a02e8ea`。最终 [CI 40189423924](https://github.com/dezhiguan/askdb/actions/runs/40189423924) 的全部阶段成功。
- 正式评测：`ev_dd17bc2ce54d4a0b9bb6c13d6c52ae0b`，完整 24 条、得分 1.0、`newFailures=[]`，所有分组和契约维度通过，12 个提示词实际版本均为 1。通过后才调用平台发布 API，返回 v2 / gatePassed=true。前几轮失败与 v1 历史均保留。
- 采样修复在隔离验证进程中对先前失败的 stages 问题连续运行 3 次，全部真实 SQL 执行与四组结果核对通过；正式实例最终全量测评再次覆盖并通过。隔离进程只使用合成配置，没有修改服务进程或生产数据。
- 新版本真实浏览器调试：HTTP SSE 200、无页面异常、实际结果表返回，`askdb.ok=true`、`prompt_fallback=false`，链路 `8b4b9f24a7e6e7e1f4975d4bb1d1e00b`。此前已验证追问、会话持久化、注入拦截和独立链路页面。
- 最终平台审计哈希链：140 条，intact=true。两轮业务查询分别在 AskDB PostgreSQL 保存 2 条审计，问题、执行尝试与结果字段均在。底座集中载荷能力限制由原业务审计补足。
- 网关：独立 askdb-staging Key ACTIVE，日预算 30 元；当前 SDK 1.0.21，严格模式使用受管网关和平台提示词，没有依赖厂商 Key。完整代码与环境变更见 Keel PR 36、AskDB PR 3/4/5/6。
- 运行：agents/askdb 2/2 Ready，原 askdb/askdb 0/0 保留，AskDB 独立 2Gi PVC 已绑定并保留；公网原站点健康与部署后冒烟通过。未执行生产环境标签推广。
