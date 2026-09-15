"""统计聚合下推之后，**两条后端必须给出同一组数字**。

2026-09-15 把 /api/audit/stats 从"把窗口内每条 record 整条拉回 Python 累加"
改成"每个维度各一条聚合 SQL"。生产实测这一页 days=30 要 1.1 秒、days=1 只要
0.23 秒 —— 完美线性，也就是说它的代价与"这个月发生过多少事"成正比，
而显示的永远是那十几个数字。审计页与执行追踪页的首屏就卡在它身上。

改完之后同一组数字有了两份算法。这份用例把同一批记录同时写进 PostgreSQL 与
JSONL，逐个字段比对 —— 与 test_tasks_pushdown 同一条思路，理由也同一条：
分叉的表现不是报错，而是页面上的数字安静地变了。

**「按模型」那一维有意不写第三份算法**：归因规则只有 audit.model_contrib
一份，入库时算好存进 model_agg 列，SQL 只做加法。这里验的是那条加法路径
与文件后端逐条合并的结果一致。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from askdb import audit, auditstore


def _ts(minutes_ago: int) -> str:
    return (datetime.now().astimezone()
            - timedelta(minutes=minutes_ago)).isoformat(timespec="microseconds")


def _rec(trace: str, ago: int, **kw) -> dict:
    base = {
        "trace_id": trace, "thread_id": f"th-{trace}", "ts": _ts(ago),
        "phase": "done", "kind": "ask", "user": "alice", "rejected_by": "",
        "elapsed_ms": 1000, "tok_in": 300, "tok_out": 40, "cost_cny": 0.01,
        "model": "qwen-max", "question": "有多少文档",
        "steps": [{"step": "generate_sql", "status": "ok",
                   "model": "qwen-max", "cost_cny": 0.01}],
    }
    base.update(kw)
    return base


#: 覆盖统计里每一个分支：拦截与否、有没有节点链、模型成败、按步归因与按记录
#: 归因两条路、缓存命中、直查、写坏的数值、跨天。
CORPUS: list[dict] = [
    _rec("s00000000001", 5),
    _rec("s00000000002", 10, elapsed_ms=8000, tok_in=1200, tok_out=900),
    _rec("s00000000003", 15, rejected_by="R-11"),
    _rec("s00000000004", 20, rejected_by="R-02"),
    _rec("s00000000005", 25, rejected_by="R-11"),
    # 模型失败与备选：成功率按节点算，不按整次调用算
    # 记录级金额 = 各步之和，与真实链路一致（成本表要对得上账）
    _rec("s00000000006", 30, cost_cny=0.03, steps=[
        {"step": "generate_sql", "status": "error", "model": "qwen-max"},
        {"step": "generate_sql", "status": "fallback", "model": "qwen-plus",
         "cost_cny": 0.02},
        {"step": "assess", "status": "ok", "model": "qwen-max", "cost_cny": 0.01}]),
    # 有金额、没模型名（2026-09-10 之前那批记录）：钱挂回记录级模型
    _rec("s00000000007", 35, cost_cny=0.03, steps=[
        {"step": "generate_sql", "status": "ok", "cost_cny": 0.03}]),
    # 老记录：step 上既没模型也没金额 → 退回记录级归因
    _rec("s00000000008", 40, steps=[{"step": "generate_sql", "status": "ok"}],
         cost_cny=0.05),
    # 没有节点链：trace_complete 的分母算它、分子不算
    _rec("s00000000009", 45, steps=[]),
    # 缓存命中：不进「按模型」
    _rec("s0000000000a", 50, cached=True, model="cache", steps=[],
         elapsed_ms=0, tok_in=0, tok_out=0, cost_cny=0.0),
    # 直查：kind=sql，没有模型
    _rec("s0000000000b", 55, kind="sql", model="", steps=[], cost_cny=0.0),
    # 数值写坏了：两条后端都该当 0，而不是一条 500、一条 0
    _rec("s0000000000c", 60, cost_cny="贵", elapsed_ms="慢", tok_in=None, steps=[]),
    # 昨天与前天各一条：按日序列要分得开
    _rec("s0000000000d", 60 * 26),
    _rec("s0000000000e", 60 * 50, rejected_by="EXEC"),
    # 三十天窗口之外：不该进任何一个数
    _rec("s0000000000f", 60 * 24 * 40),
]


@pytest.fixture
def twin(cfg, audit_store):
    """同一批记录同时进库与进文件。"""
    cfg.raw["observability"] = {**cfg.raw["observability"], "store": "postgres"}
    assert audit._resolve(cfg)[0] is not None, "cfg 没有路由到 PostgreSQL"
    for r in CORPUS:
        auditstore.append_audit(r)
    path = Path(cfg.audit_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in CORPUS) + "\n",
                    encoding="utf-8")
    return cfg, path


@pytest.mark.parametrize("days", [1, 2, 7, 30, 60])
def test_stats_are_identical(twin, days):
    """逐字段比。窗口大小也要比 —— 时间下推写反了只在某个天数上露馅。"""
    cfg, path = twin
    pg = audit.stats(cfg, days=days)
    fs = audit.stats(path, days=days)
    for key in sorted(set(pg) | set(fs)):
        assert pg[key] == fs[key], f"days={days} 的 {key} 对不上：{pg[key]} != {fs[key]}"


def test_stats_are_scoped_to_one_user(twin):
    """只看某个人的 —— 列表只给本人、统计却给全量的话，那张成本卡就是一次
    按天的聚合泄露。"""
    cfg, path = twin
    assert audit.stats(cfg, days=30, only_user="alice") == \
        audit.stats(path, days=30, only_user="alice")
    assert audit.stats(cfg, days=30, only_user="nobody")["calls"] == 0


def test_the_numbers_are_not_all_empty(twin):
    """比对本身要有内容可比：两边一起算错成 0 的话上面那些断言也会绿。"""
    cfg, _path = twin
    got = audit.stats(cfg, days=30)
    assert got["calls"] == len(CORPUS) - 1          # 窗口外那条不算
    assert got["blocked"] == 4
    assert got["model_calls"] > 0 and got["model_failed"] == 1
    assert got["by_kind"] == {"ask": got["calls"] - 1, "sql": 1}
    assert set(got["by_rule"]) == {"R-11", "R-02", "EXEC"}
    assert "cache" not in got["by_model"], "缓存命中不该出现在按模型那张表里"
    assert got["elapsed_p50_ms"] is not None


def test_cost_table_adds_up(twin):
    """「按模型」各行之和 = 总成本。对不上账的成本表比没有更坏 ——
    看的人不会知道少的是哪一笔。"""
    cfg, _path = twin
    got = audit.stats(cfg, days=30)
    by_model = round(sum(v["cost_cny"] for v in got["by_model"].values()), 6)
    assert by_model == got["cost_cny"]
