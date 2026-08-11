"""模型层测试 —— 不打真实接口。"""

from __future__ import annotations

import pytest

from askdb.llm import LlmClient, LlmNotConfigured, LlmUsage, SqlDraft, _usage_of


def test_missing_key_gives_step_by_step_fix(cfg, monkeypatch):
    monkeypatch.delenv(cfg.llm["api_key_env"], raising=False)
    with pytest.raises(LlmNotConfigured) as e:
        LlmClient(cfg)._build()
    msg = str(e.value)
    assert ".env" in msg and cfg.llm["api_key_env"] in msg
    assert "askdb sql" in msg          # 指出无密钥也能验证护栏


def test_usage_accumulates():
    u = LlmUsage(10, 5)
    u.add(LlmUsage(3, 2))
    assert (u.input_tokens, u.output_tokens) == (13, 7)


def test_usage_of_parses_metadata():
    class Raw:
        usage_metadata = {"input_tokens": 7, "output_tokens": 3}

    u = _usage_of({"raw": Raw(), "parsed": None})
    assert (u.input_tokens, u.output_tokens) == (7, 3)


def test_usage_of_tolerates_missing_metadata():
    assert _usage_of({"raw": object()}).input_tokens == 0
    assert _usage_of(object()).output_tokens == 0


def test_generate_sql_returns_draft(cfg, monkeypatch):
    calls: list[list] = []

    class FakeModel:
        def invoke(self, messages):
            calls.append(messages)
            return {"parsed": SqlDraft(sql="SELECT 1 AS a", reasoning="ok"), "raw": None}

    class FakeChat:
        def with_structured_output(self, *a, **k):
            return FakeModel()

    c = LlmClient(cfg)
    c._model = FakeChat()
    draft, usage = c.generate_sql("问题", "【可用的表】documents")
    assert draft.sql == "SELECT 1 AS a" and usage.input_tokens == 0
    system, human = calls[0]
    assert "只能生成一条 SELECT" in system[1]
    assert "duckdb" in system[1]
    assert "documents" in human[1] and "问题" in human[1]


def test_retry_prompt_carries_error_and_last_sql(cfg):
    seen: list[str] = []

    class FakeModel:
        def invoke(self, messages):
            seen.append(messages[1][1])
            return {"parsed": SqlDraft(sql="SELECT 1 AS a"), "raw": None}

    class FakeChat:
        def with_structured_output(self, *a, **k):
            return FakeModel()

    c = LlmClient(cfg)
    c._model = FakeChat()
    c.generate_sql("问题", "schema", last_sql="SELECT bad", error="字段不存在：bad")
    assert "SELECT bad" in seen[0] and "字段不存在" in seen[0]
    assert "不要重复同样的错误" in seen[0]


def test_unparsed_output_raises(cfg):
    class FakeModel:
        def invoke(self, messages):
            return {"parsed": None, "raw": None}

    class FakeChat:
        def with_structured_output(self, *a, **k):
            return FakeModel()

    c = LlmClient(cfg)
    c._model = FakeChat()
    with pytest.raises(RuntimeError, match="结构化"):
        c.generate_sql("问题", "schema")


def test_build_is_cached(cfg):
    c = LlmClient(cfg)
    sentinel = object()
    c._model = sentinel
    assert c._build() is sentinel


def test_sql_draft_defaults():
    d = SqlDraft(sql="SELECT 1")
    assert d.reasoning == ""
