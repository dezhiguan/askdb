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
# 不装 postgres：该实例连的是本地样例库，用不到 PG 驱动。
RUN pip install --no-cache-dir ".[web]"

COPY askdb ./askdb
COPY config ./config
COPY data/__init__.py data/seed.py ./data/

# 样例库在**构建期**生成并固化进镜像：
#   · 固定随机种子 → 每次构建产出完全一致的数据，可复现
#   · 运行期容器只读，不需要写盘，也就不需要挂卷
RUN python -m data.seed && test -s data/sample.duckdb

# 非 root 运行。审计日志与检查点仍要可写 —— 直查链路不写检查点，
# 但审计要留痕（§8 准入条件第 5 条），所以单独给 data 目录写权限。
RUN useradd --system --uid 10001 askdb \
    && chown -R 10001:10001 /app/data
# 必须用**数字 UID**，不能用用户名：k8s 的 runAsNonRoot 只能校验数字，
# 遇到名字会直接拒绝启动容器
#   container has runAsNonRoot and image has non-numeric user (askdb)
USER 10001

EXPOSE 8000

# 只听容器内 0.0.0.0（k8s 需要），对外暴露由 Service + 前置 nginx 控制。
# 绝不能理解为"可以公网直连" —— 见 deploy/README.md 的安全边界一节。
CMD ["python", "-m", "askdb.cli", "serve", \
     "-c", "config/public.yaml", "--host", "0.0.0.0", "--port", "8000"]
