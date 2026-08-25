# askdb 对外开放实例 · 部署说明

| | |
|---|---|
| **拟制日期** | 2026-08-13 |
| **版本** | V1.0 |
| **目标** | 与 CareerMate 同机（Server 3 的 k3s），经 Server 2 的 nginx 以 `askdb.ragforge.net` 对外 |

---

## 安全边界（先看这一节）

askdb **不设账号体系** —— 设计文档 §1.1 写明"数据库连接本身即权限边界"。
这意味着**能访问到服务端口的人 = 有权查那个库的人**。因此对外开放实例
必须同时满足两条，缺一不可：

1. **连的是合成样例库，不是任何真实数据源。**
   `data/sample.duckdb` 由 `data/seed.py` 用固定随机种子在**构建期**生成，
   10.4 万行，没有一条真实数据。镜像里不含任何生产连接串。
2. **接了模型，但用四层限制兜住成本。**
   开放实例无法区分调用方 —— 接了模型就等于任何人都能花部署方的钱。
   任何单独一层都不够，四层叠起来才成立：

   | 层 | 做法 | 挡什么 |
   |---|---|---|
   | 便宜的模型 | `deepseek-v4-flash`，约 ¥0.0009 / 次 | 单价 |
   | 每日配额 | `daily_quota: 500`，**按模型调用次数计** | 总量 |
   | 共享计数 | 计数存 Redis（`ASKDB_REDIS_URL`） | 多副本各算各的 |
   | 入口限流 | nginx 对 `/api/ask` 限 6r/min per IP | 短时间刷光当天额度 |

   配额扣在 `LlmClient` 里，一次调用扣一次 —— **不是一次提问扣一次**。
   一次提问会调好几次模型（多步规划每步一次生成 + 一次评估，反思重试再各来
   一次），按提问计会低估花费好几倍。超限时拦在调用之前，一个 token 都不花。

**直查 SQL 这条链路（护栏 → 强制改写 → 干跑 → 只读执行）本来就不调模型**，
一个 token 都不花，而它恰好是这个项目最要紧的部分。

> 绝不要把 `ragforge-prod.yaml` 或任何指向真实库的配置用于对外实例。

---

## 一次性准备

### 1. 证书 ✅ 已完成（2026-08-13）

单独签发的 DV 证书已上传，SAN 覆盖 `askdb.ragforge.net` 与
`www.askdb.ragforge.net`，有效期至 **2026-11-10**。

```
宿主机 /data/ssl/ragforge/askdb.ragforge.net.{pem,key}
  → 容器 /etc/nginx/ssl/ragforge/
```

nginx 跑在 `ragforge-nginx` 容器里（`docker-compose-ingress.yml`），
证书目录是挂进去的，所以要放在**宿主机** `/data/ssl/ragforge/`，
不是容器内路径。私钥权限 600。

> **90 天到期，须记得续签。**主域名那张 2026-09-05 到期，比这张还早。

### 2. DNS ✅ 已完成（2026-08-13）

`askdb.ragforge.net` A 记录 → `8.163.63.222`（Server 2，与 `ragforge.net` 同址）。

### 3. GitHub Secrets

复用 CareerMate 那套 SSH 脚手架，不新增登机凭据：

| Secret | 说明 |
|---|---|
| `CAREERMATE_APP_SSH_KEY` | Server 3 私钥（已有） |
| `CAREERMATE_APP_HOST` | Server 3 地址（已有） |
| `CAREERMATE_INGRESS_HOST` | 跳板机，默认 `8.163.63.222`（已有） |
| `ACR_REGISTRY` / `ACR_USERNAME` / `ACR_PASSWORD` | 镜像仓库（已有） |

**askdb 不需要任何新的 GitHub secret** —— 它不连数据库，登机与推镜像都复用上表。

模型密钥与 Redis 地址走 **k8s Secret**，由运维一次性创建，不经过 GitHub：

```bash
# 模型密钥（必需，缺了 Pod 起不来）
kubectl -n askdb create secret generic askdb-llm \
  --from-literal=DEEPSEEK_API_KEY=... \
  --from-literal=DASHSCOPE_API_KEY=...

# 配额共享计数（可选；不建则退回本地文件计数，单副本下依然正确）
kubectl -n askdb create secret generic askdb-redis \
  --from-literal=ASKDB_REDIS_URL='redis://:口令@172.25.90.183:6379/2'

# 调用链观测（可选，二选一；不建则审计页如实显示"未接入"，主链路零影响）
# 首选：自托管 Langfuse（部署在数据机 /opt/langfuse，docker compose）。
# 注意数据机安全组需放行 3000 端口（源 = 172.25.90.184/32，即 k8s 节点）。
kubectl -n askdb create secret generic askdb-langfuse \
  --from-literal=LANGFUSE_HOST=http://172.25.90.183:3000 \
  --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-... \
  --from-literal=LANGFUSE_SECRET_KEY=sk-lf-...

# 备选：LangSmith 云（仅在部署环境能出海时可用；228 机房实测不通）
kubectl -n askdb create secret generic askdb-langsmith \
  --from-literal=LANGSMITH_TRACING=true \
  --from-literal=LANGSMITH_API_KEY=lsv2_... \
  --from-literal=LANGSMITH_PROJECT=askdb-prod
```

建完用 `curl -s https://askdb.ragforge.net/api/health | jq .quota` 确认
`backend` 是 `redis`、`multi_replica_safe` 是 `true`。若显示 `file`，说明
Secret 没生效 —— 此时**不要**把 replicas 调大于 1，配额会变成 N 倍。

---

## 部署

### 应用（自动）

推 `main` 即触发 `.github/workflows/ci-cd.yml`：
测试 → 构建镜像（tag 取 commit sha）→ 推 ACR → 经跳板机 SSH 到 Server 3 →
`kubectl apply` → 等待 rollout → 冒烟。

镜像 tag 用 commit sha 而非 `latest`：出问题时能确切知道线上跑的是哪次提交，
回滚也只是把 tag 改回去。

### 入口（手动一次）

`deploy/nginx-askdb.conf` 的内容要**合并进 rag-forge 仓库的 `nginx.conf`**，
不要单独在服务器上放文件 —— Server 2 的入口配置由 rag-forge 的 CI 统一推送，
单放的文件下次部署就被覆盖了。

合并后推 rag-forge 的 main，其 CI 检测到 `nginx.conf` 变化会自动推送并 reload。

---

## 验证

```bash
curl -s https://askdb.ragforge.net/api/health | python3 -m json.tool
```

应看到 `datasource.type = duckdb`、`llm.disabled = true`。

护栏是否生效：

```bash
curl -s -X POST https://askdb.ragforge.net/api/sql \
  -H 'Content-Type: application/json' \
  -d '{"sql":"DELETE FROM documents"}' | python3 -m json.tool
```

应返回 `200` + `ok: false` + `rejected_by: "R-02"` —— **护栏拒绝不是 5xx**，
这是接口的既定约定（见 `server.py` 开头）。

内省与自检端点应被 nginx 挡在外面：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://askdb.ragforge.net/api/introspect   # 期望 404
```

---

## 回滚

```bash
# 在 Server 3 上
kubectl -n askdb rollout undo deployment/askdb
kubectl -n askdb rollout status deployment/askdb --timeout=120s
```

nginx 侧回滚：还原 rag-forge 仓库的 `nginx.conf` 并重推。

---

## 运维要点

- **审计日志**在容器内 `/app/data/audit-public.jsonl`，随容器重建丢失。
  开放实例上这是可接受的 —— 那里没有真实数据，留痕只为观察用量。
  真要留存就挂个卷。
- **限流**在 nginx 层（5 r/s，突发 10）。askdb 自身没有限流能力，
  因为它原本假设访问者是可信的。
- **资源**：requests 50m CPU / 192Mi，limits 500m / 512Mi。
  样例库 5MB 级，DuckDB 常驻内存很小。
