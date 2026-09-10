# config/ 目录说明

拟制日期 2026-09-10 · V1.1
修订：V1.1 入口配置撤内置语义层，ragforge schemas 随冻结件迁出/删除；V1.0 首版收口

**命名规则：入口配置按部署形态命名，`schemas/` 里的白名单与口径按数据源命名。**

## 入口配置（`askdb serve -c` 用的那份，共三份）

| 文件 | 部署形态 |
|---|---|
| `askdb.yaml` | 本机开发。无内置 datasource，数据源来自运行时注册表 |
| `public.yaml` | 对外实例（Dockerfile 写死用它）。同样无内置 datasource |
| `sample.yaml` | 自带 DuckDB 样例库，不依赖任何外部服务即可跑通全链路 |

`askdb.yaml` 与 `public.yaml` **有意不配 `tables_file` / `metrics_file`**（2026-09-10 起）：
数据源全部来自运行时注册表，每个源的白名单跟着源存在 PostgreSQL 的
`askdb_sources` 表里（见 `askdb/sources.py` 顶部说明），护栏用按源派生的配置
（`sources.derive_config`）。没有任何数据源时查询直接被拒 —— 这是预期行为。
代价：业务口径中心为空页；要恢复词典，先给主力源写一对
`schemas/<源名>-tables/metrics.yaml` 再把两行指回来。

## `schemas/` —— 表白名单与业务口径

当前仅 `sample-tables/metrics.yaml`（样例库），由 `sample.yaml` 引用，
相对路径以**仓库根**为基准（见 `askdb/config.py` 的 root 解析）。

这两类文件的内容质量约等于产品准确率：白名单同时是安全边界与准确率边界，
口径是模型永远猜不出的业务定义。**改内容是产品决策，不是配置微调。**
将来某个运行时源要承担正经业务问答，就照 `sample-*` 的模式给它补一对。

## 不在本目录的配置

评测冻结区在 `evals/config/`：三份冻结配置（`ragforge-prod.yaml` 及其变体
`-eval` / `-tight`）连同它们引用的 `ragforge-prod-tables/metrics.yaml`
自包含存放，内容冻结 —— 白名单是评测语义的一部分，改了已公开的评测轮次
就无法复现。注意它们的 root 是 `evals/`，文件内相对路径按此书写，
复制回本目录时路径要跟着改。
