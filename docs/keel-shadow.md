# askdb 影子接入 Keel

影子期内旧追踪、旧审计、旧评测继续写。下面三组数字对上之前，不删除 `trace.py`、`audit.py`、`quota.py`、`approvals.py`、`evalstore.py` 以及 `evals/`。

开关是环境变量 `KEEL_SHADOW=1`。未设置时行为与现在相同。密钥放部署环境，不进 git。

| 变量 | 作用 |
|---|---|
| `LANGFUSE_HOST` 或 `LANGFUSE_BASE_URL` | `https://jp.cloud.langfuse.com` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 对应环境的项目密钥。dev 用 `dev-keel`，staging 用 `staging-keel`，prod 用 `prod-keel` |
| `KEEL_TRACE_BUFFER_PATH` | 上报失败时的本地缓冲 |
| `KEEL_AUDIT_URL` / `KEEL_AUDIT_TOKEN` / `KEEL_AUDIT_SPOOL_PATH` | 审计镜像。影子期写失败不拒绝查询 |
| `KEEL_LLM_BASE_URL` / `KEEL_LLM_KEY` | 设了之后模型调用走薄网关，不再用厂商地址 |
| `KEEL_MANIFEST` | `deploy/keel-manifest.yaml`，用来挂上 `/v1/invoke` |
| `KEEL_ENV` | `dev`、`staging` 或 `prod` |

`/api/ask` 等原有接口保留。`/v1/invoke` 只在上述变量齐备时额外挂上。

内部的 router、verifier、synthesizer 不单独注册。它们的 span 类型是 `agent`，同一次问答共用一个 trace。

审计白名单是 `askdb.audit.SUMMARY_FIELDS`，已写入 manifest 的 `audit.captureFields`。状态三档：`ok`/`hit` → `ok`，`fallback`/`degraded`/`empty` → `fallback`，其余 → `failed`。

敏感表审批仍走现有挂起路径。`approval_policy` 迁到 keel-server 是 P3-1 的事。历史审计要先导出归档，再停旧库。

## 对比口径

从 `KEEL_SHADOW=1` 在目标环境打开时起，连续 7 天。口径在打开前写死，结束后不改。

1. **链路数。** 旧侧按审计记录的 `trace_id` 去重。新侧按 Langfuse observations 的 `traceId` 去重，同一 `askdb.trace_id` 只算一条。允许误差 1%：追踪上报失败时可以丢进缓冲，审计不允许丢。
2. **审计条数。** 按 `action` 计数。`action` 取旧记录的 `kind`，没有 `kind` 时记为 `ask`。新侧数 `KEEL_AUDIT_SPOOL_PATH` 里的行。误差为 0。
3. **评测分数。** 数据集 `askdb/nl2sql` 上同一批用例，逐条绝对差不超过 0.01，总分绝对差不超过 0.01。影子期内不实现完整的 `keel eval`。

一周结束后把三组成对数字填在下面。三组都在阈值内，才允许删除旧文件。

| 指标 | 旧 | 新 | 差 | 是否在阈值内 |
|---|---|---|---|---|
| 链路数（trace_id 去重） | | | | |
| 审计条数（按 action） | | | | |
| nl2sql 总分 | | | | |
