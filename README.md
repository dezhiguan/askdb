<div align="right">

**English** · [简体中文](README.zh-CN.md)

</div>

# askdb

**Turn natural language into *constrained* SQL, and execute it safely.**

The premise is not "make the model good at writing SQL." It is: **the model will be wrong — make the pipeline around it reliable anyway.**

---

## How this differs from a general-purpose agent

Hand a general-purpose agent (Claude Code, Cursor, or any ReAct agent with a `run_sql` tool) a database connection, and it will answer these questions too — with more flexibility. It explores better, covers the long tail better, and needs no upfront configuration.

**askdb does not aim to be smarter. It aims to be safe to run repeatedly.**

| | General agent | askdb |
|---|---|---|
| Flexibility / exploration / long-tail coverage | **wins** | limited to an allowlist of tables |
| Constraint strength | soft (prompt-level) | **hard (AST rewriting + database privileges)** |
| Measurability | behaves differently each run; no regression testing | **fixed pipeline — replayable, ablatable** |
| Cost | exploratory, unpredictable in advance | **bounded, capped** |
| Structured audit | scattered across a conversation | **one JSON record per call** |

In one line: **a general-purpose agent is a probe; askdb is a production line.**

| Situation | Use |
|---|---|
| Ad-hoc lookups, exploring an unfamiliar database | A general agent — askdb loses here, and that's fine |
| High-frequency repeated calls · non-SQL users · tenant isolation guarantees · a number you can quote for accuracy · audit trails | askdb |

---

## Documentation

| File | Contents |
|---|---|
| [`docs/tech-design.html`](docs/tech-design.html) | Technical design spec V1.1 — 11 chapters + 2 appendices: 17 guardrail rules, evaluation plan, production boundaries |
| [`docs/prototype.html`](docs/prototype.html) | Interactive prototype — data onboarding wizard, query pipeline, multi-step planning |

Single-file HTML, no external dependencies — download and open in a browser (GitHub does not render HTML).
**Every metric in those documents is a design-stage placeholder**; see [`docs/README.md`](docs/README.md).

---

## Execution pipeline

```
question
   │
   ├─▶ [1] Schema retrieval   inject only matched tables and business metrics — never the whole schema
   ├─▶ [2] Plan / replan      decide single-step vs multi-step
   ├─▶ [3] SQL generation     structured output, enforced JSON schema
   ├─▶ [4] Static validation  AST checks + forced rewriting · zero token cost
   │        └── fails ──▶ [7] reflect & retry ──▶ back to [3] (bounded)
   ├─▶ [5] EXPLAIN dry run    reject if estimated scan exceeds threshold
   ├─▶ [6] Read-only execute  dedicated read-only role + timeout + row cap
   ├─▶ [8] Result assessment  enough to answer? if not, back to [2]
   └─▶ [9] Answer + lineage   result + SQL for every round + data timestamp
```

**Design principle: step 4 is always code, never the model.** Letting a model review its own output is the same as having no guardrail at all.

---

## Guardrails

| ID | Rule | Type | Status |
|---|---|---|---|
| R-01 | Single statement only (blocks statement stacking) | static | ✅ |
| R-02 | Statement-type allowlist (`SELECT` / `WITH…SELECT` only) | static | ✅ |
| R-03 | Table allowlist (covers subqueries, CTEs, JOINs) | static | ✅ |
| R-04 | Column existence (blocks hallucinated columns) | static | ✅ partial¹ |
| R-05 | No `SELECT *` | static | ✅ |
| R-06 | No cross-schema / cross-database references | static | ⬜ |
| R-07 | Dangerous-function denylist | static | ✅ |
| R-08 | Cartesian product detection | static | ⬜ |
| R-09 | **Forced `LIMIT` injection** | rewrite | ✅ |
| R-10 | **Forced tenant-predicate injection** | rewrite | ✅ |
| R-11 | Estimated scan-row threshold (EXPLAIN) | dry run | ⬜ |
| R-12 | Statement timeout | execution | ⬜ |
| R-13 | Result row cap | execution | ⬜ |
| R-14 | Retry cap | control | ⬜ |
| R-15 | Carry-over result size cap (multi-step) | control | ⬜ |
| R-16 | Total step cap (multi-step) | control | ⬜ |
| R-17 | Cumulative cost cap | control | ⬜ |

¹ Covers table-qualified columns, and bare columns when exactly one table is in scope. Full resolution for bare columns under multi-table JOINs is pending.

**Rewriting happens on the AST and the SQL is regenerated from it — no prompt can override it:**

```sql
-- model output
SELECT file_name, status FROM documents WHERE status = 'PROCESSING';

-- what actually executes
SELECT file_name, status FROM documents
WHERE status = 'PROCESSING'
  AND documents.org_id = 65   -- R-10, injected
LIMIT 1000;                   -- R-09, injected
```

Tenant isolation is **two-layer**: application-level AST rewriting (R-10) **plus** database row-level security (PostgreSQL RLS).
Application-level rewriting alone is not sufficient — a single missed branch in a subquery, CTE, UNION, or view is an escalation path.

---

## Quick start

```bash
git clone https://github.com/dezhiguan/askdb.git && cd askdb

# Requires Python >= 3.10; uv recommended
uv venv --python 3.12 && uv pip install -e .

# Build the local sample database (fixed seed — byte-identical for everyone)
python -m data.seed

# Configure the model key
cp .env.example .env   # set DASHSCOPE_API_KEY
```

Runs without connecting to anything external — the sample database ships with documents, knowledge bases, organizations, and token-usage tables.

---

## Configuration

Three YAML files with separate concerns:

| File | Contents |
|---|---|
| `config/askdb.yaml` | Data source, tenant policy, guardrail thresholds, model |
| `config/tables.yaml` | **Table allowlist and column semantics** |
| `config/metrics.yaml` | **Business metric definitions** |

The last two determine accuracy far more than prompt tuning does:

```yaml
# tables.yaml — column names are not self-explanatory; supply the business meaning
status:
  desc: "Processing state. Note: 'document count' means COMPLETED rows, not all rows"
  enum: [PENDING, PROCESSING, COMPLETED, FAILED]
org_id:
  desc: Organization ID
  tenant: true        # ← marks the tenant column, triggering R-10 rewriting
```

```yaml
# metrics.yaml — the layer a model can never infer
- name: stuck documents
  aliases: [stuck, stalled, not progressing]
  predicate: "status = 'PROCESSING' AND updated_at < now() - INTERVAL 1 HOUR"
```

---

## Status and roadmap

**Currently P0, in progress. Not yet runnable end to end.**

| Phase | Contents | Target | Status |
|---|---|---|---|
| P0 | Sample DB, config system, LangGraph skeleton, single-round Q&A working | 2026-08-14 | 🚧 |
| P1 | Schema retrieval, all 17 guardrail rules, read-only execution | 2026-08-18 | ⬜ |
| P2 | Reflect & retry, EXPLAIN dry run, semantic layer, web UI | 2026-08-21 | ⬜ |
| P3 | **58-question golden set, replay harness, five ablation groups** | 2026-08-25 | ⬜ |
| P4 | MCP packaging (stateless spec), backfill measured numbers | 2026-08-28 | ⬜ |
| P5 | Multi-step query planning, ablation group F | 2026-09-02 | ⬜ |

> **No unmeasured metric will appear in this README.** Accuracy, block rate, and cost figures land after P3,
> published alongside the held-out set score and the unfiltered distribution of failure categories.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Orchestration | `langgraph` | Needs conditional routing and state persistence |
| Abstractions / model | `langchain-core`, `langchain-openai` | Structured output; OpenAI-compatible endpoints |
| **SQL parsing & rewriting** | `sqlglot` | A rewritable AST is the prerequisite for forced injection |
| Data | DuckDB (bundled sample) / PostgreSQL | |
| Interface | FastAPI + MCP | MCP per the 2026-07-28 stateless spec |

**The `langchain` meta-package is deliberately not used.** `AgentExecutor` supports neither conditional routing nor state persistence, and cannot resume from an intermediate node after a failure — all three are requirements here.

---

## Production boundaries

| Scenario | Verdict |
|---|---|
| Connect to a **primary** production database | **Prohibited** — unattended aggregate queries can realistically take down the primary |
| Connect to a **read replica**, for people who can read SQL | Conditionally allowed — 8 admission criteria (read-only role, RLS, audit, quota, …) |
| Expose to **end users** | **Prohibited** — end users cannot verify the SQL, so a wrong metric definition goes straight into a decision |

**On the word "trustworthy":** as long as an LLM writes the SQL, "the result is always correct" does not exist.
What this project claims is a **trustworthy process** — dangerous operations are blocked, results are self-verifiable, decisions are traceable — **not trustworthy results**. Output always ships with the SQL that produced it.

---

## License

MIT
