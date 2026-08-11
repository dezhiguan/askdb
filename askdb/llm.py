"""模型调用 —— 经 OpenAI 兼容端点，结构化输出。

刻意保持薄：本项目的价值在护栏与链路，不在提示词技巧。
唯一的硬要求是**结构化输出**，避免模型把解释文字混进 SQL。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from .config import Config


class LlmNotConfigured(RuntimeError):
    """未配置密钥 —— 消息里直接给出可执行的修复步骤。"""


class SqlDraft(BaseModel):
    """模型的结构化产出。字段说明会进入 function schema，模型看得到。"""

    sql: str = Field(description="一条 SELECT 语句。不要写 markdown 代码块，不要加解释文字。")
    reasoning: str = Field(default="", description="一句话说明用了哪些表、命中了哪个业务口径。")


SYSTEM = """你是一个只读数据查询助手，把用户的问题翻译成一条 SQL。

硬性要求：
1. 只能生成一条 SELECT 语句。禁止 INSERT/UPDATE/DELETE/DDL，禁止多条语句。
2. 只能使用下面列出的表和字段。**不存在的字段一律不要编**，宁可少查一列。
3. 禁止 SELECT *，显式列出需要的列。
4. 标注为租户隔离列的字段（如 org_id），**不要自己写进 WHERE**，系统会强制注入。
5. 涉及【业务口径】里的概念时，必须使用给定的定义表达式，不得自行构造。
6. 结果列请使用中文别名，便于阅读。
7. SQL 方言是 {dialect}。

如果问题无法用给定的表回答，就在 reasoning 里说明缺什么，sql 字段返回空字符串。"""

USER = """{schema}

【用户问题】
{question}"""

RETRY = """{schema}

【用户问题】
{question}

【上一次生成的 SQL】
{last_sql}

【失败原因】
{error}

请修正后重新生成。不要重复同样的错误。"""


@dataclass
class LlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "LlmUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


class LlmClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None

    def _build(self):
        if self._model is not None:
            return self._model
        key = self.cfg.api_key()
        if not key:
            env = self.cfg.llm["api_key_env"]
            raise LlmNotConfigured(
                f"未配置模型密钥（环境变量 {env}）。\n"
                f"  1. cp .env.example .env\n"
                f"  2. 在 .env 中填入 {env}=你的密钥\n"
                f"  3. 或直接 export {env}=...\n"
                f"提示：无需密钥也能验证护栏与执行链路，用 `askdb sql \"SELECT ...\"`。"
            )
        from langchain_openai import ChatOpenAI  # 延迟导入，未配密钥时不必加载

        self._model = ChatOpenAI(
            model=self.cfg.llm["model"],
            base_url=self.cfg.llm["base_url"],
            api_key=key,
            temperature=float(self.cfg.llm.get("temperature", 0)),
            timeout=60,
            max_retries=1,
        )
        return self._model

    def generate_sql(
        self,
        question: str,
        schema_prompt: str,
        dialect: str = "duckdb",
        last_sql: str = "",
        error: str = "",
    ) -> tuple[SqlDraft, LlmUsage]:
        model = self._build().with_structured_output(SqlDraft, include_raw=True)
        system = SYSTEM.format(dialect=dialect)
        if error:
            human = RETRY.format(schema=schema_prompt, question=question, last_sql=last_sql, error=error)
        else:
            human = USER.format(schema=schema_prompt, question=question)

        out = model.invoke([("system", system), ("human", human)])
        draft = out["parsed"] if isinstance(out, dict) else out
        usage = _usage_of(out)
        if draft is None:
            raise RuntimeError("模型未按结构化格式返回，请重试或更换模型。")
        return draft, usage


def _usage_of(out: object) -> LlmUsage:
    raw = out.get("raw") if isinstance(out, dict) else None
    meta = getattr(raw, "usage_metadata", None) or {}
    return LlmUsage(
        input_tokens=int(meta.get("input_tokens", 0) or 0),
        output_tokens=int(meta.get("output_tokens", 0) or 0),
    )
