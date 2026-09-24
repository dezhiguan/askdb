"""Semantic Agent contracts and prompt."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SemanticContractDraft(BaseModel):
    metric_definition: str = Field(description="统一指标口径")
    time_window: str = Field(default="", description="所有子任务共用的时间窗口")
    comparison_window: str = Field(default="", description="比较期；不比较则为空")
    grain: str = Field(default="", description="聚合粒度")
    common_filters: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


SYSTEM = """你是 askdb Semantic Agent。你的职责是给并行查询建立统一语义契约，
不是生成 SQL。只能根据用户问题和已批准的任务计划定义指标、时间窗、粒度和公共过滤；
不知道的口径写入 caveats，不得猜测。所有 Query Worker 必须遵循同一契约。"""
