# askdb 对外开放实例 · 部署说明

| | |
|---|---|
| **拟制日期** | 2026-08-13 |
| **版本** | V1.1（2026-09-02 修订） |
| **目标** | 与 CareerMate 同机（Server 3 的 k3s），经 Server 2 的 nginx 以 `askdb.ragforge.net` 对外 |

| 版本 | 日期 | 修订内容 |
|---|---|---|
| V1.0 | 2026-08-13 | 首版：证书、DNS、k8s、入口、冒烟 |
| V1.1 | 2026-09-02 | 独立前端工程上线的配套：会话密钥 Secret、前端发布链路与 CI 关卡、静态资源缓存与 gzip、验证一节按线上现状纠偏 |

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
   | 便宜的模型 | `qwen3.8-flash`，约 ¥0.0043 / 次 | 单价 |
   | 每日配额 | `daily_quota: 12000`，**按模型调用次数计** | 总量 |
   | 应答缓存 | `answer_cache.ttl_seconds: 600`，命中即零调用 | 同一个问题被反复问 |
   | 共享计数 | 计数存 Redis（`ASKDB_REDIS_URL`） | 多副本各算各的 |
   | 入口限流 | nginx 按路径三档（见下表） | 一个人刷光全站额度 |

   2026-09-09 按日访问量 10 万级重配（`deploy/nginx-askdb.conf`）：

   | 路径 | 速率（per IP） | 突发 | 为什么是这个价 |
   |---|---|---|---|
   | 静态资源 + 只读接口 | 30 r/s | 120 / 60 | 一次冷加载就要 11 个请求，给窄了是自己挡自己 |
   | `/api/sql` | 2 r/s | 20 | 一条真打到生产库的语句 + sqlglot 解析 |
   | `/api/ask` | **20 r/min** | 10 | 唯一花钱的路径 |

   > 上一版这里写的是"`/api/ask` 不再做每 IP 限流"（2026-08-25 运营决定）。
   > 两处需要更正：
   >
   > 1. **那句话当时就与实际行为不符。** nginx 的 `limit_req` 只在本层一条
   >    都没写时才继承上层；那个 location 里一条都没写，所以它一直在吃
   >    server 块的 5 r/s，而不是"不限"。
   > 2. 额度从 500 提到 12000 之后，"单人刷光全天额度"的代价从几块钱变成
   >    几十块 + 全站当天不可用，所以这一档必须显式存在。现在两条
   >    `limit_req` 都写在 location 里，行为与文档一致。

   `¥0.0011/次` 那个旧口径也不能再用了 —— 它是样例库 + keyword 召回 +
   `token_budget: 1500` 时候的数。现在 `schema_rag` 是 vector 召回、top_k 8、
   预算 4000，注入的 schema 本身就能到 4000 token，实际约 ¥0.0043/次。

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

**askdb 不需要任何新的 GitHub secret** —— 登机与推镜像都复用上表。
（2026-09-03 起对外实例直连 ragforge 生产主库，但库口令走 k8s Secret，不经 GitHub。）

模型密钥与 Redis 地址走 **k8s Secret**，由运维一次性创建，不经过 GitHub：

```bash
# 模型密钥（必需，缺了 Pod 起不来）
# 数据库口令。**必需** —— manifest 里是 optional: false，缺了 Pod 会卡在
# CreateContainerConfigError。这是有意的：连不上库的实例本来就不该起来，
# 设成 optional 会让它照常起、每次查询报"数据源不可用"，排查方向完全跑偏。
#
# 走**内网**地址：实测 Server 3 的 pod 到 8.163.30.216:5432（公网）超时，
# 到 172.25.90.183:5432（同 VPC）通。
kubectl -n askdb create secret generic askdb-db \
  --from-literal=ASKDB_PROD_PG_PASSWORD='<askdb_ro 的口令>'

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
  --from-literal=LANGFUSE_PUBLIC_URL=http://localhost:3000 \
  --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-... \
  --from-literal=LANGFUSE_SECRET_KEY=sk-lf-...
# PUBLIC_URL 是审计页"观测"链接的跳转地址：自托管实例仅内网可达，
# 浏览器侧先挂隧道（ssh -f -N langfuse，见 ~/.ssh/config）再点链接。

# 备选：LangSmith 云（仅在部署环境能出海时可用；228 机房实测不通）
kubectl -n askdb create secret generic askdb-langsmith \
  --from-literal=LANGSMITH_TRACING=true \
  --from-literal=LANGSMITH_API_KEY=lsv2_... \
  --from-literal=LANGSMITH_PROJECT=askdb-prod

# 登录与一键体验的会话签名密钥（**独立前端的登录能力依赖它**，2026-09-02 新增）。
# 缺了不会报错：服务照常起、页面照常开，只是登录整体关闭、前端连入口都不显示
# （askdb/auth.py 的 enabled() = 配置里开 且 这把密钥在）。冒烟里有断言兜住。
# 两个副本必须共用同一把：票是无状态签名，各拿各的密钥表现为"刷新几次就掉线"。
kubectl -n askdb create secret generic askdb-auth \
  --from-literal=ASKDB_SESSION_SECRET="$(python -m askdb.cli session-secret | cut -d= -f2)"
```

### 数据源元数据库（2026-09-06 起必配）

数据源不再存在 `var/sources/*.yaml`，改存 PostgreSQL 的 `askdb_sources` 表。
连接串**只从环境变量来，不进配置文件** —— 这是"改数据源不用重启、数据源
出问题不影响启动"这条要求的落点：启动配置里再没有任何一条数据源。

| 环境变量 | 缺省 | 缺了会怎样 |
|---|---|---|
| `ASKDB_SOURCES_DSN` | 回落到 `ASKDB_IDENTITY_DSN` | 数据源相关接口一律 **503**（`sources_store_unavailable`），服务本身照起 |
| `ASKDB_SOURCES_PASSWORD` | 无 | DSN 里已写 `password=` 就不需要 |
| `ASKDB_SOURCES_SCHEMA` | `public` | 一般不设，测试用它做隔离 |

**503 而不是空列表是有意的**：读不到与"一个源都没有"是两回事，
显示成空页面会让人以为实例正常，而它此刻既查不了数也存不下新源。

两副本共用同一个库，因此"在 A 副本加的源 B 副本立刻看得见" —— 这是换掉
文件存储的直接收益，原来两个 Pod 各写各的 hostPath 且**无锁**。

首次上线跑一次迁移（幂等，但**服务开始写库之后别再跑**，会把库里更新过的
白名单改回 yaml 里的旧版本）：

```bash
python -m scripts.migrate_sources_to_pg -c config/public.yaml --dry-run   # 先看
python -m scripts.migrate_sources_to_pg -c config/public.yaml             # 再写
```

脚本**不删** `var/sources` —— 迁移出错时那批 yaml 是唯一的原始数据。
回读核对通过后由人手工删。

另有两把**有意不配**的密钥，别顺手补上：

| 环境变量 | 不配的后果 | 为什么不配 |
|---|---|---|
| `ASKDB_ADMIN_TOKEN` | 角色成员的写入整体关闭（fail-closed），页面显示只读 | 对外实例没有可信调用方，开了等于谁都能改成员名单 |
| `ASKDB_SECRET_KEY` | 运行时添加数据源时不接受明文口令，只能填环境变量名 | 口令一个字不落盘，这正是这个实例该有的姿态 |

建完用 `curl -s https://askdb.ragforge.net/api/health | jq .quota` 确认
`backend` 是 `redis`、`multi_replica_safe` 是 `true`。若显示 `file`，说明
Secret 没生效 —— 此时**不要**把 replicas 调大于 1，配额会变成 N 倍。
（当前 `replicas: 4`，所以这条 Secret 是硬前提，不是可选项。）

### 接入 ragforge 与 careermate 两个数据源（2026-09-07）

内置数据源已撤，两个库都走运行时注册表（见 `config/public.yaml` 开头那段）。
**顺序不能换**：库侧角色 → Secret → 滚 Pod → 注册数据源 → 最后才推代码。
反过来先推代码，会有一段时间站点一个数据源都没有，且冒烟会红在探针上。

先在数据机上建三套库侧对象（脚本自带执行命令与回滚说明）：

```bash
# 1. 元数据库：askdb 自己的数据，要能写。单独一个库，不与业务库同库 ——
#    权限方向相反，混在一起迟早有人把可写那把配到 datasource 上去。
#    用超级用户：末尾要在新库里授 public schema 的 CREATE（PG 15 起不再默认
#    放开），少这一句的症状是数据源页 503 且日志 permission denied for schema。
#    **这台 PG 上超级用户是 ragforge，没有 postgres 这个角色** —— 写
#    -U postgres 会 FATAL: role "postgres" does not exist（2026-09-07 实测）。
ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
  psql -U ragforge -d postgres -v pwd=\"'<meta 口令>'\"" < scripts/askdb_sources_store_setup.sql

# 2. ragforge 全库只读。**不能复用 askdb_ro** —— 它那 5 张表上的 RLS 条件是
#    current_setting('app.org_id')，而运行时数据源写死不做租户隔离、不会去 SET
#    它，结果是连得上、扫得出表、查询不报错、结果永远为空。
ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
  psql -U ragforge -d ragforge -v pwd=\"'<ro_all 口令>'\"" < scripts/ragforge_readonly_all_setup.sql

# 3. careermate 只读（该库此前没有任何只读角色）。必须用超级用户执行：
#    GRANT ... ON ALL TABLES 要属主权限，普通角色跑会静默只授到一部分表。
#    这台机上超级用户就是 ragforge（见上），别照旧文写 -U postgres。
ssh root@8.163.30.216 "docker exec -i ragforge-postgres \
  psql -U ragforge -d careermate_db -v pwd=\"'<cm 口令>'\"" < scripts/careermate_readonly_setup.sql
```

再建/补 Secret。两个数据源的口令走 `askdb-db`（`envFrom` 取整个 Secret，
加键即可，不必改 manifest）—— 本实例有意不配 `ASKDB_SECRET_KEY`，
所以数据源口令**只能填环境变量名**，明文那条路是关着的：

```bash
kubectl -n askdb create secret generic askdb-sources \
  --from-literal=ASKDB_SOURCES_DSN='host=172.25.90.183 port=5432 dbname=askdb_meta user=askdb_meta' \
  --from-literal=ASKDB_SOURCES_PASSWORD='<meta 口令>'

kubectl -n askdb create secret generic askdb-db \
  --from-literal=ASKDB_PROD_PG_PASSWORD='<askdb_ro 的口令，保持原值>' \
  --from-literal=RAGFORGE_RO_ALL_PASSWORD='<ro_all 口令>' \
  --from-literal=CAREERMATE_RO_PASSWORD='<cm 口令>' \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n askdb rollout restart deployment/askdb
kubectl -n askdb rollout status deployment/askdb --timeout=180s
```

滚完确认 `/api/sources` 不再 503（此时旧镜像里内置源还在，站点照常可用）。
然后登录（写操作一律要登录，管理员令牌过不了能力位判定）并注册两个源：

```bash
curl -c /tmp/j -s -X POST https://askdb.ragforge.net/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"owner","password":"<口令>"}'

for s in \
  '{"name":"ragforge 生产库","type":"postgresql","env":"prod_ro","upstream":"8.163.30.216:5432","dsn":"host=172.25.90.183 port=5432 dbname=ragforge user=ragforge_ro_all","password_env":"RAGFORGE_RO_ALL_PASSWORD"}' \
  '{"name":"careermate 生产库","type":"postgresql","env":"prod_ro","upstream":"8.163.30.216:5432","dsn":"host=172.25.90.183 port=5432 dbname=careermate_db user=careermate_ro","password_env":"CAREERMATE_RO_PASSWORD"}'
do
  curl -b /tmp/j -s -X POST https://askdb.ragforge.net/api/sources \
    -H 'Content-Type: application/json' -d "$s" | jq '.source.id, .visible_count'
done
```

**新源的表默认一张都不开放** —— 扫描只解决"看得见"，开放是单独一步。
逐个源把扫出来的表提交进白名单：

```bash
SID=src_xxxxxxxxxxxx
TABLES=$(curl -b /tmp/j -s "https://askdb.ragforge.net/api/sources/$SID/scan" \
         | jq -c '[.tables[].name]')
curl -b /tmp/j -s -X PUT "https://askdb.ragforge.net/api/sources/$SID/tables" \
  -H 'Content-Type: application/json' -d "{\"tables\":$TABLES}" | jq '.table_count'
```

两个源都注册并开好表之后，才推代码（撤内置源 + `auth.required: true`）。
CI 的部署后实证会回落到注册表里第一个开了表的源；注册表若是空的，
探针给 `reason` 而不是让护栏那条断言背锅。

---

## 部署

### 应用（自动）

推 `main` 即触发 `.github/workflows/ci-cd.yml`：
测试 → 构建镜像（tag 取 commit sha）→ 推 ACR → 经跳板机 SSH 到 Server 3 →
`kubectl apply` → 等待 rollout → 冒烟。

镜像 tag 用 commit sha 而非 `latest`：出问题时能确切知道线上跑的是哪次提交，
回滚也只是把 tag 改回去。

### 前端（跟着应用一起走，但有一步是手动的）

界面是 `frontend/` 下的独立工程（React + Vite），**构建产物入 git**：
`npm run build` 直接写到 `askdb/web/`，由 FastAPI 托管。镜像里没有 node
（`Dockerfile` 只 `COPY askdb ./askdb`），所以线上跑的就是仓库里那份产物。

改完前端必须自己构建并把产物一起提交：

```bash
cd frontend
npm ci          # 用 ci 不用 install：install 会按 semver 升依赖，产物跟着变
npm run build   # → ../askdb/web/{index.html,assets/*}
cd .. && git add frontend askdb/web
```

**忘了这一步的后果是"看不见"的**：单测绿、镜像构建绿、rollout 绿、
`/api/health` 绿，只有界面停在上一版。因此 CI 里有一道 `frontend` 关卡
（`.github/workflows/ci-cd.yml`）——它自己重新构建一遍，产物与仓库里的
对不上就直接红，且 `build-and-push` 依赖它通过。冒烟侧另有一条：
真去取一遍首页引用的每个 `/assets/*`，挡住"接口全绿但页面白屏"。

> 产物已验证可复现：同一份 `package-lock.json` 在干净目录 `npm ci` 后
> 构建，文件名 hash 与内容逐字节一致。所以那道关卡红了就是真忘了构建，
> 不是工具链抖动。

### 入口（手动一次）

`deploy/nginx-askdb.conf` 的内容要**合并进 rag-forge 仓库的 `nginx.conf`**，
不要单独在服务器上放文件 —— Server 2 的入口配置由 rag-forge 的 CI 统一推送，
单放的文件下次部署就被覆盖了。

合并后推 rag-forge 的 main，其 CI 检测到 `nginx.conf` 变化会自动推送并
`docker compose up -d --force-recreate`。

> ⚠️ **那条流水线不跑 `nginx -t`**，而这份 `nginx.conf` 是三个站点共用的
> （ragforge.net / careerforge.cn / askdb.ragforge.net）。askdb 这一段写错一个
> 分号，nginx 起不来，三个站点一起挂。合并后、推 main 前，务必先在 Server 2 上
> 验一遍语法：
>
> ```bash
> # 在 Server 2 的 /opt/rag-forge 下，用候选配置起一个一次性容器验语法，
> # 不碰正在服务的那个
> docker run --rm \
>   -v /opt/rag-forge/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
>   -v /data/ssl:/etc/nginx/ssl:ro \
>   nginx:alpine nginx -t
> ```

本次前端上线带来的两处入口改动（见 `deploy/nginx-askdb.conf`）：

| 改动 | 为什么 |
|---|---|
| `gzip on` + `gzip_proxied any` | 冷加载 290KB JS + 53KB CSS → 88KB + 11KB。**`gzip_proxied` 少不得**：它默认 off，而 askdb 的响应全部来自 `proxy_pass`，只写 `gzip on` 一个字节都不会压 |
| `location ^~ /assets/` 长缓存 | 产物文件名带内容 hash，改一版就是新文件名，可以放心 `immutable` 缓存一年；`index.html` 由后端发 `no-store`，永远拿得到最新引用 |

---

## 验证

```bash
curl -s https://askdb.ragforge.net/api/health | python3 -m json.tool
```

应看到 `guard.daily_quota = 12000`、`quota.backend = redis` 且
`multi_replica_safe = true`。

> `datasource.type = duckdb` 这句已作废：2026-09-03 起对外实例直连 ragforge
> 生产主库，2026-09-07 起两个源都走运行时注册表，都不是 duckdb。

> `llm.disabled` 现在是 `false` —— 实例已接模型，成本由"便宜的模型 + 每日配额 +
> 共享计数"三层兜住，不再靠"不接模型"。旧版本这里写的是 `disabled = true`，
> 按那句话去核会误判成配置坏了。

界面与前端产物（**接口全绿而页面白屏是最容易漏的一种故障**）：

```bash
BASE=https://askdb.ragforge.net
# 首页应是 200 + text/html，并引用带 hash 的 /assets/*
curl -s "${BASE}/" | grep -o '/assets/[^"]\+'
# 逐个取一遍，都应是 200、非空、content-type 正确
for R in $(curl -s "${BASE}/" | grep -o '/assets/[^"]\+' | sort -u); do
  curl -s -o /dev/null -w "${R} → %{http_code} %{content_type} %{size_download}B\n" "${BASE}${R}"
done
```

登录能力（依赖 `askdb-auth` Secret，缺了会静默关闭）：

```bash
curl -s https://askdb.ragforge.net/api/auth/me | python3 -m json.tool
```

应看到 `enabled: true`、`required: false`（匿名可读，登录不是读的门 ——
2026-09-06 起登录也不放宽可见面，只放开写；2026-09-03～09-07 之间这里一度是
`required: true`，那段时间的旧记录按 `false` 去核会误判）、`scope.tables` 非空。
若 `enabled` 是 `false`，去建 `askdb-auth`（见上文），不是代码问题。

> `/api/auth/me` 里那份内置账号列表已随 61f4a8b「登录落地」下线（一键体验改为
> 纯前端跳过登录，不再有内置的免登录账号），旧版本这里要求它非空，按那句话去核
> 会误判成配置坏了。

写门是否还在（未登录不该改得动配置）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://askdb.ragforge.net/api/sources \
  -H 'Content-Type: application/json' -d '{}'   # 期望 401
```

> 中间件在路由之前跑，所以这个空 body 不会落到任何处理函数上，不会多出数据源。
> 期望 401 且 `code` 为 `login_required`；若是 `login_unavailable`，说明会话密钥
> 没配、登录整体关闭 —— 门关着，但也没人进得来。

护栏是否生效：

```bash
curl -s -X POST https://askdb.ragforge.net/api/sql \
  -H 'Content-Type: application/json' \
  -d '{"sql":"DELETE FROM documents"}' | python3 -m json.tool
```

应返回 `200` + `ok: false` + `rejected_by: "R-02"` —— **护栏拒绝不是 5xx**，
这是接口的既定约定（见 `server.py` 开头）。

内省与自检端点（2026-08-25 运营决定**放开**，与 ask 的每 IP 限流一起）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://askdb.ragforge.net/api/introspect   # 期望 200
```

> 旧版本这里写的是"应被 nginx 挡在外面，期望 404"。放开之后那句话就反了 ——
> 冒烟里对应的断言也已经改成钉住 200（哪天又变回 404，说明入口被回滚，
> 需要有人知道）。

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

- **审计日志**在容器内 `/app/var/audit-public.jsonl`，已挂 hostPath
  `/opt/askdb/var`，**重建不丢**。位置很关键：每日配额靠数当天的审计条数实现，
  日志一丢配额就归零，等于形同虚设。（早期版本写在 `/app/data`、随容器重建丢失，
  那是挂卷之前的状态。）
- **限流**分三层：nginx 按路径三档（30 r/s ／ `/api/sql` 2 r/s ／
  `/api/ask` 20 r/min，见上表）；应用侧对出站建连 10 次/分、登录失败
  10 次/分（`server.py` 的 `_SOURCE_DIAL_RL` / `_LOGIN_RL`，**进程内计数**，
  N 副本实际是 N 倍）；以及每日模型调用配额 12000（走 Redis 共享计数，
  副本间是准的）。

  > 应用侧那几个"按 IP 分桶"的限流器，2026-09-09 之前在**线上是失效的**：
  > 进程前面隔着 nginx 和 NodePort，`request.client.host` 对所有访客都是
  > 同一个值，等于全站共用一个桶。现在改读 nginx 写入的 `X-Real-IP`
  > （`server.py` 的 `_client_ip`）。`_LOGIN_RL` 更早连 key 都没传，
  > 是字面意义上的全局桶 —— 任何人每分钟 10 个请求就能让所有人登不上去。
- **资源**：requests 200m CPU / 256Mi，limits 1000m / 768Mi，4 副本。
  每个 Pod 是单个 uvicorn worker，一个 Python 进程吃不满一个核 ——
  扩容靠加副本，把 CPU limit 提到 1 核以上没有意义。
