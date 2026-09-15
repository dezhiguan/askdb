"""向量路径的 Schema 召回基准 —— recall@k。

**为什么要有这个东西。**
`config/public.yaml` 的 schema_rag 段里记着一条结论："5 张表 / 1500 token 下
recall@k 只有 90.4%，放宽到 8 张 / 4000 token 后升到 99.1%"。它是这套参数
（top_k=8 / max_k=12 / token_budget=4000）唯一的判据。但那份基准量的是
**关键词回落路径**，2026-09-07 切 `mode: vector` 之后，向量路径下的召回率
至今没有实测数 —— 而当时的注释自己就写着"要测得有 embedding 密钥，跑不了
离线"。于是任何要动召回注入量的改动（例如 schema 分层注入：只给前 N 张表
完整列级明细）都没有判据可依。这个模块补的就是那份判据。

产出这个数之前，别动 top_k / max_k / token_budget，也别做分层注入。

怎么测
------
recall@k 只需要**一次**召回：向量检索返回的是一个有序列表，所有 k 的
recall 都能从同一份排名里算出来。所以这里不按不同 k 反复跑，而是一次取回
全部表的排名与相似度，再离线算 recall@1..N。

两个口径，各管各的（一个问题往往不止一张表能沾边，只报一个数会骗人）：

**recall@k（严格）**  `want` 里**每一张**表都在前 k 名内才算命中。
    它是下限，回答的是"模型能不能把这道题完整答对"。

**recall@k（主表）**  只看 `primary` 那一张 —— 承载被问那个量的表。
    它回答的是分层注入真正关心的问题："真正要写进 SQL 的那张表，
    有没有排在会拿到完整列级明细的那几名里"。

还会一并报出 `min_score`（0.35）这条线：过线表数为 0 时 schema_rag 判定本次
是**盲选**，那是与"排名对但分不够"完全不同的失败形态，排查方向也不同。

用例从哪来
----------
两处，都不是为这次测量编的：

  · `evals/recall-cases.jsonl` —— 23 条，取自 `evals/baseline-cases.jsonl`
    的生产真实提问，覆盖 12 个数据源。`want` / `primary` 是**人工定稿**，
    理由同 baseline.py 里 expect_sql 那段：真实 trace 里存在"链路成功但
    答非所问"，把历史成功当标准答案等于把错答固化成基线。
  · `evals/golden-ragforge.jsonl` / `golden-careermate.jsonl` —— 其中带
    expect_sql 的那些，`want` 由 SQL 的 FROM/JOIN 解析得出（CTE 名剔除），
    `primary` 取第一张。

**自带表名的问题单独计。** "documents 表一共有多少行"这类问题把表名写在了
问题里，向量召回当然命中 —— 把它们混进总数会让这份基准自我吹捧。报告里
按 `name_in_q` 分开列，看的时候以"问题里没有表名"那一栏为准。

保真与偏差
----------
跑的是**本机** shop_* / ragforge / careermate_db 库。这些库与生产运行时源
是同一份种子数据，表注释逐字相同（抽查 shop_logistics 的 carrier_scores、
shipments 两条注释，与生产 trace 4bac5ce7f21b 召回全文完全一致）—— 所以
语义召回的输入是保真的。

一处**已知的偏差，方向是保守的**：这里把库里全部表都当作白名单，而生产上
每个源的白名单是人工勾选的子集。表越多竞争越激烈，所以这里测出来的数
**不高于**生产实际值。拿它当下限用是安全的，拿它当生产实测值报不行。

跑法
----
    # 需要 DASHSCOPE_API_KEY（嵌入）与本机 Postgres（向量落 pgvector）
    ASKDB_SOURCES_DSN="host=127.0.0.1 port=5432 user=amy dbname=askdb_store_test" \
        python -m evals.recall

    python -m evals.recall --sources shop_logistics,shop_payment   # 只跑某几个源
    python -m evals.recall --report                                # 不跑，只读已有结果

结果写 evals/results/recall.json，上一份转存 recall.prev.json —— 与
baseline / blind 同一套前后对照惯例。

首次跑会为每个源建一次向量索引（每张表一条 embedding）。索引按
`schema_rag._fingerprint`（表结构 + 口径 + 嵌入模型）分 collection，
表结构没变就不会重建，重复跑不会重复计费。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from askdb import config as _config          # noqa: E402
from askdb import schema_rag, sources, vectors  # noqa: E402
from askdb.executor import Executor          # noqa: E402

#: 用例里的 source 名 → 本机库名。生产上这些是运行时注册的数据源，
#: 本机用同一份种子库，表注释逐字相同（见模块头的「保真与偏差」）。
LOCAL_DB = {
    "careermate": "careermate_db",
    "ragforge": "ragforge",
    "shop_order": "shop_order",
    "shop_customer": "shop_customer",
    "shop_supply": "shop_supply",
    "shop_aftersale": "shop_aftersale",
    "shop_catalog": "shop_catalog",
    "shop_logistics": "shop_logistics",
    "shop_inventory": "shop_inventory",
    "shop_payment": "shop_payment",
    "shop_marketing": "shop_marketing",
    "shop_review": "shop_review",
}

#: 生产面向公网的那份配置 —— 要测的就是它那组 schema_rag 参数
#: （mode=vector / top_k=8 / max_k=12 / min_score=0.35）。
#: **不要换成 config/askdb.yaml**：那份是本机测试实例，schema_rag.mode 写的是
#: `all`（全量注入），召回这一维被有意固定住了，拿它测召回会恒为 100%。
CONFIG = "config/public.yaml"

LOCAL_DSN = "host=127.0.0.1 port=5432 user=amy dbname={db}"

#: 算到第几名为止。比最大的源（shop_order 32 张）还大，保证排名不被截断。
MAX_K = 40


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
def _tables_of_sql(sql: str) -> list[str]:
    """从标准答案 SQL 里取出它真正读了哪些表。CTE 名要剔除 —— 它是查询内部
    定义的临时名字，不是库里的表，留着会变成一个永远召回不到的 want。"""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="postgres")
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    seen: list[str] = []
    for t in tree.find_all(exp.Table):
        if t.name and t.name not in ctes and t.name not in seen:
            seen.append(t.name)
    return seen


def load_cases(only: set[str] | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    for line in (HERE / "recall-cases.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            c["origin"] = "baseline"
            cases.append(c)

    # golden 集里带 expect_sql 的那些：want 由 SQL 解析得出，不必人工再标一遍。
    for fname, source in (("golden-ragforge.jsonl", "ragforge"),
                          ("golden-careermate.jsonl", "careermate")):
        path = HERE / fname
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            g = json.loads(line)
            sql = (g.get("expect_sql") or "").strip()
            if not sql:
                continue                      # 没有标准答案就没有 want，跳过
            try:
                want = _tables_of_sql(sql)
            except Exception:                 # noqa: BLE001
                continue                      # 解析不了的不猜，直接不计入
            if not want:
                continue
            cases.append({"id": f"{source[:2]}-{g['id']}", "source": source,
                          "question": g["question"], "primary": want[0],
                          "want": want, "origin": "golden",
                          "note": "want 由 expect_sql 的 FROM/JOIN 解析得出"})

    for c in cases:
        # 表名（或去掉下划线的写法）直接出现在问题里 —— 这类命中不算本事，
        # 单独计，别混进总数（见模块头）。
        q = c["question"].lower()
        c["name_in_q"] = any(t.lower() in q or t.lower().replace("_", "") in q
                             for t in c["want"])
    if only:
        cases = [c for c in cases if c["source"] in only]
    return cases


# --------------------------------------------------------------------------
# 按源派生配置 + 建索引
# --------------------------------------------------------------------------
def derive(base: Any, source: str) -> Any:
    """扫本机库 → 落白名单 → 派生出与生产运行时源同构的一份配置。

    走的是 server.py 里 `/api/sources/{id}/tables` 那条完全相同的路径
    （introspect → describe → whitelist_from_scan → derive_config），
    所以 desc / enum 的来源与生产一致：都来自库里的 COMMENT。
    """
    db = LOCAL_DB[source]
    src = sources.build(name=source, type_="postgresql",
                        dsn=LOCAL_DSN.format(db=db), env="test")
    # 先派生一份空白名单的配置，只为拿到一个能连库的 Executor
    with Executor(sources.derive_config(base, src)) as ex:
        names = sorted(t["name"] for t in ex.introspect())
        cols = ex.describe(names)
        # 外键跟着结构一起取 —— 生产那条路径（server.py 的 /tables）现在也这么做，
        # 这里不取就等于拿一份没有关联信息的白名单去测有关联信息的链路。
        fks = ex.foreign_keys(names)
    src.tables = sources.whitelist_from_scan(cols, names, fks)
    return sources.derive_config(base, src), names


def rank_tables(cfg: Any, question: str) -> tuple[list[tuple[str, float]], int]:
    """这道题下全部表的排名与相似度（高到低），以及本次 embedding token 数。

    直接打 VectorIndex 而不走 schema_rag.recall()：recall() 只返回**挑中**的
    那几张（还混着盲选兜底、关键词补齐、口径条目），而 recall@k 要的是完整排名。
    盲选与兜底那几条分支在这里不参与 —— 它们是召回失败之后的处置，不是召回
    本身的质量。
    """
    idx = vectors.get_index(cfg)
    want = MAX_K + len(cfg.metrics) + 2
    hits, tok = idx.search_with_usage(question, want)
    ranked = [(h.key.split(":", 1)[1], h.score) for h in hits
              if h.key.startswith("table:") and h.key.split(":", 1)[1] in cfg.tables]
    return ranked, tok


# --------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------
def injected(cfg: Any, question: str, backend: Any = None) -> Any:
    """**真正被注入提示词的**那几张表 —— 走完整条 schema_rag.recall()。

    与 rank_tables 是两个口径，都要量：
      · rank_tables 量的是**排序质量**（向量检索把主表排在第几），
        它绕过 recall() 的后处理，所以任何"召回后兜底"在它上面都看不出来；
      · 这里量的是**最终结果**（主表到底进没进提示词），后处理正是冲它去的。
    2026-09-15 加这一项，起因是字面锚点上线后基准一个数都没动 —— 不是改动没用，
    是这份基准**评不了后处理**。一个评不了自己要评的东西的基准，比没有更坏。

    **返回整个 Recall 而不只是表名**：同一次召回要同时喂两个口径（上面那两位
    注入命中，和下面 score_linking 的 precision 与留痕），调两次就是白烧一遍
    embedding、白跑一次值检索，而且两份结果还可能不一致。
    """
    return schema_rag.recall(question, cfg, backend=backend)


def score(case: dict[str, Any], ranked: list[tuple[str, float]],
          min_score: float, injected_names: list[str] | None = None) -> dict[str, Any]:
    order = [t for t, _ in ranked]
    by_score = dict(ranked)
    ranks = {t: (order.index(t) + 1 if t in order else None) for t in case["want"]}
    strict = max((r or 10**6) for r in ranks.values())    # 全部都要在前 k 名内
    pr = ranks.get(case["primary"])
    return {
        "id": case["id"], "source": case["source"], "origin": case["origin"],
        "question": case["question"], "want": case["want"],
        "primary": case["primary"], "name_in_q": case["name_in_q"],
        "ranks": ranks,
        #: 严格口径：want 里排名最靠后那张的名次。它就是"recall@k 从第几个 k
        #: 开始命中"，所以后面算任意 k 的 recall 只要比一次大小。
        "k_strict": strict if strict < 10**6 else None,
        "k_primary": pr,
        "primary_score": round(by_score.get(case["primary"], 0.0), 4),
        #: 一张都没过线 = schema_rag 会判本次为盲选。与"排名对但分不够"是
        #: 两种失败，排查方向不同，所以单独出一位。
        "any_over_min": any(s >= min_score for _, s in ranked),
        "primary_over_min": by_score.get(case["primary"], 0.0) >= min_score,
        #: 主表 / 全部 want 有没有真的进提示词。**这才是答错与否直接依赖的那一位**
        #: —— 排第 27 名和"根本没被注入"是两件事，前者只是排序差，后者是模型
        #: 手上压根没有那张表。
        "primary_injected": (case["primary"] in (injected_names or [])
                             if injected_names is not None else None),
        "all_injected": (all(t in (injected_names or []) for t in case["want"])
                         if injected_names is not None else None),
    }


def score_linking(case: dict[str, Any], rec: Any) -> dict[str, Any]:
    """Schema Linking 三条新链路的账。

    **只报 injected 那两位没有的东西** —— 主表/全部 want 有没有进提示词，
    上面的 primary_injected / all_injected 已经在报了，这里再算一遍就是
    两个名字量同一件事，迟早对不上。

    precision 是这里最要紧的一位：召回率靠多塞表就能刷上去，而上下文预算
    是真金白银 —— 只报召回不报 precision 的基准会奖励一个把全库塞进去的
    实现。avg_picked 是它的绝对量版本，两者一起看才知道涨的召回是"补对了"
    还是"补多了"。
    """
    picked = set(rec.table_names)
    want = set(case["want"])
    return {
        "id": case["id"], "source": case["source"],
        "name_in_q": case["name_in_q"],
        "n_picked": len(picked),
        "precision": round(len(want & picked) / max(len(picked), 1), 4),
        "fk_added": list(rec.fk_added),
        "value_hits": [str(h) for h in rec.value_hits],
        "coverage_gaps": list(rec.coverage_gaps),
    }


def summarize_linking(items: list[dict[str, Any]]) -> dict[str, Any]:
    def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = max(len(rows), 1)
        return {
            "n": len(rows),
            "precision": round(sum(x["precision"] for x in rows) / n, 4),
            "avg_picked": round(sum(x["n_picked"] for x in rows) / n, 2),
            "fk_added_rate": round(sum(1 for x in rows if x["fk_added"]) / n, 4),
            "value_hit_rate": round(sum(1 for x in rows if x["value_hits"]) / n, 4),
            "coverage_gap_rate": round(sum(1 for x in rows if x["coverage_gaps"]) / n, 4),
        }
    return {"all": agg(items),
            "no_table_name_in_question": agg([x for x in items
                                              if not x["name_in_q"]])}


def _rate(items: list[dict[str, Any]], key: str, k: int) -> float:
    if not items:
        return 0.0
    ok = sum(1 for x in items if x[key] is not None and x[key] <= k)
    return round(ok / len(items), 4)


def summarize(items: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    clean = [x for x in items if not x["name_in_q"]]

    def block(xs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(xs),
            "recall_strict": {str(k): _rate(xs, "k_strict", k) for k in ks},
            "recall_primary": {str(k): _rate(xs, "k_primary", k) for k in ks},
            "primary_injected_rate": round(
                sum(1 for x in xs if x.get("primary_injected")) / max(1, len(xs)), 4),
            "all_injected_rate": round(
                sum(1 for x in xs if x.get("all_injected")) / max(1, len(xs)), 4),
            "blind_rate": round(sum(1 for x in xs if not x["any_over_min"])
                                / max(1, len(xs)), 4),
            "primary_below_min_rate": round(
                sum(1 for x in xs if not x["primary_over_min"]) / max(1, len(xs)), 4),
        }

    per_source: dict[str, Any] = {}
    for s in sorted({x["source"] for x in items}):
        per_source[s] = block([x for x in items if x["source"] == s])
    return {
        "all": block(items),
        "no_table_name_in_question": block(clean),
        "table_name_in_question": block([x for x in items if x["name_in_q"]]),
        "per_source": per_source,
    }


# --------------------------------------------------------------------------
def _git_head() -> str:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                          # noqa: BLE001
        return ""


def report(data: dict[str, Any]) -> None:
    ks = data["ks"]
    s = data["summary"]
    print(f"\n跑于 {data['ts']} · {data['build']} · {data['n_cases']} 条 / "
          f"{len(data['summary']['per_source'])} 源 · "
          f"embedding {data['embed_tokens']} tok ≈ ¥{data['embed_cost_cny']}")
    print(f"配置 {data['config']}：top_k={data['top_k']} max_k={data['max_k']} "
          f"min_score={data['min_score']} model={data['embedding_model']}")

    for name, label in (("no_table_name_in_question", "问题里没有表名（以这栏为准）"),
                        ("table_name_in_question", "问题里自带表名"),
                        ("all", "全部")):
        b = s[name]
        if not b["n"]:
            continue
        print(f"\n  {label} · {b['n']} 条")
        head = "    k        " + "".join(f"{k:>8}" for k in ks)
        print(head)
        print("    严格     " + "".join(f"{b['recall_strict'][str(k)]*100:>7.1f}%" for k in ks))
        print("    主表     " + "".join(f"{b['recall_primary'][str(k)]*100:>7.1f}%" for k in ks))
        lk = (data.get("summary_linking") or {}).get(name) or {}
        if lk.get("n"):
            print(f"    **Schema Linking** precision {lk['precision']:.4f} · "
                  f"平均注入 {lk['avg_picked']} 张 · FK 补表 "
                  f"{lk['fk_added_rate']*100:.1f}% · 值命中 "
                  f"{lk['value_hit_rate']*100:.1f}% · 覆盖缺口 "
                  f"{lk['coverage_gap_rate']*100:.1f}%")
        print(f"    **注入命中** 主表 {b['primary_injected_rate']*100:.1f}% · "
              f"全部 want {b['all_injected_rate']*100:.1f}%")
        print(f"    盲选率 {b['blind_rate']*100:.1f}% · "
              f"主表未过 min_score {b['primary_below_min_rate']*100:.1f}%")

    print("\n  分源（主表口径）")
    for src, b in sorted(s["per_source"].items()):
        cells = "".join(f"{b['recall_primary'][str(k)]*100:>7.1f}%" for k in ks)
        print(f"    {src:<16}{b['n']:>3} 条 {cells}")

    miss = [x for x in data["items"]
            if x["k_primary"] is None or x["k_primary"] > 3]
    if miss:
        print(f"\n  主表不在前 3 名的 {len(miss)} 条（分层注入会伤到的就是这些）")
        for x in miss:
            r = x["k_primary"]
            print(f"    {x['id']:<12} [{x['source']}] 第 {r if r else '—'} 名 "
                  f"score={x['primary_score']} · {x['question'][:30]} → {x['primary']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="", help="只跑这几个源，逗号分隔")
    ap.add_argument("--report", action="store_true", help="不跑，只读已有结果")
    ap.add_argument("--note", default="", help="记进结果里的一句话")
    # 对照实验的开关。同一份代码、同一批用例，开与关各跑一次 —— 这是唯一
    # 能把"新链路带来的变化"与"用例集本身的性质"分开的办法。改配置文件
    # 也能达到同样效果，但那样跑出来的两份结果不写在命令里，事后没人能复现。
    ap.add_argument("--no-linking", action="store_true",
                    help="关掉 FK 扩展与值检索，跑出改造前的端到端对照")
    args = ap.parse_args()

    out_path = HERE / "results" / "recall.json"
    if args.report:
        if not out_path.exists():
            print("还没有结果，先跑一次 python -m evals.recall")
            return 1
        report(json.loads(out_path.read_text(encoding="utf-8")))
        return 0

    base = _config.load(ROOT / CONFIG)
    rag = base.raw["schema_rag"]
    if rag.get("mode") != "vector":
        print(f"{CONFIG} 的 schema_rag.mode 是 {rag.get('mode')!r}，不是 vector —— "
              "这份基准测的是向量路径，换配置或改模式后再跑")
        return 1
    if not base.api_key():
        print(f"未配置 {base.llm['api_key_env']}，向量路径跑不了 —— "
              "这正是这份基准一直缺位的原因，别在这里回落关键词凑一个数出来")
        return 1

    only = {s.strip() for s in args.sources.split(",") if s.strip()} or None
    cases = load_cases(only)
    if not cases:
        print("没有用例")
        return 1

    if args.no_linking:
        rag["fk_expand_max"] = 0
        rag["value_link"] = False
        print("  [对照组] FK 扩展与值检索已关闭")

    min_score = float(rag.get("min_score", 0.35))
    ks = [1, 2, 3, 5, 8, 12]
    items: list[dict[str, Any]] = []
    linking: list[dict[str, Any]] = []
    embed_tokens = 0
    t0 = time.time()

    for src in sorted({c["source"] for c in cases}):
        mine = [c for c in cases if c["source"] == src]
        try:
            cfg, names = derive(base, src)
        except Exception as e:                 # noqa: BLE001
            print(f"  {src}: 派生失败，跳过 —— {str(e).splitlines()[0]}")
            continue
        print(f"  {src}: {len(names)} 张表 / {len(mine)} 条用例", flush=True)
        # 值检索要连库（拿提问里的取值去真实数据里探一次）。连不上就传 None，
        # 那一路自然跳过，其余口径照测 —— 基准不该因为一档增量能力而跑不起来。
        try:
            ex_link = Executor(cfg).__enter__()
        except Exception as e:                 # noqa: BLE001
            print(f"    ! {src} 执行器建不起来，值检索这一路跳过："
                  f"{str(e).splitlines()[0]}")
            ex_link = None
        for c in mine:
            unknown = [t for t in c["want"] if t not in cfg.tables]
            if unknown:
                # want 里有库里根本没有的表 = 用例过期了。静默跳过会让基准
                # 悄悄变小，所以说出来。
                print(f"    ! {c['id']} 的 want 里 {unknown} 不在库中，跳过")
                continue
            try:
                ranked, tok = rank_tables(cfg, c["question"])
            except Exception as e:             # noqa: BLE001
                print(f"    ! {c['id']} 召回失败：{str(e).splitlines()[0]}")
                continue
            embed_tokens += tok
            try:
                rec = injected(cfg, c["question"],
                               ex_link.backend if ex_link else None)
            except Exception as e:             # noqa: BLE001
                print(f"    ! {c['id']} 注入口径失败：{str(e).splitlines()[0]}")
                rec = None
            items.append(score(c, ranked, min_score,
                               list(rec.table_names) if rec else None))
            if rec is not None:
                linking.append(score_linking(c, rec))

    data = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "build": _git_head(),
        "note": args.note,
        "config": CONFIG,
        "mode": rag.get("mode"),
        "top_k": rag.get("top_k"), "max_k": rag.get("max_k"),
        "min_score": min_score,
        "embedding_model": rag.get("embedding_model"),
        "embed_tokens": embed_tokens,
        "embed_cost_cny": round(embed_tokens / 1000
                                * float(rag.get("embedding_price_per_1k", 0) or 0), 6),
        "elapsed_s": round(time.time() - t0, 1),
        "n_cases": len(items),
        "ks": ks,
        "summary": summarize(items, ks),
        # 开关状态一起落盘：同一份代码开关一开一关跑出来的两份结果，
        # 不记开关就分不出谁是谁。
        "switches": {"fk_expand_max": rag.get("fk_expand_max"),
                     "value_link": rag.get("value_link"),
                     "coverage_check": rag.get("coverage_check")},
        "summary_linking": summarize_linking(linking),
        "items": items,
        "items_linking": linking,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        shutil.copyfile(out_path, out_path.with_suffix(".prev.json"))
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    report(data)
    print(f"\n写入 {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
