"""Skill 层 —— 领域方法论 / 口径（可信数据 Agent v2 §4）。

「可信」的来源不是 loop，是这里固化的方法论。**更自主 ≠ 更对** —— 一个纯 ReAct
循环套三个工具，会原样复现线上实测查出的错（JD 用 file_type 得 0、把不存在的
「供应商」攀附 model_config.vendor、软删除时过滤时不过滤）。把这些扳正的是下面的
口径规则，不是更强的自主性。

这些是**通用、与数据源无关**的规则，防的是失败**模式**而非某张具体表。部署方可在
config 的 `skill.rules` 追加数据源特定口径（无需改码），与通用规则一并注入 LLM。
"""
from __future__ import annotations

from typing import Any


#: 通用方法论。每一条对应一类线上实测暴露过的错法。
GENERAL_RULES: list[str] = [
    "实体接地：问题里的业务实体必须在「可用的表」里真实存在。找不到对应表/列就"
    "如实拒答，严禁把它攀附到名字相近的列上硬答（例：库里没有「供应商」实体，"
    "就不能拿 model_config.vendor 之类同名列冒充）。",
    "软删除口径统一：表若有 deleted_at / is_delete / is_deleted / status(含 DELETED) "
    "这类删除标记，计数与明细默认**排除**已删除，并在结论里声明「已排除 N 条已删除」；"
    "同一类问题不得时加过滤时不加。",
    "缓存计数器不当真值：doc_count / *_count 这类缓存/派生计数列会与真实计数漂移；"
    "统计口径以对明细表 COUNT 为准，若确要用缓存列须在结论声明。",
    "枚举先查证：按某列的枚举值过滤前，先用 get_table_schema 确认该列的确切取值，不要靠"
    "列名猜（例：判 JD 文档应确认是 chunk_type 还是 file_type 承载业务类型）。",
    "口径必声明：结论要说清这个数怎么来的 —— 哪张表、哪个过滤条件、哪个时间口径。",
    "只据实答：结论只能基于工具真正返回的数据，不得编造；查不到就说查不到。",
]


def rules(cfg: Any) -> list[str]:
    """通用规则 + 部署方在 config.skill.rules 里追加的数据源特定口径。"""
    extra = []
    try:
        extra = list((cfg.raw.get("skill") or {}).get("rules") or [])
    except Exception:
        extra = []
    return GENERAL_RULES + [str(r) for r in extra]


def render(cfg: Any) -> str:
    """渲染成可注入 LLM 系统提示的一段文本。无规则则返回空串。"""
    rs = rules(cfg)
    if not rs:
        return ""
    body = "\n".join(f"{i}. {r}" for i, r in enumerate(rs, 1))
    return "【方法论 · 必须遵循】\n" + body
