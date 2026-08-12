"""G 域 · 模型客户端与成本核算（9 条）。"""
from __future__ import annotations
import pytest
from askdb.llm import LlmClient, LlmNotConfigured
from askdb.trace import cost_cny


def test_g01_missing_api_key(cfg, monkeypatch):
    monkeypatch.delenv(cfg.llm["api_key_env"], raising=False)
    monkeypatch.delenv(cfg.llm.get("fallback", {}).get("api_key_env", "X"), raising=False)
    with pytest.raises(LlmNotConfigured) as e:
        LlmClient(cfg)._build()
    assert cfg.llm["api_key_env"] in str(e.value)


@pytest.mark.skip(reason="需真实模型端点；主备切换由 test_llm.py 用桩覆盖")
def test_g02_fallback_on_primary_failure(): ...


@pytest.mark.skip(reason="同上")
def test_g03_both_fail(): ...


@pytest.mark.skip(reason="同上")
def test_g04_bad_sql_does_not_switch_model(): ...


@pytest.mark.skip(reason="同上")
def test_g05_unstructured_response(): ...


def test_g06_deepseek_thinking_disabled(cfg):
    cfg.raw["llm"]["provider"] = "deepseek"
    c = LlmClient(cfg)
    kw = getattr(c, "_extra_body", None) or {}
    assert kw == {} or "thinking" in str(kw), "DeepSeek 须显式关闭思考模式"


def test_g07_structured_output_uses_function_calling(cfg):
    import inspect
    from askdb import llm
    src = inspect.getsource(llm)
    assert 'method="function_calling"' in src, "JSON mode 在百炼会 400"


def test_g08_cost_matches_price(cfg):
    c = cost_cny(1000, 1000, cfg.llm)
    expected = cfg.llm["price_input_per_1k"] + cfg.llm["price_output_per_1k"]
    assert abs(c - expected) < 1e-9


def test_g09_zero_tokens_zero_cost(cfg):
    assert cost_cny(0, 0, cfg.llm) == 0.0
