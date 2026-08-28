"""生成本机样例库（DuckDB）。

固定随机种子，保证任何人 clone 下来生成的数据完全一致 ——
这是评测结果可复现的前提。

用法：python -m data.seed
"""

from __future__ import annotations

import csv
import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

SEED = 20260811
# org 316 用独立种子，与上面那条流互不干扰（原因见 KBS_316 的注释）
SEED_316 = 20260828
NOW = datetime(2026, 8, 11, 14, 0, 0)
OUT = Path(__file__).resolve().parent / "sample.duckdb"

ORGS = [(65, "平台组织"), (66, "外部合作方"), (67, "测试组织"),
        (316, "职场数据分享组织")]

# (kb_id, org_id, 名称, 文档量, 失败率, 卡住数)
KBS = [
    (1,  65, "产品中心",   24108, 0.020, 0),
    (3,  65, "合规库",     15663, 0.012, 1),
    (5,  65, "研发库",      9820, 0.018, 2),
    (7,  65, "外部采集库", 12204, 0.276, 0),
    (9,  65, "人事库",      4410, 0.008, 0),
    (12, 65, "历史归档库",  8640, 0.341, 3),
    (15, 65, "项目库",      6302, 0.014, 0),
    (18, 65, "财务档案",    3901, 0.011, 0),
    (21, 65, "客户资料",    5140, 0.009, 0),
    (24, 65, "培训材料",    2880, 0.016, 0),
    (31, 65, "测试沙箱库",  1902, 0.224, 0),
    (40, 66, "合作方文档",  6120, 0.031, 0),
    (41, 66, "合作方归档",  2050, 0.019, 0),
    (50, 67, "沙箱",         820, 0.052, 0),
]

# org 316「职场数据分享组织」—— 对外实例的默认租户（config/public.yaml）。
#
# 单独一张表、单独一条随机流（SEED_316），**不能并进 KBS**：两个生成循环
# 共用同一个 rnd，往 KBS 里追加会让后面的 model_usage 循环整体偏移，
# 65/66/67 的既有用量数据全部改变，历史评测就不可比了 —— 实测确认过。
# 以后再加组织照这个模式来：新组织一张新表 + 一个新种子，老组织永不受扰。
#
# 文档总量 13,959 对齐真实 org 316 的规模（见 README.zh-CN.md 的环境对照表），
# 知识库主题也照真实组织的求职数据构成来，便于用样例库预演真实场景的问法。
KBS_316 = [
    (70, 316, "职位描述库",  5240, 0.023, 1),
    (71, 316, "公司情报库",  2680, 0.041, 0),
    (72, 316, "面试题库",    2115, 0.014, 2),
    (73, 316, "薪资样本库",  1460, 0.009, 0),
    (74, 316, "行业研报库",  1520, 0.196, 1),
    (75, 316, "简历样本库",   944, 0.011, 0),
]

# 走独立随机流的组织。再加新组织时三处一起补：KBS_xxx、SEED_xxx、这里。
EXT_ORGS = {316}

# 每个组织生成多少条模型用量
USAGE_ROWS = {65: 4000, 316: 2500}
USAGE_ROWS_DEFAULT = 600

FILE_TYPES = ["pdf", "docx", "xlsx", "pptx", "md", "txt"]
FILE_WEIGHTS = [46, 24, 14, 8, 5, 3]
ERRORS = ["OCR_TIMEOUT", "ENCRYPTED_FILE", "UNSUPPORTED_VERSION", "PARSE_ERROR", "OOM"]
ERROR_WEIGHTS = [42, 22, 18, 12, 6]

MODELS = [
    ("qwen-max",           "ANSWER",  0.0024, 0.0096),
    ("qwen-max",           "REWRITE", 0.0024, 0.0096),
    ("qwen3-rerank",       "RERANK",  0.0008, 0.0),
    ("deepseek-v4",        "ANSWER",  0.0006, 0.0024),
    ("text-embedding-v4",  "EMBED",   0.0002, 0.0),
]

DDL = """
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS knowledge_bases;
DROP TABLE IF EXISTS model_usage;
DROP TABLE IF EXISTS orgs;

CREATE TABLE orgs (
  id   BIGINT PRIMARY KEY,
  name VARCHAR NOT NULL
);

CREATE TABLE knowledge_bases (
  id        BIGINT PRIMARY KEY,
  org_id    BIGINT  NOT NULL,
  name      VARCHAR NOT NULL,
  doc_count BIGINT  NOT NULL
);

CREATE TABLE documents (
  id         BIGINT PRIMARY KEY,
  org_id     BIGINT   NOT NULL,
  kb_id      BIGINT   NOT NULL,
  file_name  VARCHAR  NOT NULL,
  file_type  VARCHAR  NOT NULL,
  status     VARCHAR  NOT NULL,
  error_code VARCHAR,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE model_usage (
  id            BIGINT PRIMARY KEY,
  org_id        BIGINT   NOT NULL,
  model         VARCHAR  NOT NULL,
  stage         VARCHAR  NOT NULL,
  input_tokens  BIGINT   NOT NULL,
  output_tokens BIGINT   NOT NULL,
  cost_cny      DOUBLE   NOT NULL,
  created_at    TIMESTAMP NOT NULL
);
"""


def _bulk_load(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple], tmp: Path) -> None:
    """经 CSV + COPY 批量导入。

    逐条 executemany 灌 10 万行需要十几分钟，COPY 只要几秒 ——
    差别不在 DuckDB，在于每行一次往返的开销。
    """
    path = tmp / f"{table}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    con.execute(f"COPY {table} FROM '{path}' (FORMAT CSV, HEADER false, NULLSTR '')")


def _gen_docs(rnd: random.Random, kbs: list, doc_id: int) -> tuple[list, int]:
    """按知识库清单生成文档行，返回 (行, 下一个 doc_id)。

    rnd 由调用方传入而不是在函数里新建：哪些组织共用一条随机流、哪些各走各的，
    是这份样例库可复现性的关键约定，必须留在 build() 里一眼看全。
    """
    docs: list[tuple] = []
    for kb_id, org_id, kb_name, total, fail_rate, stuck in kbs:
        for _ in range(total):
            created = NOW - timedelta(minutes=rnd.randint(30, 60 * 24 * 90))
            ftype = rnd.choices(FILE_TYPES, FILE_WEIGHTS)[0]
            roll = rnd.random()
            if roll < fail_rate:
                status, err = "FAILED", rnd.choices(ERRORS, ERROR_WEIGHTS)[0]
            elif roll < fail_rate + 0.015:
                status, err = "PENDING", None
            else:
                status, err = "COMPLETED", None
            updated = created + timedelta(minutes=rnd.randint(1, 240))
            if updated > NOW:
                updated = NOW - timedelta(minutes=rnd.randint(1, 60))
            docs.append((doc_id, org_id, kb_id, f"{kb_name}_{doc_id}.{ftype}",
                         ftype, status, err, created, updated))
            doc_id += 1

        # 卡在 PROCESSING 且超过 1 小时未变更 —— 对应口径「卡住的文档」
        for _ in range(stuck):
            created = NOW - timedelta(hours=rnd.randint(2, 30))
            updated = NOW - timedelta(minutes=rnd.randint(70, 400))
            ftype = rnd.choices(FILE_TYPES, FILE_WEIGHTS)[0]
            docs.append((doc_id, org_id, kb_id, f"{kb_name}_卡住_{doc_id}.{ftype}",
                         ftype, "PROCESSING", None, created, updated))
            doc_id += 1

    return docs, doc_id


def _gen_usage(rnd: random.Random, orgs: list, uid: int) -> tuple[list, int]:
    """按组织清单生成模型用量行，返回 (行, 下一个 uid)。"""
    usage: list[tuple] = []
    for org_id, _ in orgs:
        n = USAGE_ROWS.get(org_id, USAGE_ROWS_DEFAULT)
        for _ in range(n):
            model, stage, in_price, out_price = rnd.choice(MODELS)
            tin = rnd.randint(400, 9000)
            tout = 0 if out_price == 0 else rnd.randint(80, 1400)
            cost = tin / 1000 * in_price + tout / 1000 * out_price
            ts = NOW - timedelta(minutes=rnd.randint(0, 60 * 24 * 40))
            usage.append((uid, org_id, model, stage, tin, tout, round(cost, 6), ts))
            uid += 1

    return usage, uid


def build(out: Path | None = None, quiet: bool = False) -> Path:
    # 两条独立的随机流。rnd 覆盖 org 65/66/67（历史口径，任何改动都不许扰动它），
    # rnd_316 只管 org 316。两条流的消费顺序互不影响，所以新增组织不会改动老数据。
    rnd = random.Random(SEED)
    rnd_316 = random.Random(SEED_316)

    target = out or OUT
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    con = duckdb.connect(str(target))
    con.execute(DDL)

    base_orgs = [o for o in ORGS if o[0] not in EXT_ORGS]
    orgs_316 = [o for o in ORGS if o[0] in EXT_ORGS]

    # 先跑老组织再跑 316，让既有 doc_id / uid 保持原值（新数据一律排在后面）
    docs, doc_id = _gen_docs(rnd, KBS, 1)
    docs_316, _ = _gen_docs(rnd_316, KBS_316, doc_id)
    docs += docs_316

    usage, uid = _gen_usage(rnd, base_orgs, 1)
    usage_316, _ = _gen_usage(rnd_316, orgs_316, uid)
    usage += usage_316

    all_kbs = KBS + KBS_316

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _bulk_load(con, "orgs", [tuple(o) for o in ORGS], tmp)
        _bulk_load(con, "knowledge_bases", [(k, o, n, cnt) for k, o, n, cnt, _, _ in all_kbs], tmp)
        _bulk_load(con, "documents", docs, tmp)
        _bulk_load(con, "model_usage", usage, tmp)

    stats = con.execute("""
        SELECT (SELECT COUNT(*) FROM documents),
               (SELECT COUNT(*) FROM documents WHERE status='FAILED'),
               (SELECT COUNT(*) FROM documents WHERE status='PROCESSING'),
               (SELECT COUNT(*) FROM model_usage)
    """).fetchone()
    con.close()

    if not quiet:
        print(f"样例库已生成：{target}")
        print(f"  documents    {stats[0]:>8,}  （FAILED {stats[1]:,} · PROCESSING {stats[2]:,}）")
        print(f"  model_usage  {stats[3]:>8,}")
        print(f"  knowledge_bases {len(all_kbs)} · orgs {len(ORGS)}")
    return target


if __name__ == "__main__":
    build()
