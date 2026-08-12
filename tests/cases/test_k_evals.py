"""K 域 · 评测工具链（9 条）。"""
from __future__ import annotations
import json
from decimal import Decimal as D
from pathlib import Path
import pytest
from evals.replay import _cell_eq, _rows_match

ROOT = Path(__file__).resolve().parent.parent.parent


def test_k01_golden_sql_passes_same_guard():
    import inspect, evals.replay as rp
    assert "guard.check" in inspect.getsource(rp._expected)


def test_k02_tolerates_rounding():
    assert _cell_eq(D("233.4954287"), D("233.5"))


def test_k03_rejects_metric_error():
    assert not _cell_eq(D("0.1656"), D("0.3727"))


def test_k04_string_vs_decimal():
    assert _cell_eq("233.4875621890547264", D("233.5"))


def test_k05_row_order_and_count():
    assert _rows_match([(2, "b"), (1, "a")], [(1, "a"), (2, "b")])
    assert not _rows_match([(1, "a")], [(1, "a"), (2, "b")])


def test_k06_reject_cases_check_rule():
    import inspect, evals.replay as rp
    assert "expect_rule" in inspect.getsource(rp)


def test_k07_results_record_provenance():
    p = ROOT / "evals" / "results" / "ragforge-blind.json"
    if not p.exists():
        pytest.skip("无结果文件")
    d = json.loads(p.read_text(encoding="utf-8"))
    for k in ("datasource", "golden", "model", "config"):
        assert d["provenance"].get(k), f"出处缺 {k}"


def test_k08_quota_failures_excluded_from_accuracy():
    """配额触顶不得污染准确率分母 —— 历史上出现过 23 题被判 QUOTA 失败。"""
    import inspect, evals.replay as rp
    src = inspect.getsource(rp)
    assert "QUOTA" in src and "answerable" in src, "须能把配额失败与模型失败区分开"


def test_k09_ablation_groups_share_questions():
    p = ROOT / "evals" / "results" / "ragforge-ablation.json"
    if not p.exists():
        pytest.skip("无消融结果")
    d = json.loads(p.read_text(encoding="utf-8"))
    sets = [{o["id"] for o in g["outcomes"]} for g in d.values()]
    assert all(s == sets[0] for s in sets), "各组必须跑完全相同的题"
