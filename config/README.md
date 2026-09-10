# config/ 目录说明

拟制日期 2026-09-10 · V1.0

**命名规则：入口配置按部署形态命名，`schemas/` 里的白名单与口径按数据源命名。**
`ragforge` 开头的文件不是历史残留 —— 它指的是被查询的那个 ragforge 数据库。

## 入口配置（`askdb serve -c` 用的那份，共三份）

| 文件 | 部署形态 |
|---|---|
| `askdb.yaml` | 本机开发。无内置 datasource，数据源来自运行时注册表 |
| `public.yaml` | 对外实例（Dockerfile 写死用它）。同样无内置 datasource |
| `sample.yaml` | 自带 DuckDB 样例库，不依赖任何外部服务即可跑通全链路 |

## `schemas/` —— 表白名单与业务口径

按数据源命名：`sample-*`（样例库）、`ragforge-dev-*`（本机 ragforge 库）、
`ragforge-prod-*`（云上 ragforge 库）。由入口配置的 `tables_file` / `metrics_file`
引用，相对路径以**仓库根**为基准（见 `askdb/config.py` 的 root 解析）。

这两类文件的内容质量约等于产品准确率：白名单同时是安全边界与准确率边界，
口径是模型永远猜不出的业务定义。**改内容是产品决策，不是配置微调。**

## 不在本目录的配置

评测冻结件在 `evals/config/`（`ragforge-prod.yaml` 及其变体 `-eval` / `-tight`），
内容冻结，改了已公开的评测轮次就无法复现。注意它们的 root 是 `evals/`，
文件内相对路径带 `../` 前缀，复制回本目录时路径要跟着改。

运行时添加的数据源**没有配置文件** —— 存在 PostgreSQL 的 `askdb_sources` 表里
（见 `askdb/sources.py` 顶部说明），数量增减不影响本目录。
