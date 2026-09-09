# askdb 对外开放实例镜像。
#
# 只装运行 public 配置所需的东西：不装 postgres 驱动（该实例连的是本地
# 合成样例库），不装评测与测试依赖。镜像里也**不含任何密钥** ——
# 该实例有意不接模型，见 config/public.yaml 的说明。

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层：源码改动不必重装依赖
COPY pyproject.toml README.md ./
COPY askdb/__init__.py ./askdb/
# 必须带 [web]：镜像跑的就是 Web 服务，而 fastapi/uvicorn 在 web extra 里。
# 只装主依赖会构建成功、推送成功、拉取成功，直到容器启动才炸在
# `import uvicorn` —— 前面每一关都是绿的，最难查的那种。
# 带 [redis]：每日配额的共享计数走 Redis。没装的话不会构建失败，
# 而是运行期第一次调模型时才抛"未安装 redis 包"——同样是最难查的那种。
# 带 [postgres]：2026-09-03 起对外实例直连 ragforge 生产主库，不再跑样例库。
# 这一项漏掉的表现与上面两项同类且更隐蔽 —— 镜像照常构建、Pod 照常起、
# 页面照常开，只在第一次查询时报"缺少 PostgreSQL 驱动"，
# 而错误落在数据源那一侧，很容易被当成库或网络的问题去查。
RUN pip install --no-cache-dir ".[web,redis,postgres]"

COPY askdb ./askdb
COPY config ./config
COPY data/__init__.py data/seed.py ./data/
# 评测结果随镜像一起走 —— 少了它评测页是空的。
# 带结果 JSON（逐题通过与否、失败原因、trace_id、耗时成本）、题库 jsonl，
# 以及**回放器本身**（golden + replay）——「运行回归评测」那个按钮要真跑，
# 靠的就是这两个模块。
#
# 2026-09-09 由"不带回放器"改为带。原来那条取舍的结果是：页面上摆着一个
# 在这套镜像上点一次失败一次的按钮（ImportError → 501）。要么按钮不该在，
# 要么套件就得在，不能两边都留着。
#
# 只带这两个模块，不带出题脚本（golden_*.py）、消融、混沌与 run_live：
# 那些是开发机上的工具，进镜像只会扩大攻击面与体积。golden.py 与 replay.py
# 除 askdb 自己外只依赖标准库，不引入任何新的 pip 依赖。
#
# 题库这一行是 2026-09-07 补的。此前只带 results，而 /api/eval 要按结果文件
# 里记的 provenance.golden 去读题库才能给出"评测集全集多少题、覆盖哪几类、
# 每题问的是什么"——文件不在镜像里，那一整块就静默为空，页面上「评测集」
# 卡片显示 "—"，看着像没跑过评测，实际是跑了但题目丢在构建上下文外面。
COPY evals/results ./evals/results
COPY evals/*.jsonl ./evals/
COPY evals/__init__.py evals/golden.py evals/replay.py ./evals/

# 样例库在**构建期**生成并固化进镜像：
#   · 固定随机种子 → 每次构建产出完全一致的数据，可复现
#   · 运行期容器只读，不需要写盘，也就不需要挂卷
RUN python -m data.seed && test -s data/sample.duckdb

# 非 root 运行。审计与检查点写在 /app/var（k8s 会往这里挂持久卷）——
# 不写进 /app/data，那里是随镜像固化的样例库，挂卷会把它遮掉。
# 审计必须留痕（§8 准入条件第 5 条），而且每日配额的计数就靠它。
RUN useradd --system --uid 10001 askdb \
    && mkdir -p /app/var \
    && chown -R 10001:10001 /app/data /app/var
# 必须用**数字 UID**，不能用用户名：k8s 的 runAsNonRoot 只能校验数字，
# 遇到名字会直接拒绝启动容器
#   container has runAsNonRoot and image has non-numeric user (askdb)
USER 10001

EXPOSE 8000

# 只听容器内 0.0.0.0（k8s 需要），对外暴露由 Service + 前置 nginx 控制。
# 绝不能理解为"可以公网直连" —— 见 deploy/README.md 的安全边界一节。
CMD ["python", "-m", "askdb.cli", "serve", \
     "-c", "config/public.yaml", "--host", "0.0.0.0", "--port", "8000"]
