"""Cross-source selection stays on the current database unless recall says otherwise."""

from types import SimpleNamespace

from askdb.multiagent.source_select import RecallHit, plan_sources

_CONTRACT = [{
    "contract_id": "orders_events_day",
    "left_source": "builtin",
    "right_source": "src_events",
    "join_keys": ["date"],
    "grain": "day",
    "aggregate_only": True,
}]


def _cfg(source_id: str, name: str = ""):
    return SimpleNamespace(source_id=source_id, source_name=name or source_id)


def _recall(hits: dict[str, RecallHit]):
    def recall(_question, cfg):
        return hits[cfg.source_id or "builtin"]
    return recall


def test_confident_current_source_does_not_recall_peers():
    seen = []

    def recall(_question, cfg):
        seen.append(cfg.source_id)
        return RecallHit("builtin", "当前库", ("orders",), blind=False)

    plan = plan_sources(
        "订单有多少", _cfg("builtin", "当前库"), [_cfg("src_events", "事件库")],
        allow_cross_source=True, contracts=_CONTRACT, max_sources=3, recall=recall)
    assert plan.action == "single"
    assert seen == ["builtin"]


def test_compare_marker_adds_a_source_only_when_it_hits_new_tables():
    hits = {
        "builtin": RecallHit("builtin", "订单库", ("orders",)),
        "src_events": RecallHit("src_events", "事件库", ("events",)),
        "src_copy": RecallHit("src_copy", "副本", ("orders",)),
    }
    plan = plan_sources(
        "对比订单和访问", _cfg("builtin"), [_cfg("src_copy"), _cfg("src_events")],
        allow_cross_source=True, contracts=_CONTRACT, max_sources=3,
        recall=_recall(hits))
    assert plan.action == "multi"
    assert [hit.source_id for hit in plan.selected] == ["builtin", "src_events"]


def test_missing_contract_keeps_the_current_answer_and_names_the_other_source():
    hits = {
        "builtin": RecallHit("builtin", "订单库", ("orders",)),
        "src_events": RecallHit("src_events", "事件库", ("events",)),
    }
    plan = plan_sources(
        "对比订单和访问", _cfg("builtin"), [_cfg("src_events")],
        allow_cross_source=True, contracts=[], max_sources=3, recall=_recall(hits))
    assert plan.action == "degrade"
    assert plan.selected[0].source_id == "builtin"
    assert "事件库" in plan.reason
    assert plan.as_omitted() == [{"id": "src_events", "name": "事件库"}]


def test_blind_current_source_rejects_when_the_other_source_cannot_be_joined():
    hits = {
        "builtin": RecallHit("builtin", "订单库", (), blind=True),
        "src_events": RecallHit("src_events", "事件库", ("events",)),
    }
    plan = plan_sources(
        "访问有多少", _cfg("builtin"), [_cfg("src_events")],
        allow_cross_source=False, contracts=_CONTRACT, max_sources=3,
        recall=_recall(hits))
    assert plan.action == "reject"
    assert "订单库" in plan.reason and "事件库" in plan.reason


def test_extra_sources_stop_at_max_workers():
    hits = {
        "builtin": RecallHit("builtin", "当前", () , blind=True),
        "src_a": RecallHit("src_a", "A", ("a", "b", "c")),
        "src_b": RecallHit("src_b", "B", ("d",)),
        "src_c": RecallHit("src_c", "C", ("e", "f")),
    }
    contracts = [
        {"contract_id": "ab", "left_source": "builtin", "right_source": "src_a",
         "join_keys": ["id"], "grain": "day", "aggregate_only": True},
        {"contract_id": "ac", "left_source": "builtin", "right_source": "src_c",
         "join_keys": ["id"], "grain": "day", "aggregate_only": True},
        {"contract_id": "ca", "left_source": "src_a", "right_source": "src_c",
         "join_keys": ["id"], "grain": "day", "aggregate_only": True},
    ]
    plan = plan_sources(
        "分别看", _cfg("builtin"), [_cfg("src_b"), _cfg("src_a"), _cfg("src_c")],
        allow_cross_source=True, contracts=contracts, max_sources=2,
        recall=_recall(hits))
    assert plan.action == "multi"
    assert [hit.source_id for hit in plan.selected] == ["builtin", "src_a"]
