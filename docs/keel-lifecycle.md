# AskDB 在 Keel 上运行

本次接入按 `docs/specs/keel-lifecycle.md` 实施。根目录 `agent.yaml` 是平台清单，原生 Web 服务同时提供 SDK 的标准协议。`KEEL_ENABLED=1` 开启严格治理模式；缺模型、审计、桥接身份或环境配置时启动失败。`KEEL_SHADOW=1` 仅兼容原影子迁移，不能作为完整接入的开关。

## 能力归属

| 能力 | 执行路径 |
|---|---|
| 注册、候选、自检、调试、评测、发布、回滚、退役 | 既有 Keel 控制台/API；不建立另一份平台生命周期数据库 |
| 模型、逻辑路由、授权、日预算、结算 | `ctx.llm` → keel-llm，业务进程不需要厂商 Key；保留实际 token 供业务步骤预算使用 |
| 提示词 | `ctx.prompt` 读取环境版本；12 份完整模板的 Git 副本位于 `prompts/`，generation 关联实际版本 |
| 工具 | 四个只读业务工具经 `ctx.tools.call` 执行，使用底座的工具审计、追踪与输出护栏，再执行 AskDB 的 SQL 护栏与只读执行器 |
| 会话上下文 | 使用平台传入的用户隔离历史和 SDK ContextWindow，复用摘要/检索/上下文预算；评测不读普通会话历史 |
| 链路 | 同一次调用共用 SDK tracer，内部 router/verifier/synthesizer 等保持 agent span，查询节点送到调试 SSE |
| 审计 | SDK 白名单审计上报、持久缓冲；旧业务审计仍保留，影子期不删除历史或本地业务审批 |
| SQL、库表权限、脱敏、租户、业务口径、数据源登记、任务检查点与续跑 | 保留 AskDB 领域实现，平台用户按经过认证的 username 映射本地角色，只能收窄权限 |
| 评测数据 | `evals/keel-seed.jsonl`，固定合成库的 14 条计算/分组/安全题；评分必须匹配返回表格的行，SQL 里出现答案不算通过 |

生产/预生产不静默使用本地提示词。`KEEL_LOCAL_PROMPTS=1` 只用于接入诊断；这类调用在最终证据中标记 `prompt_fallback`，不能通过正式发布门禁。

## 构建与部署

1. `.keel-sdk-ref` 固定本次平台源码；`scripts/build_keel_image.sh <registry>/askdb:<revision>` 可从该提交构建 wheel 和镜像。手工构建时，在匹配本次平台变更的 Keel SDK checkout 执行 `uv build --wheel --out-dir /tmp/askdb-sdk-wheel sdk-python`，将 wheel 复制到本项目 `wheel/`。不要从公共 PyPI 安装同名 SDK。正式发布应使用包含通用 `KEEL_BRIDGE_TOKEN` 和结构化 token 返回能力的正式 SDK wheel。
2. 构建 `docker build --platform linux/amd64 -f Dockerfile.keel -t <registry>/askdb:<revision> .`。治理镜像使用 Langfuse v4 评测能力；旧 v2 旁路留在 `legacy-observe` extra，严格治理模式不再调用旧 Langfuse ingestion。
3. 从 askdb 命名空间复制 `askdb-db`、`askdb-auth`、`askdb-sources`、按需 `askdb-redis` 到 `agents`，保留数据源白名单；不复制 `askdb-llm` 厂商密钥。生成独立 `agents/askdb-bridge` Secret（key=`token`），同一值配置到 `keel-system/keel-server` 的 `KEEL_ASKDB_TOKEN`。
4. 将 `deploy/keel-model-defaults.json` 的 askdb 两个别名条目合并进模型网关的 `request-defaults` Secret，保留其他智能体的条目。Qwen 的强制结构化调用需要关闭思考；DeepSeek 同样由网关处理厂商参数。修改后滚动更新网关。
5. 底座部署本次桥接凭据和 username 修复；注册 staging，底座会生成 `agents/keel-askdb`。`deploy/k8s/askdb-keel.yaml` 挂载此 Secret 和持久卷，Pod 的 agent/env 标签用于平台评测 Job 选择实际镜像。启动与注册自检可能需先创建运行 Pod、注册后再重启 Pod以读取生成凭据。
6. 使用有效控制台会话操作 `scripts/keel_lifecycle.py --url ... --cookies ... register`，随后 `sync-prompts`（需先提交模板）、通过 SDK `keel eval import --file evals/keel-seed.jsonl` 导入数据集，再由 `evaluate` 启动平台 Job。评测 Job 使用自己的 AskDB `KEEL_BRIDGE_TOKEN`，绝不能注入元智能体的凭据。
7. 在调试窗口验证真实默认数据库查询、同会话追问和拒绝路径，核对模型账单、generation 的提示词版本、工具节点和审计。完整门禁通过后，通过已有评测发布入口将实际镜像与提示词快照发布，不手工移动 production 标签。

评测服务只读取 `config/keel-eval.yaml` 和镜像内合成库，使用独立检查点/审计路径。每次用新 thread_id，不访问生产数据源注册表。正式候选的提示词、数据集、镜像必须能在平台页面核查。测试用的平台账号还需要有对应 AskDB 查询角色；显示名不作为权限依据。

## 尚需通过目标环境验收的项目

本地/真实依赖的结果写入单独验收记录。`docs/keel-shadow.md` 的七天成对对账仍然适用；未通过之前不删除旧审计、追踪、审批及评测。平台自身尚未配置的持久审计/嵌入模型能力不能假定已具备，必须核查实际部署与返回证据。
