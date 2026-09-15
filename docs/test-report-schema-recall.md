# Schema 召回基准 · 向量路径 recall@k

| | |
|---|---|
| **版本** | V1.0 |
| **拟制日期** | 2026-09-15 |
| **被测** | `askdb@0c159f5` · `config/public.yaml` 的 `schema_rag`（`mode: vector` / `top_k: 8` / `max_k: 12` / `min_score: 0.35` / `text-embedding-v4`） |
| **用例** | 97 条 / 12 个数据源（`evals/recall-cases.jsonl` 23 条 + 两个 golden 集里带 `expect_sql` 的 74 条） |
| **跑法** | `python -m evals.recall` · 原始结果 `evals/results/recall.json` |
| **代价** | embedding 48,345 token ≈ ¥0.0242，93 秒 |

## 为什么补这一份

`config/public.yaml` 的 `schema_rag` 段里记着一条结论 ——「5 张表 / 1500 token 下
recall@k 只有 90.4%，放宽到 8 张 / 4000 token 后升到 99.1%」。它是现在这组参数唯一的
判据。但那份基准量的是**关键词回落路径**；2026-09-07 切 `mode: vector` 之后，向量路径
下的召回率一直没有实测数，当时的注释自己写着「要测得有 embedding 密钥，跑不了离线」。

于是任何要动召回注入量的改动都没有判据 —— 包括
[`design-agent-trace-optimization.html`](./design-agent-trace-optimization.html) 里
P2-2 提的「schema 分层注入：只给相似度前 N 张表完整列级明细」。这份基准就是为它补的。

## 结论

**分层注入不能做。** 主表落在前 3 名的只有 74.0% —— 按"前 3 名给完整列、其余只给表头"
分层，四分之一的问题里，真正要写进 SQL 的那张表拿不到列名。

**现在这组参数（top_k=8 / max_k=12）是对的，不要往下调。** 曲线在 k=8 附近才进入平台区。

### recall@k（96 条，问题里不含表名）

| k | 主表口径 | 严格口径 |
|---:|---:|---:|
| 1 | 40.6% | 31.2% |
| 2 | 65.6% | 52.1% |
| 3 | **74.0%** | 58.3% |
| 5 | 86.5% | 68.8% |
| 8 | **95.8%** | 84.4% |
| 12 | 97.9% | 94.8% |

两个口径分开报，因为一个问题往往不止一张表沾得上边，只报一个数会骗人：

- **主表**：只看承载被问那个量的那张表。它回答分层注入真正关心的问题 ——
  "要写进 SQL 的那张表，在不在会拿到完整列级明细的那几名里"。
- **严格**：`want` 里**每一张**表都要在前 k 名内。它是下限，回答"这道题能不能完整答对"。

自带表名的问题只有 1 条（`documents 表一共有多少行`），单独计，未混进上表。

### 分源（主表口径）

| 源 | 表数 | 用例 | @3 | @8 | @12 |
|---|---:|---:|---:|---:|---:|
| careermate | 33 | 23 | 69.6% | 91.3% | 95.7% |
| ragforge | 25 | 55 | 72.7% | 96.4% | 98.2% |
| shop_catalog | 28 | 2 | 50.0% | 100% | 100% |
| shop_payment | 14 | 2 | 50.0% | 100% | 100% |
| shop_supply | 12 | 2 | 50.0% | 100% | 100% |
| shop_aftersale / shop_customer / shop_inventory / shop_logistics / shop_marketing / shop_order / shop_review | 8–32 | 各 1–2 | 100% | 100% | 100% |

**shop_\* 每个源只有 1–2 条，这一栏说明不了问题**，放在这里只是为了不把样本量藏起来。
有统计意义的是 careermate（23 条）与 ragforge（55 条）—— 也正是表最多的两个源。

## 落榜的 25 条，形状高度一致

主表排不进前 3 名的 25 条里，**25 条的主表都是各自领域的枢纽表 / 主实体表**：
`documents`×8、`knowledge_bases`×3、`retrieval_logs`×3、`resumes`×3、
`agent_sessions`×2，以及 `users`、`organizations`、`career_tasks`、`skus`、
`payments`、`purchase_orders` 各 1。没有一条是冷僻表。

原因在实探里看得很清楚 —— 三种失败形态：

### A · 枢纽表被卫星表淹没

```
【careermate】有多少用户创建过简历但从没发起过会话      → users 第 27 名
   1. resume_generation_run  0.7048
   2. agent_pending_actions  0.6353
   3. interview_sessions     0.6344
   ...
  27. users                  0.4397
```

问题里的名词是"简历""会话"，于是描述里复述这些名词的卫星表全部压过 `users`。而
`users` 才是被计数的主体 —— 它的表描述里没有"简历"也没有"会话"，语义上等于隐形。

**这条连 `max_k=12` 都进不去，也就是说它根本不会被注入提示词。**同类的还有
`ra-j11`（`organizations` 第 16 名、相似度 0.32，低于 `min_score`）。两条合计 2.1%。

### B · 预聚合表压过明细表

```
【shop_supply】逐个供应商统计采购金额并排序，全部列出   → purchase_orders 第 7 名
   1. purchase_stats_daily   0.7243
   2. suppliers              0.6899
   3. supplier_contacts      0.6626
```

`*_stats_daily` 的描述天然更像"统计金额"这类问法。**这个坑在 shop_logistics 上已经
被填过**：`logistics_stats_daily` 的表注释里写着「**只用于看履约趋势与总量**；要查
「哪些运单」「哪些省份慢」…用 shipments」，`shipments` 的注释里也写了反向指路 ——
而 shop_logistics 两条用例的主表 @1 都是 100%。`shop_supply` 的
`purchase_stats_daily` 没有这句话。

样本太小（n=2）不足以证因果，但方向是明确的，且改注释的成本极低。

### C · 词形混淆，没有字面锚点

```
【ragforge】JD 文档数是多少                             → documents 第 7 名
   1. judge_metrics_daily    0.4476      ← "JD" 被吸到了 "judge"
   2. document_chunks        0.4371
   ...

【ragforge】documents 表一共有多少行                    → documents 第 2 名
   1. document_chunks        0.4915      ← 问题里逐字写着 documents，仍被压过
```

**向量召回对字面表名不敏感** —— 问题里原样写出表名也不保证第一名。关键词路径在这类
问法上反而更稳。第二例的 `JD 文档数` 正是生产上那条已知缺陷（「JD 数」恒 0）的
召回侧证据。

## 与老基准的关系

老基准（关键词路径）的说法是「8 张表 / 4000 token → 99.1%」，这里向量路径的主表
@8 是 95.8%、严格 @8 是 84.4%。**两个数不可直接相减**：用例集不同、口径不同（老的那个
度量的是"表在白名单里、模型却回答库里没有这类数据"，更接近这里的主表口径），采样方式
也不同。要判断"向量是不是不如关键词"，得在同一份用例上把两条路径都跑一遍 —— 本模块
加一个 `--mode keyword` 就能做，但那是另一件事，不在这份基准的范围内。

## 保真与偏差

跑的是**本机** `shop_*` / `ragforge` / `careermate_db` 库，不碰生产。

- **保真**：这些库与生产运行时源是同一份种子数据，表注释逐字相同。抽查
  `shop_logistics` 的 `carrier_scores`、`shipments` 两条注释，与生产 trace
  `4bac5ce7f21b` 的召回全文完全一致。语义召回的输入是真的。
- **偏差，方向保守**：这里把库里**全部**表当作白名单，而生产上每个源的白名单是人工
  勾选的子集。表越多竞争越激烈，所以这里的数**不高于**生产实际值。当下限用是安全的，
  当生产实测值报不行。`b01` 的 `users` 排到第 27 名，有一部分就是这个偏差造成的
  （careermate 本机 33 张表全开）。
- `want` / `primary` 的定稿纪律与 `baseline.py` 一致：**人工定稿，不从历史 trace 扒**。
  真实 trace 里存在"链路成功但答非所问"，拿历史成功当标准答案等于把错答固化成基线。

## 由此确定的事

| 结论 | 依据 |
|---|---|
| **P2-2「schema 分层注入」不做** | 主表 @3 = 74.0%，四分之一的问题主表拿不到列名 |
| **`top_k=8` / `max_k=12` 维持不动** | 曲线 k=8 才进平台区（95.8%），@12 收益已递减（97.9%） |
| 新增待办 · 补枢纽表的指路注释 | 形态 B：`shop_logistics` 已有的写法照抄到 `purchase_stats_daily` 等同构表上，成本极低 |
| 新增待办 · 召回加字面锚点 | 形态 C：问题里出现表名／别名时给一个确定性加权，向量单打独斗吃不住这类问法 |
| 新增待办 · 2.1% 的主表进不了注入集 | `b01`（第 27 名）与 `ra-j11`（第 16 名，低于 `min_score`）——**静默答错的风险位**，不是性能问题 |

## 复跑

```bash
# 需要 DASHSCOPE_API_KEY（嵌入）与一个装了 pgvector 的 Postgres（向量落盘）
ASKDB_SOURCES_DSN="host=127.0.0.1 port=5432 user=amy dbname=askdb_store_test" \
    python -m evals.recall

python -m evals.recall --sources shop_logistics,shop_payment   # 只跑某几个源
python -m evals.recall --report                                # 不跑，只读已有结果
```

索引按 `schema_rag._fingerprint`（表结构 + 口径 + 嵌入模型）分 collection，表结构没变
就不重建 —— 重复跑不会重复计费。上一份结果自动转存 `recall.prev.json`。
