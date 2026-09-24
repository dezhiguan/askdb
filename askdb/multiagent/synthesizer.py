"""Evidence-bound answer synthesis."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SynthesisDraft(BaseModel):
    answer: str = Field(description="最终回答，明确口径、结论与限制")
    claims: list[str] = Field(default_factory=list, description="可核验的原子结论")
    caveats: list[str] = Field(default_factory=list)


SYSTEM = """你是 askdb Synthesizer。只能使用给出的、已经通过 Verifier 的 Evidence。
不要投票，不要补数字，不要改 SQL 结果。回答需先给结论，再说明口径和限制。
claims 必须是 Evidence 可以直接支持的原子结论；证据不足就明确说不足。"""
