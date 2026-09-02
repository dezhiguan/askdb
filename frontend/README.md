# askdb 前端

可信数据问答平台的工作台界面。React 19 + Vite + TypeScript。

## 与后端的关系

构建产物直接写到 `../askdb/web/`，由 FastAPI 托管：

- `/` → `askdb/web/index.html`（本工程的构建产物）
- `/assets/*` → 带 hash 的 JS/CSS
- `/legacy` → `askdb/web_legacy/index.html`，换壳前的单文件页面

产物**入 git**。`Dockerfile` 只 `COPY askdb/`，镜像里因此不需要 node，
部署链路与换壳前一致。改完前端必须 `npm run build` 并把产物一起提交，
否则线上看到的还是上一次的界面。

漏了这一步不会有任何报错 —— 单测绿、镜像绿、rollout 绿、`/api/health` 绿。
所以 CI 里有一道 `frontend` 关卡自己重新构建一遍并逐字节比对，对不上就红，
`build-and-push` 依赖它通过（见 `.github/workflows/ci-cd.yml`）。
本地构建请用 `npm ci` 而不是 `npm install`：后者会按 semver 升依赖，
产出的 bundle 与 CI 不一致，那道关卡就会红在与你的改动无关的地方。
部署侧的完整说明见 `deploy/README.md` 的「前端」一节。

## 本地开发

```bash
# 终端 1：后端
cd .. && python -m askdb.cli serve -c config/public.yaml --port 8011

# 终端 2：前端（5173，/api 反代到 8011）
npm install && npm run dev
```

## 构建

```bash
npm run build     # tsc -b && vite build → ../askdb/web
npm run lint      # oxlint
```

## 接后端的进度

已接真实后端：

- 外壳顶栏（数据源、模型、租户、当前配置）→ `/api/health`
- 审计中心（流水分页、检索、统计、判定链路复放、成本分布）→ `/api/audit`、`/api/audit/stats`、`/api/replay`
- 数据源（连接状态、表白名单、字段与注释、连接自检、护栏阈值）→ `/api/schema`、`/api/introspect`、`/api/selfcheck`
- 运行时添加只读数据源（测试连接、扫描、勾选白名单、删除）→ `/api/sources`
  由 `datasources.allow_runtime_add` 控制。
- 查询工作台（自然语言提问、直查 SQL、结果/SQL/执行链路/断点续跑）→ `/api/ask`、`/api/sql`、`/api/resume`
- 业务口径（指标清单、定义、命中词、来源表）→ `/api/schema`（按角色收窄）
- 执行追踪（最近执行、节点级 span、耗时分布、观测后端状态）→ 同上三个接口

其余页面仍在样例数据上，每页顶部有 `MockNotice` 声明后端现状与接入阶段。

接线时改两处：在 `src/api.ts` 加接口函数（不要在组件里直接 fetch），
接完从 `src/components/MockNotice.tsx` 的 `NOTICES` 里删掉该页条目。
漏删会留下一条显眼的假声明 —— 这比漏加安全。

## 目录

- `src/api.ts` `src/useHealth.ts` — 后端接入层
- `src/components/` — 外壳、查询工作区、结果页签、证据侧栏、弹层
- `src/pages/` — 任务、数据源、治理、追踪、Connector、开发者、路线
- `src/data/mockData.ts` — 尚未接后端的页面所用的样例数据，造数只在这一处
- `src/styles/` — 主题、外壳、通用组件、页面、响应式
