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
2. **不接模型。**
   `config/public.yaml` 显式声明 `llm.disabled: true`，`api_key_env` 指向一个
   永不设置的变量名 —— 即使部署机上恰好有 `DASHSCOPE_API_KEY`，也不会误开。
   理由：开放实例无法区分调用方，接了模型等于任何人都能花部署方的钱。

**直查 SQL 这条链路（护栏 → 强制改写 → 干跑 → 只读执行）本来就不调模型**，
一个 token 都不花，而它恰好是这个项目最要紧的部分。

> 绝不要把 `ragforge-prod.yaml` 或任何指向真实库的配置用于对外实例。

---

## 一次性准备

### 1. 证书 —— 唯一的硬前提

现有证书是 `/etc/nginx/ssl/ragforge/www.ragforge.net.pem`，**大概率只签了
`ragforge.net` 与 `www.ragforge.net`**，不含子域名。上线前必须先确认：

```bash
openssl x509 -in /etc/nginx/ssl/ragforge/www.ragforge.net.pem -noout -text \
  | grep -A1 "Subject Alternative Name"
```

- 若已包含 `*.ragforge.net` → 直接用现有证书，无需改动
- 若不包含 → **先申请通配符证书**，或单独为 `askdb.ragforge.net` 签一张，
  再把 `deploy/nginx-askdb.conf` 里的证书路径改过去

这一步没有捷径。证书不匹配时浏览器会直接拦下，不是"能用但有警告"。

**若暂时拿不到证书**，可改走子路径 `https://ragforge.net/askdb/`，
复用现有证书、不动 DNS。代价：前端有 6 处绝对路径 `fetch("/api/...")`
需要改成相对路径，否则子路径下会打到根域名去。

### 2. DNS

`askdb.ragforge.net` A 记录指向 **Server 2**（nginx 入口所在机器），
与 `ragforge.net` 同一个地址。

### 3. GitHub Secrets

复用 CareerMate 那套 SSH 脚手架，不新增登机凭据：

| Secret | 说明 |
|---|---|
| `CAREERMATE_APP_SSH_KEY` | Server 3 私钥（已有） |
| `CAREERMATE_APP_HOST` | Server 3 地址（已有） |
| `CAREERMATE_INGRESS_HOST` | 跳板机，默认 `8.163.63.222`（已有） |
| `ACR_REGISTRY` / `ACR_USERNAME` / `ACR_PASSWORD` | 镜像仓库（已有） |

**askdb 不需要任何新 secret** —— 它不连数据库、不接模型。

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
