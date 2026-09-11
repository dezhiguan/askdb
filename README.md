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

**Optional agentic mode (`agent.enabled`).** Since 2026-09-11 askdb can *also* run
an LLM-orchestrated loop: the model picks among three read-only tools
(`search_schema` / `get_table_schema` / `execute_sql`) turn by turn, so it explores
and covers the long tail like a general agent — **without giving up the hard
safety.** The autonomy lives in orchestration; the safety stays welded to the tool
boundary: every `execute_sql` still passes the same code-decided gates
(AST → EXPLAIN → read-only role → masking), the model never decides whether a
statement may run, per-query steps/tokens are capped (R-16/R-17), and every call is
still one audited, replayable record. It is off by default; when off, behaviour is
byte-for-byte the deterministic pipeline below. See
[`docs/design-trusted-data-agent-v2.html`](docs/design-trusted-data-agent-v2.html).

---

## Documentation

| File | Contents |
|---|---|
| [`docs/tech-design.html`](docs/tech-design.html) | Technical design spec V1.1 — 11 chapters + 2 appendices: guardrail rules, evaluation plan, production boundaries |
| [`docs/design-rbac.md`](docs/design-rbac.md) · [`.html`](docs/design-rbac.html) | Roles and permissions design V1.4 — 27 permission points across 8 screens; all four stages shipped. V1.4 flattened the visible surface: every role, including anonymous, sees the same thing, and approval is the only remaining role difference |
| [`docs/prototype.html`](docs/prototype.html) | **Product prototype** — the console across all four product phases, including screens with no backend behind them yet |
| [`docs/design-trusted-data-agent-v2.html`](docs/design-trusted-data-agent-v2.html) | **Trusted Data Agent v2** — the Skill / Tool / LLM / Runtime layering behind the optional agentic mode: end-to-end flow, short/long auto-async, task-centre state machine, human approval/review, checkpoint resume |
| [`docs/design-resume.html`](docs/design-resume.html) | Task resume design V1.1 — continue from a checkpoint instead of restarting |
| [`docs/design-replay-api.html`](docs/design-replay-api.html) | Decision-chain replay API design V1.1 — field allowlist and dual kill-switch |
| [`docs/design-quota-multi-replica.html`](docs/design-quota-multi-replica.html) | Daily-quota multi-replica design V1.1 — counting moved to the model-call site with Redis storage |
| [`deploy/README.md`](deploy/README.md) | **Deployment runbook** — secrets, database-side roles, ingress merge, smoke checks, rollback |

Single-file HTML, no external dependencies — download and open in a browser (GitHub does not render HTML).

**The prototype deliberately covers more than what is built.** It is the product
target, not a description of the current release: its phase-three and phase-four
groups — connector nodes, developer tooling, the delivery roadmap — have no
backend behind them. What actually ships is listed under
[Web console](#web-console).

**Every metric in the design documents is a design-stage placeholder** — the measured
numbers are in [Measured results](#measured-results) below. See [`docs/README.md`](docs/README.md).

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

This is the default. With `agent.enabled`, the fixed order above is replaced by an
LLM loop that chooses the same steps as tools — but steps 4–6 stay exactly here,
inside the `execute_sql` tool, code-decided and unskippable. Same guardrails, same
audit; only the orchestration changes. See [How this differs](#how-this-differs-from-a-general-purpose-agent).

---

## Guardrails

| ID | Rule | Type | Status |
|---|---|---|---|
| R-01 | Single statement only (blocks statement stacking) | static | ✅ |
| R-02 | Statement-type allowlist (`SELECT` / `WITH…SELECT` only) | static | ✅ |
| R-03 | Table allowlist (covers subqueries, CTEs, JOINs) | static | ✅ |
| R-04 | Column existence (blocks hallucinated columns) | static | ✅ partial¹ |
| R-05 | No `SELECT *` | static | ✅ |
| R-06 | No cross-schema / cross-database references | static | ✅ |
| R-07 | Dangerous-function denylist | static | ✅ |
| R-08 | Cartesian product detection | static | ✅ |
| R-09 | **Forced `LIMIT` injection** | rewrite | ✅ |
| R-10 | **Forced tenant-predicate injection** | rewrite | ✅ |
| R-11 | Estimated scan-row threshold (EXPLAIN) | dry run | ✅ |
| R-12 | Statement timeout | execution | ✅ |
| R-13 | Result row cap | execution | ✅ |
| R-14 | Retry cap | control | ✅ |
| R-15 | Carry-over result size cap (multi-step) | control | ✅ |
| R-16 | Total step cap (multi-step) | control | ✅ |
| R-17 | Cumulative cost cap | control | ✅ |
| R-19 | **Forced data-age window injection** (per-role visible time range) | rewrite | ✅ |
| R-20 | Parse budget (SQL length / paren depth), rejected before parsing | static | ✅ |
| R-21 | No sampling (TABLESAMPLE) — an estimate is not the answer | static | ✅ |
| R-22 | Enum literal case normalised to what the database declares | rewrite | ✅ |
| R-23 | The answer must come from data (no table reference in the AST → reject) | static | ✅ |
| R-24 | Relative-time anchor: `MAX(time column)` used as "today" is rejected; hard-coded dates only warn | static | ✅ |

¹ Covers table-qualified columns, and bare columns when exactly one table is in scope.
Full resolution for bare columns under multi-table JOINs is pending.

**R-18 does not exist.** `docs/design-rbac.md` records that the data-age rule took
R-19 because R-18 was already taken by a fan-out-amplification rule — but no such
rule appears in the code or in the design spec. The number is reserved and unused;
the gap in the sequence is intentional, not a missing implementation.

Where each rule lives: R-01…R-10 and R-19…R-24 in `guard.py`, R-11…R-13 in
`executor.py`, R-14 in the `graph.py` router, R-15…R-17 in `planner.py`.
`guard.ENFORCED_ELSEWHERE` names the ones enforced outside the guard module, so
nobody reads that file and concludes the guardrails are only what is in it.

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

> **Runtime-registered sources deliberately run without tenant isolation.**
> `sources.derive_config()` pins `tenant.enabled = False`, because one structural
> scan cannot tell which column carries tenancy — in ragforge, `documents` is
> attributed only indirectly through `kb_id`. Guessing wrong is a cross-tenant
> read, so the rule is turned off rather than approximated, and the boundary moves
> entirely onto the read-only database role. Read the header of
> `config/public.yaml` before opening a source to anyone.

---

## Web console

`askdb serve` mounts a React console (source in `frontend/`, built output committed
to `askdb/web/` and served by FastAPI). Nine screens, grouped the way the prototype
groups them:

| Group | Screen | What it does |
|---|---|---|
| Workspace | **Query agent** | Ask in natural language. Every answer ships with the SQL, the rules that fired, and the predicates that were injected |
| Workspace | **Task center** | Longer or multi-step runs, bucketed by outcome, resumable from their checkpoint |
| Workspace | **Data sources** | Runtime registry — register a source, scan it, then open tables one allowlist at a time |
| Governance | **Identity & access** | Roles, members, and the scope a role actually receives at query time |
| Governance | **Business glossary** | Metric definitions and column semantics — the layer a model cannot infer |
| Governance | **Agent quality** | Live health, plus offline replay against the frozen golden sets |
| Governance | **Execution traces** | Per-call span tree; links out to Langfuse when observability is wired |
| Governance | **Audit** | One record per call — including blocked calls and direct SQL |
| — | **Approvals** | Over-threshold queries (R-11) queue here. The nav entry was pulled 2026-09-06; the page and `/api/approvals` both remain, so restoring it is one line |

Result **review** is separate from approval and deliberately so: approval decides
beforehand whether a query may run, review decides afterwards whether the number it
produced counts.

In the prototype but not implemented: connector nodes, developer tooling, the
delivery roadmap.

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

```bash
askdb check                              # config + datasource self-check (run this first)
askdb sql "SELECT file_name FROM documents WHERE status='PROCESSING'"
askdb ask  "which documents have been stuck processing for over an hour"
askdb serve                              # console at http://127.0.0.1:8000
```

**`askdb sql` needs no model key.** It skips generation and runs guard → dry run → execute,
so you can verify the entire guardrail chain before configuring anything.

> **The data-source screen needs a PostgreSQL side database.** Since 2026-09-06 the
> source registry lives in a table rather than in `var/sources/*.yaml`, reached via
> `ASKDB_SOURCES_DSN`. Without it the service still starts and the configured source
> still answers questions — the source endpoints return **503**
> (`sources_store_unavailable`) instead of an empty list, deliberately: "cannot read
> the registry" and "there are no sources" are different facts, and rendering the
> second when the first is true makes a broken instance look healthy.

### Development

```bash
uv pip install -e ".[dev]"     # dev installs every optional extra on purpose —
                               # skip one and a batch of tests silently skips while CI stays green
pytest                         # 752 tests · coverage gate at 81%
python -m evals.replay --blind        # held-out set (the final score)
python -m evals.ablation --groups A,B,C,D,E,F
python -m evals.chaos                 # fault injection
```

Frontend work has one extra step that is easy to miss:

```bash
cd frontend && npm ci && npm run build   # writes into ../askdb/web/
cd .. && git add frontend askdb/web      # the build output is committed
```

The image contains no Node, so what runs in production is exactly the committed
output. Forget the rebuild and every signal stays green while the UI stays one
version behind — which is why CI rebuilds it and fails on any difference.

---

## Configuration

One YAML per deployment target. Each carries the same three concerns — data source,
table allowlist, business metrics — split across files so the two that matter most
can be edited without touching thresholds:

| File | Used for |
|---|---|
| `config/askdb.yaml` | Local development instance. No datasource block either — sources come from the runtime registry |
| `config/sample.yaml` | The bundled DuckDB sample database, used by the evaluation and fault-injection runs |
| `config/public.yaml` | The public instance. Carries **no datasource block at all** — every source comes from the runtime registry |
| `config/schemas/` | **Table allowlists and metric definitions**, named after the data source (currently only `sample-*`). Since 2026-09-10 the entry configs carry no built-in semantic layer — every source comes from the runtime registry, and each source's allowlist lives in the database with it |
| `evals/config/` | Frozen evaluation configs (`ragforge-prod.yaml` — the one the production evaluation ran on — plus its variants `ragforge-eval.yaml` and `ragforge-tight.yaml`) **together with the ragforge allowlist/metric files they reference**, self-contained, kept so a published run stays reproducible |

The allowlist and the metric file determine accuracy far more than prompt tuning does:

```yaml
# schemas/sample-tables.yaml — column names are not self-explanatory; supply the business meaning
status:
  desc: "Processing state. Note: 'document count' means COMPLETED rows, not all rows"
  enum: [PENDING, PROCESSING, COMPLETED, FAILED]
org_id:
  desc: Organization ID
  tenant: true        # ← marks the tenant column, triggering R-10 rewriting
```

```yaml
# schemas/sample-metrics.yaml — the layer a model can never infer
- name: stuck documents
  aliases: [stuck, stalled, not progressing]
  predicate: "status = 'PROCESSING' AND updated_at < now() - INTERVAL 1 HOUR"
```

**Agentic mode** is two config blocks (both optional; absent ⇒ the deterministic
pipeline). `agent.enabled` turns on the LLM tool-selection loop; `max_steps`
(R-16) and `cost_cap_tokens` (R-17) bound each query; `async_after_ms` is the
wall-clock threshold past which a long query detaches to the task centre. `skill.rules`
appends deployment-specific calibers to the built-in methodology injected into the loop:

```yaml
agent:
  enabled: true
  max_steps: 6            # R-16 tool-call cap per query
  cost_cap_tokens: 20000  # R-17 accumulated-token cap; converge past it
  async_after_ms: 20000   # sync up to this, then detach to the task centre
skill:
  rules:
    - "JD document count is documents.chunk_type='JD', not file_type"
```

**Data sources are no longer configuration.** They live in the `askdb_sources`
table and are added from the console at runtime, so changing one needs no restart
and a source that breaks cannot stop the service from starting. Two replicas share
one registry, which is the whole point of moving off per-pod files: previously each
pod wrote its own host path, with no lock.

Secrets never sit in a config file. Model keys, database passwords, the session
signing key, the Redis URL and the observability keys all arrive as environment
variables from Kubernetes Secrets — see [`deploy/README.md`](deploy/README.md).

One key the public instance leaves unset on purpose:

| Variable | Consequence | Why |
|---|---|---|
| `ASKDB_ADMIN_TOKEN` | Role-member writes are closed entirely (fail-closed) | An open instance has no trusted caller; setting it would let anyone edit the member list |

`ASKDB_SECRET_KEY` (the master key for source passwords) **has been set since
2026-09-10**; this section used to say it was deliberately left unset. The reason for
the change matters more than the conclusion:

Without a master key, a runtime-added source can only reference an **environment
variable name**, and that variable has to be in the pod first — meaning a Secret edit
and a rollout for every database added. That works for our own infrastructure
(`RAGFORGE_RO_ALL_PASSWORD` and friends, known at deploy time). It does **not** work
for third-party databases, and "add a source at runtime" exists precisely so that no
deploy is needed.

The cost, stated plainly: passwords no longer stay off disk entirely — they are stored
Fernet-encrypted in `askdb_sources.password_enc`, with the master key living only in the
environment, so the database alone does not decrypt them. **If the master key is lost or
rotated, stored passwords cannot be decrypted** — the code deliberately falls back to
`None` and surfaces it as an authentication failure rather than an opaque crypto error.
Guard that key like a database password.


### Source types

Three, and their guardrails land in different places — **different landing spots have to be
written down**, or the reader will assume they are equivalent:

| | DuckDB | PostgreSQL | MySQL / MariaDB |
|---|---|---|---|
| Read-only | file opened `read_only` | read-only role + `default_transaction_read_only` | session-level `SET SESSION TRANSACTION READ ONLY` (**MySQL has no account-level switch**, so the self-check also reads `SHOW GRANTS`) |
| Statement timeout (R-12) | watchdog thread calling `interrupt()` | native `statement_timeout` | `max_execution_time` (`max_statement_time` on MariaDB), probed in order; if neither sticks the self-check goes red |
| Scan estimate (R-11) | `EXPLAIN` cardinality | `EXPLAIN (FORMAT JSON)` | `EXPLAIN FORMAT=JSON`, falling back to the classic `EXPLAIN` rows column |
| Row-level security | none | **yes**, `app.org_id` + RLS policies | **none** — `tenant.mode: rls` is refused at startup on MySQL rather than silently degrading to a single layer |
| Column comments | not available | `col_description` | `COLUMN_COMMENT` |
| Enum values | none | most-common values from `pg_stats` | native `ENUM`/`SET` read straight off the type; other columns have no free equivalent |

All three share one connection-string syntax (`host=… port=… dbname=… user=…`) and the
password never goes in it. MySQL TLS reuses the PostgreSQL keys: `sslmode=require`, or
`sslmode=verify-ca|verify-full` with `sslrootcert=…`.

A **SELECT-only account** is recommended, the same posture as `askdb_ro` on the
PostgreSQL side:

```sql
CREATE USER 'askdb_ro'@'%' IDENTIFIED BY '…';
GRANT SELECT ON pet.* TO 'askdb_ro'@'%';
GRANT USAGE ON *.* TO 'askdb_ro'@'%' WITH MAX_USER_CONNECTIONS 5;
```

### Privileged accounts (root / superuser)

**They are admitted, but never shown as clean.** The self-check splits in two, by what
each item actually proves:

| | Items | Proves | On failure |
|---|---|---|---|
| **Blocking** | reachability & auth · **write probe** · statement timeout | this connection cannot write *right now*, and we will not flatten the target database | registration refused |
| Advisory | account is read-only · connection limit · not a superuser · whitelist visibility | the account *should not* be able to write (posture, not present capability) | admitted, but the row stays ✕, the API returns `warnings`, and the card reads "有告警" |

The write probe is the only item that does not rely on a declaration — it really issues
`DELETE … WHERE 1=0` and the engine must reject it (MySQL error 1792). So root still
cannot write: the session-level read-only transaction holds, and the probe re-verifies on
every registration. What is missing is the second line of defence if that layer is ever
bypassed, so production databases should still use a read-only account. Set
`datasources.strict_account_check: true` to make the advisory items blocking again.

Two guardrails were added alongside, both reachable only by privileged accounts:

- `SELECT … INTO OUTFILE / DUMPFILE` — a write wearing a SELECT's name, targeting the
  *server's filesystem*, so the read-only transaction does not stop it. Now rejected by a
  text scan *before* parsing, attributed to R-02. Previously it was blocked only because
  sqlglot happened not to parse it.
- Locking reads (`FOR UPDATE` / `LOCK IN SHARE MODE` / `FOR SHARE`) — they change nothing
  but block writers on the target database. `FOR UPDATE` is refused by the engine in a
  read-only transaction (1792); **`LOCK IN SHARE MODE` is not** (verified), and a locking
  read over a large table blocks writers until the statement timeout fires.

---

## Repository layout

```
askdb/                one module per concern
  guard.py            static validation + forced AST rewriting   R-01…R-10, R-19
  executor.py         read-only execution, EXPLAIN dry run, masking   R-11…R-13
  planner.py          multi-step planning and its caps          R-15…R-17
  graph.py            LangGraph state machine, checkpoints, retry routing   R-14
  tools.py            three read-only atoms + tiered registry (agentic mode)
  agent.py            LLM autonomous loop — intent preflight + ReAct (agent.enabled)
  skill.py            domain methodology / calibers injected into the agent
  async_runner.py     wall-clock threshold → detach long queries to the task centre
  schema_rag.py       schema retrieval — keyword or vector mode
  sources.py          runtime data-source registry (PostgreSQL-backed)
  identity.py         roles, members, the scope each role gets at query time
  auth.py             stateless signed sessions
  approvals.py        pre-execution approval for over-threshold queries
  reviews.py          after-the-fact result review — a separate decision
  audit.py            one JSON record per call, risk-classified
  trace.py            span capture and replay payloads
  quota.py            daily model-call quota (Redis, or a file for single replica)
  observe.py          Langfuse / LangSmith wiring
  llm.py              model client — the quota is charged here, per model call
  server.py           FastAPI · 34 API endpoints
  mcp_server.py       stateless MCP surface (2026-07-28 spec)
  cli.py              askdb ask / sql / check / seed / serve / replay
  web/                built console, served by FastAPI — committed output
frontend/             React + Vite source; `npm run build` writes into askdb/web/
config/               one YAML per deployment target
data/                 sample-database generator, audit logs, checkpoint stores
evals/                golden sets, replay harness, ablation, chaos runner
scripts/              database-side setup and rollback SQL, registry migration
deploy/               k8s manifest, nginx server block, deployment runbook
tests/                1010 tests, coverage gate 81%
docs/                 design documents and the product prototype
```

---

## Deployment architecture

The public instance runs at `askdb.ragforge.net`. Nothing here is dedicated
hardware — askdb rides the footprint that already carries ragforge and CareerMate.

```
                          browser
                             │  HTTPS
                             ▼
  Server 2 · 8.163.63.222 ─────────────────────────────────────────
  nginx, in the ragforge-nginx container, shared by three sites
    · TLS termination · HSTS, nosniff, DENY, no-referrer
    · gzip — with gzip_proxied any, since everything here comes from proxy_pass
    · /assets/ cached immutable for a year; index.html is no-store
    · 5 r/s burst 10, except /api/ask, where the daily quota is the limit
                             │  proxy_pass → 172.25.90.184:31100
                             ▼
  Server 3 · single-node k3s ──────────────────────────────────────
  Deployment askdb · 2 replicas · NodePort 31100 · non-root uid 10001
    · image tagged by commit sha, never latest
    · initContainer chowns the host path so the container can write
    · hostPath /opt/askdb/var — audit log and checkpoints survive a rebuild
    · Secrets: askdb-llm, askdb-db (required — a pod that cannot reach its
               database should not start) · askdb-sources, askdb-auth,
               askdb-redis, askdb-langfuse (optional, each degrades one feature)
                             │
                             ▼
  Data machine · 172.25.90.183 (public 8.163.30.216) ──────────────
  PostgreSQL   askdb_meta    source registry — the only read-write database
               ragforge      read-only role over all tables
               careermate    read-only role
  Redis        shared daily-quota counter (db 2)
  Langfuse     self-hosted trace collection, port 3000
```

Pods reach the data machine on its **private** address. The public one times out
from Server 3 — a detail worth keeping, because the symptom is a data source that
looks correctly configured and simply never connects.

**Two replicas are only safe because of three specific things**, all of which rest
on both pods sharing one host path: the daily quota counts in Redis rather than per
process, the checkpoint store runs in WAL mode with a busy timeout, and audit
writes are single `O_APPEND` writes. That holds on a single node. Add a second node
and the checkpoint store needs a PostgreSQL backend and audit needs central
collection — raising `replicas` is not the whole change.

**Ingress is not deployed from this repository.** `deploy/nginx-askdb.conf` has to
be merged into rag-forge's `nginx.conf`, which serves three sites from one file and
is pushed by rag-forge's own CI — and that pipeline does not run `nginx -t`.
Validate the candidate on Server 2 first; one stray semicolon takes all three sites
down.

### Release path

Push to `main` → `.github/workflows/ci-cd.yml`:

```
tests (with a PostgreSQL service container)
  → frontend gate — rebuilds the console and fails if the output differs from the tree
  → image build, tagged by commit sha
  → push to ACR
  → SSH via the jump host → kubectl apply → wait for rollout
  → smoke
```

The smoke step is written against the failures that actually happen: it fetches
every `/assets/*` the index page references (catching "all endpoints green, page
blank"), asserts login is genuinely wired rather than merely switched on, and pins
the guardrail contract — a rejected statement is `200` with `ok: false` and
`rejected_by`, never a 5xx.

Rollback is `kubectl -n askdb rollout undo deployment/askdb`. The full runbook —
database-side role setup, secret creation, and the order in which sources must be
registered — is in [`deploy/README.md`](deploy/README.md).

---

## Measured results

> **This section reports the runs of 2026-08-12 and has not been re-cut since.**
> Later runs exist in `evals/results/` — a larger ragforge set and a first
> careermate set, both scored against different question sets and a different
> model. They are **not** folded in here: replacing a published held-out score
> with a better one from a differently-composed set is precisely the move §6.4
> of the design spec exists to prevent, and doing it as part of a documentation
> refresh would be worse. Whoever re-cuts this section should state the new
> composition and keep the run below for comparison.

Two evaluations, run against **two different databases**. They are reported
together because the contrast is itself the finding.

| | Synthetic sample DB | **Real production DB** |
|---|---|---|
| Data | generated by `data/seed.py`, fixed seed, 104k rows | ragforge (org 316), 13,959 docs / 1,407 retrieval logs |
| Table names & comments | clean, complete | real business naming; comments supplied via the `schemas/` allowlist |
| Tenant column | on every table | `documents` **has none** — resolved through a `kb_id` subquery |
| Cached counters | none | `doc_count` **measurably drifted** (cache 12,274 / actual 12,280) |
| Question set | `evals/golden.jsonl` | `evals/golden-ragforge.jsonl` (frozen at `golden-ragforge-v1`) |
| Held-out accuracy | 50.0% | **62.5%** |

The design document (§1.2) states that accuracy on real enterprise databases
lands in the 40–60% band and is not comparable to academic benchmarks. The
synthetic DB is exactly the idealised environment it describes — so **the
synthetic numbers alone mean little**. This section treats the production run
as the reference.

---

### 1. Real production database (use these numbers)

**ragforge @ org 316 · deepseek-v4-flash · 2026-08-12 · all measured**

#### Held-out set — final score

17 questions, never used for tuning; 16 answerable.

| Metric | Value |
|---|---|
| **Execution accuracy** | **62.5%** |
| False-reject rate | 6.2% |
| Block rate on must-reject | 100% |
| Multi-step misuse | 0% |
| P95 latency | 7.7 s |
| Total cost | ¥0.0588 |

Failure breakdown (unfiltered): wrong result 4 · pipeline failure 1 · blocked
by guardrail 1. Each carries a `trace_id`; `askdb replay <trace_id>` reproduces
the decision chain from the checkpoint.

> **The held-out set was actually run three times, and all three are published.**
> The first two scored 56.2%, the third 62.5%. The difference came from a
> comparator defect: the answer under test passes through `jsonable()` and is
> already a string, while the reference answer is a `Decimal` — the numeric
> tolerance branch was never reached. A model writing `AVG(x)` against a
> reference of `ROUND(AVG(x),1)` was scored wrong, and on the cost-per-day
> question the model's SQL was **logically identical** to the reference yet
> still failed.
> **This fix was made after seeing held-out results, and it raised the score** —
> precisely the move §6.4 exists to prevent. The pre-fix run is preserved
> verbatim as `ragforge-blind-run1-buggy-comparator.json`. The 1e-4 relative
> tolerance was not tuned to specific cases (rounding differences are ~2e-5;
> the metric errors it must preserve are ~55%). The contamination risk is real
> and is stated here rather than hidden.

#### Ablation (41 non-held-out questions, 37 answerable)

| Group | Configuration | Accuracy | Δ | 95% CI | False-reject | Cost | P95 |
|---|---|---|---|---|---|---|---|
| A | Bare prompt (full schema, no rewriting/retry/metrics) | 48.6% | — | [33.4%, 64.1%] | 2.7% | ¥0.085 | 2.2 s |
| B | + schema retrieval | 51.3% | +2.7pp | [35.9%, 66.6%] | 2.7% | ¥0.064 | 2.3 s |
| C | + static validation & retry | 48.6% | −2.7pp | [33.4%, 64.1%] | 2.7% | ¥0.068 | 2.6 s |
| **D** | **+ semantic layer (metrics)** | **62.2%** | **+13.5pp** | [46.1%, 75.9%] | **0.0%** | ¥0.066 | 1.9 s |
| E | + dry-run threshold (full single-step) | 62.2% | +0.0pp | [46.1%, 75.9%] | 0.0% | ¥0.066 | 2.0 s |
| F | + multi-step planning | 59.5% | −2.7pp | [43.5%, 73.7%] | 0.0% | ¥0.139 | 4.3 s |

Broken down by question type, the source of D's gain is unambiguous:

| Group | Single-table | Joins | **Metric-dependent** | Time window | Multi-hop |
|---|---|---|---|---|---|
| A | 10/10 | 1/10 | **0/7** | 2/4 | 5/6 |
| C | 9/10 | 1/10 | **0/7** | 4/4 | 4/6 |
| **D** | 9/10 | 2/10 | **3/7** | 3/4 | 6/6 |
| E | 9/10 | 2/10 | 3/7 | 3/4 | 6/6 |

**Without metric definitions the model answers 0 of 7 metric questions
correctly — not one.** This is the only clearly directional result in the whole
ablation, and it is invisible on the synthetic DB, where three of six metrics
are degenerate (no PENDING documents, `parent_document_id` always NULL,
retrieval logs 100% SUCCESS): using the definitions changes nothing there.

**Multi-step planning stays off by default**, per the rule hard-coded in the
ablation script beforehand. Group F: multi-hop accuracy **100% → 100%**, cost
**+110%**, and average step count never left 1.0 — the planner never actually
went multi-step, so the extra spend is entirely the two additional plan/assess
model calls.

---

### 2. Four things that count against this project

1. **No difference is statistically significant.** Paired McNemar exact tests
   on identical questions:

   | Comparison | Improved | Regressed | p |
   |---|---|---|---|
   | A→B | 4 | 3 | 1.000 |
   | B→C | 0 | 1 | 1.000 |
   | **C→D** | **6** | **1** | **0.125** |
   | D→E | 0 | 0 | 1.000 |
   | E→F | 1 | 3 | 0.625 |

   Even the largest effect (C→D, +13.5pp) does not reach significance. At
   n=37, **one correct answer = 2.7pp**; differences below roughly 8pp carry no
   information. Reaching significance needs an order of magnitude more questions.

2. **Joins score only 1–2/10, and that is mostly my question wording, not the
   model failing at SQL.** Replaying the failures shows three patterns: the
   model returns one extra column (`kb.id`) — semantically correct, scored wrong
   because comparison is on the exact column set; `LEFT JOIN` vs `JOIN` (the
   questions never state whether empty knowledge bases count); and ambiguous
   grouping ("which embedding model does each KB use" reads either as per-KB or
   grouped-by-model). §6.2 specifies "column order does not matter" but **never
   defines whether an extra column counts as a match**.
   **The question set is frozen and I did not change it** — loosening the
   comparator to raise my own score is exactly what §6.4 guards against. Recorded
   here for a v2 revision of the wording.

3. **The semantic-layer gain reflects my own choices.** Three of the six original
   metrics are degenerate on this data, so I added six that are *discriminative* —
   ones where computing by definition differs from computing by intuition. That
   is what a semantic layer is for, but it also means part of D's gain comes from
   questions written to exercise metrics. Degenerate metrics were excluded from
   question-writing; the reasoning and measured distributions are recorded in
   `evals/config/ragforge-prod-metrics.yaml`.

4. **The same configuration produces different results across runs.**
   `temperature: 0` does not guarantee determinism — false-reject moved from 0%
   to 6.2% between held-out runs two and three. Treat every single number as a
   noisy observation.

---

### 3. Synthetic sample DB (kept for contrast)

**104k rows · deepseek-v4-flash · 2026-08-12**

Held-out (18 questions): accuracy **50.0%** · false-reject 6.2% · P95 22.9 s · ¥0.0599.

Ablation (40 questions): A 59.5% → B 70.3% → C 70.3% → D 64.9% → E 73.0% → F 70.3%.

Two contrasts with the production run are worth noting: **schema retrieval (B)
gave +10.8pp on the synthetic DB but only +2.7pp in production, and the semantic
layer (D) scored −5.4pp synthetically versus +13.5pp in production.** The reason
is above — the synthetic DB's metrics are degenerate. This is a concrete instance
of why scores measured on a database you built yourself can mislead.

> That question set's freeze tag `golden-frozen-v1` was **applied after
> implementation finished**, which does not satisfy §6.4 ("tag before
> implementation begins"); this is recorded in the design document, V1.2.
> The production set's `golden-ragforge-v1` was tagged **before any evaluation
> was run**.

## Status and roadmap

**Runnable end to end, and deployed.** All 18 guardrail rules enforced, evaluation
complete — measured numbers are in the section above.

| Phase | Contents | Target | Status |
|---|---|---|---|
| P0 | Sample DB, config system, LangGraph skeleton, single-round Q&A | 2026-08-14 | ✅ |
| P1 | Schema retrieval, read-only execution, guardrails R-01…R-14 | 2026-08-18 | ✅ |
| P2 | Reflect & retry, EXPLAIN dry run, semantic layer, web UI + HTTP API | 2026-08-21 | ✅ |
| P2.5 | Remaining static rules R-06 / R-08 | 2026-08-23 | ✅ |
| P3 | **58-question golden set, replay harness, six ablation groups** | 2026-08-25 | ✅ |
| P4 | MCP packaging (stateless spec) | 2026-08-28 | ✅ |
| P5 | Multi-step query planning (R-15…R-17), ablation group F | 2026-09-02 | ✅ |
| P6 | Audit & replay page; `/api/replay` per the replay-API design (field allowlist + dual kill-switch); every call — including blocked ones and direct SQL — now leaves one audit record; optional observability wiring | 2026-08-24 | ✅ |
| P7 | Login (fixed accounts, stateless session), role-based scoping enforced on every query path, role-membership registry | 2026-09-02 | ✅ |
| P8 | **Public instance** — nginx + k3s, two replicas, Redis-backed quota, self-hosted Langfuse; standalone React console replacing the single-file page, with a CI gate on the committed build output | 2026-09-02 | ✅ |
| P9 | **Roles and permissions per `design-rbac.md`, all four stages** — write middleware, environment scope, masking and the R-19 data-age window, approval loop. V1.4 then flattened the visible surface: all roles see the same thing, approval is the only role difference, anonymous can read but not write | 2026-09-06 | ✅ |
| P10 | **Runtime data-source registry** — sources move from config and per-pod files into PostgreSQL, editable from the console; ragforge and careermate both registered as ordinary sources; result review added alongside approval; task center bucketed by outcome; quality centre wired to real judgements | 2026-09-07 | ✅ |
| P11 | **Optional agentic mode** (`agent.enabled`) — read-only tool atoms + tiered registry, grounded intent preflight, LLM ReAct loop, Skill calibers, long-query auto-async to the task centre; safety welded to the tool boundary, reusing approval / review / checkpoint / audit | 2026-09-11 | ✅ |

> **Not yet built:** binding members to real **auth-gateway** identities (JWKS /
> token-exchange) — until then login uses fixed accounts and member writes fall back
> to a shared admin token, which the public instance leaves unset. Data sources are
> DuckDB, PostgreSQL and MySQL/MariaDB. Multi-step planning (P5) ships but is off by
> default (ablation F); the P11 agentic mode is a separate loop gated by
`agent.enabled` — on for the public instance, off elsewhere. Runtime-registered sources carry no tenant isolation by
> design (see [Guardrails](#guardrails)). The prototype's phase-three and
> phase-four screens — connector nodes and developer tooling — are not implemented.

> **No unmeasured metric appears in this README.** Every figure above was actually run,
> published alongside the held-out set score and the unfiltered distribution of failure categories.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Orchestration | `langgraph` + `langgraph-checkpoint-sqlite` | Needs conditional routing and state persistence; checkpoints are what make failure replay and resume possible |
| Abstractions / model | `langchain-core`, `langchain-openai` | Structured output; OpenAI-compatible endpoints |
| **SQL parsing & rewriting** | `sqlglot` | A rewritable AST is the prerequisite for forced injection |
| Queried data | DuckDB (bundled sample) / PostgreSQL / MySQL (`PyMySQL`) | Every driver is a main dependency: the console's type dropdown renders `SUPPORTED_TYPES` directly, so an optional extra would mean "offered but unconnectable" |
| Source registry | PostgreSQL via `psycopg[binary,pool]` | A main dependency, not an extra — without it an instance cannot list its own sources. Pooled, because every query path reads it once |
| Console | React + Vite, served by FastAPI | Build output is committed; the image ships no Node |
| Interface | FastAPI + MCP | MCP per the 2026-07-28 stateless spec |
| Observability | Langfuse (self-hosted), LangSmith optional | Both optional. Neither configured, and the audit page says "not wired" rather than pretending |
| Quota counter | Redis | Multi-replica correctness; falls back to a file, which is still correct on one replica |

**The `langchain` meta-package is deliberately not used.** `AgentExecutor` supports neither conditional routing nor state persistence, and cannot resume from an intermediate node after a failure — all three are requirements here.

---

## Production boundaries

| Scenario | Verdict |
|---|---|
| Connect to a **primary** production database | **Prohibited** — unattended aggregate queries can realistically take down the primary |
| Connect to a **read replica**, for people who can read SQL | Conditionally allowed — 8 admission criteria (read-only role, RLS, audit, quota, …) |
| Expose to **end users** | **Prohibited** — end users cannot verify the SQL, so a wrong metric definition goes straight into a decision |

> **The public instance does not satisfy its own first row, and that is a
> knowingly taken risk, not an oversight.** `askdb.ragforge.net` reads the
> ragforge and careermate **primaries** — through dedicated read-only roles, with
> a statement timeout, a row cap, an EXPLAIN threshold and a daily quota in front
> of them, but they are primaries, not replicas. The table above says what should
> be true of a deployment someone else depends on. If you are copying this setup
> for a database with real load on it, point it at a replica.

**On the word "trustworthy":** as long as an LLM writes the SQL, "the result is always correct" does not exist.
What this project claims is a **trustworthy process** — dangerous operations are blocked, results are self-verifiable, decisions are traceable — **not trustworthy results**. Output always ships with the SQL that produced it.

---

## License

MIT
