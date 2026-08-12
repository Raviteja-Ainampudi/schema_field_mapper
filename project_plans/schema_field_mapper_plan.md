# Schema Field Mapper - Design & Delivery Plan

**Assignment:** map every field of a legacy MySQL HR schema (`legacy_hrm`) to its semantic equivalent in a modern MongoDB schema (`people_platform`), emitting one JSON document with per-field `source_field`, `destination_field` (dot notation), `type_transform`, `confidence`, `reasoning`, `notes`.

**Hard constraint from the assignment:** *you cannot pass both schemas to an LLM in a single prompt and receive a finished mapping.* Every design decision below flows from that.

**Stack decisions (confirmed):**

- LLM: **Amazon Bedrock** (region `us-east-1`, confirmed available), Converse API for a single code path across model families.
- Backend: **FastAPI** (Python 3.12) with Server-Sent Events for live progress.
- Frontend: single-page app served by the same FastAPI app (React 18 + Tailwind via CDN, no build step).
- Deployment: **AWS Lambda container image + Function URL** with response streaming (scales to zero).
- Model policy: **quality first** - default to a strong Claude model for the reasoning stage, cheap Nova models for mechanical stages, full model switching and live cost display in the UI.

---

## 1. Why the pipeline is decomposed (the constraint)

A naive solution pastes both schemas into one prompt. That is forbidden, and it is also genuinely worse: it invites hallucinated destination paths, gives no confidence calibration, and produces one giant blob you cannot validate or retry field-by-field.

Instead the work is decomposed so that **no single prompt contains both full schemas, and no single call produces the finished mapping**. Stated precisely, because this is the load-bearing claim: any semantic matcher must see fragments of both sides somewhere, so each prompt carries a small, bounded slice - never the whole of either schema, and never enough to answer the whole problem in one shot:

- Stage 1 sees only *table names and column-name lists* (no types, no comments, no structure) to decide which table pairs with which collection. It returns pairings, not mappings.
- Stage 3 sees, per call, *one batch of ~8 source fields from one table* plus, for each of those fields, only its *top-6 pre-shortlisted destination paths* - at most 48 destination paths against 74 total fields, and typically far fewer.
- Stage 4 tie-breaks see only the two conflicting candidates.

The write-up states this formulation explicitly and quantifies it from the prompt trace (largest prompt of the run: fields, paths, tokens, versus the ~3,900-token both-schemas counterfactual), so a reviewer probing the constraint finds the argument already made.

The heavy lifting of "which destination paths are even plausible" is done **deterministically in code** (Stage 2), which is cheaper, faster, auditable, and makes hallucinated paths impossible to slip through.

## 2. System architecture

> Every diagram in this document appears twice: a plain-text flowchart that reads correctly anywhere, and the same graph as mermaid for a rich preview (`Ctrl+Shift+V`). The mermaid versions will also be exported to `project_plans/diagrams/*.png` and `*.svg` via `npx -y @mermaid-js/mermaid-cli` and embedded as images, so the colored form works in the raw editor, on GitHub, and in the write-up.

```text
                    +---------------------------------------------+
                    |               BROWSER (SPA)                 |
                    |  schema input | model picker | results grid |
                    |  cost meter   | prompt trace                |
                    +------+--------------------------+-----------+
                           | POST /api/map            ^ SSE events
                           v                          |
 +-------------------------+--------------------------+----------------------+
 |            FASTAPI APP   (local uvicorn OR Lambda container)              |
 |                                                                          |
 |  +---------------+     +----------------+     +---------------------+     |
 |  | [0] NORMALIZE |---->| [2] SHORTLIST  |---->| [4] VALIDATE/REPAIR |     |
 |  |    no LLM     |     |    no LLM      |     |      no LLM         |     |
 |  +-------+-------+     +-------+--------+     +----------+----------+     |
 |          |                     |                         |               |
 |          +----------+----------+                         |               |
 |                     v                                    |               |
 |          +------------------------+                      |               |
 |          |     ORCHESTRATOR       |                      |               |
 |          +----+--------------+----+                      |               |
 |               | prompt       ^ reply                      |              |
 |               v              |                            |              |
 |     +------------------+  +--+---------------+            |              |
 |     | RESPONSE CACHE   |  |   COST LEDGER    |            |              |
 |     +--------+---------+  +--------+---------+            |              |
 +--------------|--------------------|----------------------|---------------+
                | InvokeModel        ^ token usage          v
                v                    |            +----------------------+
     +--------------------------+     |            |  outputs/            |
     |     AMAZON BEDROCK       |-----+            |   mapping_*.json     |
     |  Converse API, us-east-1 |                  |   run_report.json    |
     +--------------------------+                  +----------------------+
```

Same graph in mermaid:

```mermaid
flowchart TB
    subgraph client [Browser]
        UI["SPA: schema input, model picker,<br/>results table, cost meter, prompt trace"]
    end

    subgraph app ["FastAPI app (single artifact: local uvicorn OR Lambda container)"]
        API["REST + SSE endpoints<br/>/api/map, /api/models, /api/samples"]
        ORCH["Pipeline orchestrator<br/>stage events + progress"]
        NORM["Stage 0: normalize<br/>(pure Python)"]
        CAND["Stage 2: candidate retrieval<br/>(lexical + optional embeddings)"]
        VALID["Stage 4: validate / repair<br/>(pydantic + JSON Schema)"]
        LEDGER["Cost ledger<br/>tokens to USD per stage"]
        CACHE["Content-hash response cache"]
    end

    subgraph aws [AWS]
        BR["Amazon Bedrock Converse API<br/>us-east-1"]
        CW["CloudWatch Logs<br/>7-day retention"]
    end

    subgraph out [Artifacts]
        JSON["mapping output JSON"]
        REPORT["run_report.json<br/>tokens, cost, latency, traces"]
    end

    UI -->|POST /api/map| API
    API --> ORCH
    ORCH --> NORM --> CAND --> ORCH
    ORCH -->|"small scoped prompts only"| CACHE --> BR
    BR --> LEDGER
    ORCH --> VALID --> JSON
    LEDGER --> REPORT
    API -.->|"SSE: stage, progress, cost, done"| UI
    ORCH --> CW

    classDef browser fill:#1e3a8a,stroke:#60a5fa,stroke-width:2px,color:#eff6ff
    classDef backend fill:#065f46,stroke:#34d399,stroke-width:2px,color:#ecfdf5
    classDef cloud fill:#7c2d12,stroke:#fb923c,stroke-width:2px,color:#fff7ed
    classDef artifact fill:#4c1d95,stroke:#a78bfa,stroke-width:2px,color:#f5f3ff
    classDef determ fill:#134e4a,stroke:#2dd4bf,stroke-width:2px,color:#f0fdfa

    class UI browser
    class API,ORCH,LEDGER,CACHE backend
    class NORM,CAND,VALID determ
    class BR,CW cloud
    class JSON,REPORT artifact
```

## 3. Pipeline stages

```text
      +--------------------+          +----------------------+
      | Dataset A: MySQL   |          | Dataset B: MongoDB   |
      |    legacy_hrm      |          |   people_platform    |
      +---------+----------+          +----------+-----------+
                |                                |
                +----------------+---------------+
                                 v
          +--------------------------------------------------+
          | [0] NORMALIZE                          no LLM    |
          | flatten nested paths to dot notation,            |
          | expand abbreviations, build valid path set       |
          +-----------------------+--------------------------+
                                  v
          +--------------------------------------------------+
          | [1] ROUTE                              LLM  x1   |
          | in : table names + column NAMES only             |
          | out: 3 table -> collection pairings              |
          +-----------------------+--------------------------+
                                  v
          +--------------------------------------------------+
          | [2] SHORTLIST                          no LLM    |
          | top-6 destination paths per source field,        |
          | scored inside its matched collection only        |
          +-----------------------+--------------------------+
                                  v
          +--------------------------------------------------+
          | [3] ADJUDICATE                         LLM  xN   |<--+
          | one call per ~8 source fields, each field        |   |
          | carrying only its own 6 candidates + null        |   | repair
          +-----------------------+--------------------------+   | retry
                                  v                              |
          +--------------------------------------------------+   |
          | [4] VALIDATE & REPAIR                  no LLM    +---+
          | JSON Schema, hallucinated-path guard,            |
          | collision tie-break, coverage assertion          |
          +-----------------------+--------------------------+
                                  v
          +--------------------------------------------------+
          | [5] ASSEMBLE                           no LLM    |
          +----------+----------------------------+----------+
                     v                            v
          +---------------------+     +-------------------------+
          |   mapping JSON      |     |    run_report.json      |
          | required contract   |     | tokens, USD, traces     |
          +---------------------+     +-------------------------+
```

Same graph in mermaid:

```mermaid
flowchart LR
    A["Dataset A<br/>MySQL DDL/JSON"] --> S0
    B["Dataset B<br/>MongoDB JSON"] --> S0

    S0["Stage 0 - NORMALIZE<br/>no LLM<br/>flatten nested paths,<br/>expand abbreviations"]
    S1["Stage 1 - ROUTE<br/>LLM x1, names only<br/>table to collection"]
    S2["Stage 2 - SHORTLIST<br/>no LLM<br/>top-6 candidates per field"]
    S3["Stage 3 - ADJUDICATE<br/>LLM x N batches<br/>8 fields + their candidates"]
    S4["Stage 4 - VALIDATE & REPAIR<br/>schema check, path guard,<br/>collision tie-break"]
    S5["Stage 5 - ASSEMBLE<br/>final JSON + cost report"]

    S0 --> S1 --> S2 --> S3 --> S4 --> S5
    S4 -->|"invalid path or low confidence"| S3

    classDef det fill:#134e4a,stroke:#2dd4bf,stroke-width:2px,color:#f0fdfa
    classDef llm fill:#831843,stroke:#f472b6,stroke-width:2px,color:#fdf2f8
    classDef io fill:#1e3a8a,stroke:#60a5fa,stroke-width:2px,color:#eff6ff

    class A,B io
    class S0,S2,S4,S5 det
    class S1,S3 llm
```

### Stage 0 - Normalize (deterministic)

Parse both schemas into an intermediate representation. MongoDB nesting is flattened to dot-notation paths up front, which is what makes `fullName.firstName` a first-class candidate rather than something the model has to invent.

```python
SourceField(table, name, sql_type, nullable, key, fk_target, comment)
DestField(collection, path, bson_type, comment, is_ref)
```

Also builds an abbreviation lexicon used by Stage 2 (`f_name` -> first name, `dob` -> date of birth, `cd` -> code, `nm` -> name, `dt` -> date, `ts` -> timestamp, `sal` -> salary, `mgr` -> manager, `lvl` -> level, `stat` -> status, `ctr` -> center, `tz` -> timezone).

### Stage 1 - Table routing (1 cheap LLM call)

Prompt carries only table names + bare column-name lists and collection names + top-level field names. Returns the three pairings with confidence and one-sentence reasoning. Runs on **Nova Lite** - it is a 3-way matching problem, not a reasoning problem.

### Stage 2 - Candidate shortlisting (deterministic, free)

For each source field, score **only destination paths inside its matched collection**:

- normalized token overlap after abbreviation expansion,
- trigram / Jaro-Winkler string similarity,
- type-compatibility bonus (`DATE`/`DATETIME` -> `ISODate`, `TINYINT(1)` -> `Boolean`, `CHAR(1)` -> `String` enum, `INT PK/FK` -> `ObjectId`),
- key-role bonus (PK -> `_id`, FK -> `*Id` ref paths),
- optional Bedrock **Titan Embeddings v2** cosine similarity (toggle in UI, ~$0.00002 per run).

Keeps top 6. This is the cost lever: prompts stay ~300-500 tokens instead of thousands.

### Stage 3 - Field adjudication (LLM, batched)

One call per ~8 source fields of one table. Each field arrives with its own shortlist plus an explicit `null` option for "no good match". Output is forced through a **Converse tool-use JSON schema**, so the response is structurally valid by construction. Temperature 0. Runs on the user-selected mapper model (default Claude Sonnet class).

Returned `destination_field` is checked against the Stage 0 path set - a path that does not exist cannot survive.

### Stage 4 - Validate & repair (deterministic + at most tiny LLM calls)

- pydantic + JSON Schema validation of every record against the required output contract,
- reasoning-format check: the assignment requires `reasoning` to be *one plain-English sentence*, so a sentence-count and length validator rejects multi-sentence or rambling reasoning and repairs it with a single cheap rewrite call,
- hallucinated-path guard, with a single-field retry for anything rejected,
- collision resolution when two source fields claim one destination path (highest confidence wins; if within 0.05, one tiny tie-break call containing just that pair),
- coverage assertion: every source field appears either in `field_mappings` or in `unmapped_source_fields`, and every destination path appears either as a target or in `unmapped_destination_fields`. This is what guarantees the assignment's "every field across all three source tables".

### Stage 5 - Assemble

Emits `outputs/mapping_legacy_hrm_to_people_platform.json` exactly in the required shape, plus `outputs/run_report.json` with per-stage model, token counts, latency, USD cost, cache hits, and the full text of every prompt sent (the prompt trace doubles as evidence that the constraint was respected).

### Stage 3b - Model cascade (cost lever)

Every field first runs through a cheap model (Nova Lite or Haiku). Only fields returning confidence below a threshold (default 0.80) are re-adjudicated by the strong model. Typically 80-90% of fields resolve on the cheap pass, cutting run cost by roughly two thirds while keeping strong-model judgment where the decision is genuinely hard. Both passes are recorded, so the escalation rate is a reportable metric.

### Stage 3c - Evaluator-optimizer reflection

A bounded critic pass (one iteration, no loops) reviews only mappings still below 0.75 after the cascade. It sees just that field, its candidate set, and the first pass's reasoning, then confirms or revises. It touches a handful of fields per run, so the cost is negligible and it lifts the weakest decisions.

### Conventions knowledge pack (retrieval that earns its keep)

A small local corpus - HR domain abbreviations, ISO standards (4217 currency, 3166-1 country, IANA timezones), naming-convention rules (`snake_case` to `camelCase`, `*_cd` to `code`), canonical MySQL-to-BSON type mappings, and human-approved mappings accumulated from the UI. Three to five relevant snippets are retrieved per batch and injected alongside the candidates. This improves consistency, gives the model an authority to cite in `reasoning`, and closes a feedback loop: a mapping a user marks verified is stored and retrieved as a few-shot exemplar on later runs.

No vector database. The corpus and the schema itself are tiny, so retrieval is in-memory with lexical scoring plus Titan embeddings. Standing up OpenSearch Serverless for 74 fields would cost hundreds of dollars a month to index something that fits on a napkin.

## 3.5 AI architecture and agentic patterns

The patterns deliberately used, and the one deliberately rejected:

- **Prompt chaining with task decomposition** - the six-stage pipeline, which is what satisfies the assignment constraint.
- **Model routing by role** - cheap model for mechanical routing, strong model for semantic judgment.
- **Retrieval-augmented generation** - Stage 2 candidate shortlisting plus the conventions knowledge pack. Retrieve a scoped context, then generate.
- **Constrained decoding** - Converse tool-use JSON schemas, so output is structurally valid by construction.
- **Orchestrator-worker fan-out** - the orchestrator dispatches per-table, per-batch mapping workers and collects results.
- **Evaluator-optimizer reflection** - bounded critic pass on low-confidence fields.
- **Tool-scoped access as constraint enforcement** - the mapper calls `lookup_candidates(field_id)` rather than receiving pasted data, and no tool is capable of returning a whole schema. The constraint is enforced by code, and the prompt trace proves it.
- **Validators as code** - JSON Schema, destination-path allowlist, collision detection, coverage assertion. Guardrails live in Python, not in prompt wording.
- **Human-in-the-loop feedback as memory** - verified overrides become retrievable exemplars.
- **Determinism** - temperature 0 plus a content-hash response cache, so a rerun is reproducible and free. The cache is S3-backed in Lambda (a `/tmp` disk cache would not survive cold starts, silently breaking cache hits and resume-from-stage) and disk-backed locally behind the same interface.

Fixed thresholds shared by the pipeline, UI, and write-up: confidence bands at 0.90 and above (high), 0.80 to 0.89 (medium), below 0.80 (review); cascade escalation below 0.80; reflection below 0.75; collision tie-break when two confidences are within 0.05; candidate shortlist of 6 with a 0.15 minimum score, and no forced match when nothing qualifies.

**Rejected: an autonomous agent as the primary architecture.** The workflow is known and fixed, so there is nothing to discover; an agent loop adds latency, cost variance, and nondeterminism to a task that reviewers want reproducible; and an agent holding a schema-fetching tool could pull both schemas into its own context, which is exactly what the assignment forbids. Handing that decision to a model means the constraint can no longer be proven. Bedrock Agents as a managed service is skipped for the same reason - it obscures the exact prompt content the constraint proof depends on.

### Agent mode (experimental, built last)

To make that rejection a measurement rather than an assertion, a toggle runs the same schemas through a genuine tool-calling agent loop over the **identical scoped tool surface**: `list_tables()`, `get_source_fields(table)`, `lookup_candidates(field_id)`. No tool can return a whole schema, so agent mode stays compliant and the comparison is honest - both paths see the same information and differ only in who controls sequencing. Hard caps: 40 tool calls, a token budget, temperature 0, and the same output contract so the diff view works unchanged.

Expected finding is three to five times the tokens for similar or slightly worse consistency, which costs pennies to demonstrate. If the agent wins on specific fields, the write-up reports that and explains why. This is the first feature cut if time runs short.

## 4. Cost model

Live us-east-1 on-demand rates, per 1M tokens, held in a registry so the UI can price any run:

- Amazon Nova Micro - $0.035 in / $0.14 out
- Amazon Nova Lite - $0.06 in / $0.24 out
- Amazon Nova Pro - $0.80 in / $3.20 out
- Claude Haiku 4.5 - $1.00 in / $5.00 out
- Claude Sonnet 4.6 - $3.00 in / $15.00 out
- Titan Embeddings v2 - $0.02 in

A full 3-table run is roughly 12-18k input and 4-6k output tokens after shortlisting:

- Nova Lite router + Claude Sonnet mapper: **~$0.10-0.14 per run**
- Nova Lite router + Claude Haiku mapper: **~$0.03-0.05 per run**
- Cascade (Haiku first, Sonnet only below 0.80): **~$0.04-0.06 per run**, the default
- All Nova Lite: **~$0.002 per run**
- Agent mode, for comparison only: roughly 3-5x the equivalent pipeline run

Infrastructure is effectively free: Lambda at 1 GB for ~30 s per run stays inside the free tier for demo traffic, Function URLs cost nothing, ECR storage for a ~400 MB image is about **$0.04/month**, and CloudWatch logs are capped with 7-day retention. Persistence adds almost nothing measurable - S3 holds a few kilobytes of JSON per run, and DynamoDB on-demand at $1.25 per million writes means a thousand runs cost about **$0.00125**. Expect **under $1/month** all-in for a shared demo, dominated by whatever Bedrock tokens testers spend.

Guardrails: reserved concurrency of 2, a per-run token ceiling, an AWS Budgets alert at $5, and a shared access token on the Function URL so the demo link cannot be used to burn your Bedrock quota.

## 5. UI

Full design, including the complete data inventory, states, and testing: [docs/superpowers/specs/2026-08-11-schema-mapper-ui-design.md](../docs/superpowers/specs/2026-08-11-schema-mapper-ui-design.md).

The centerpiece is an **interactive mapping graph** rather than a data grid: source fields on the left, the destination schema as a collapsible tree on the right showing real nesting, and wires between them whose color and thickness encode confidence, dashed where a value transform is required. Because results stream over SSE, wires animate into place as Stage 3 batches return, so a reviewer watches the pipeline resolve field by field.

```text
 +--------------------------------------------------------------------------------+
 | Schema Field Mapper    [Run]   $0.11 | 14.2k/4.8k tok | constraint: 412 tok max|
 +-----------+--------------------------------------------------+-----------------+
 | INPUT     |          MAPPING GRAPH        [graph | grid]     | FIELD DETAIL    |
 | source    | emp_master            employees                  | rec_stat        |
 | 3 tbl     |  emp_cd    =========> employeeCode        0.99    |  CHAR(1)        |
 | 33 col    |  f_name    =====\                                 | -> employment   |
 |           |  l_name    ===\  \==> fullName                    |    .status 0.95 |
 | dest      |  dob        ==\ \===>   .firstName       0.98     |                 |
 | 3 coll    |  rec_stat  ~~~~\ \==>   .lastName        0.98     | candidates:     |
 | 41 path   |  is_remote  ==\ \===> employment          --      |  .status  0.91  |
 |           |                \ \==>   .status          0.95     |  .jobLevel 0.22 |
 | [sample]  |                 \===>   .isRemote        0.99     |  .isRemote 0.11 |
 |           |                                                   |                 |
 | MODELS    | thick/green = high conf   ~~ = value transform     | transform:      |
 | router    |                                                   |  A -> active    |
 | mapper    | STAGE RAIL  [0]-[1]-[2]-[3 3/5]-[4]-[5]           |  I -> inactive  |
 | tiebreak  |                                                   |  T -> terminated|
 | [agent?]  | ARENA: Sonnet vs Haiku -> 31/33 agree, 2 differ    | [verify][edit]  |
 +-----------+--------------------------------------------------+-----------------+
 | Prompt trace | Constraint proof | Cost | Transform playground | History | Diff  |
 +--------------------------------------------------------------------------------+
```

Signature features beyond the graph:

- **Candidate race view** - selecting a field shows its top-6 shortlist as competing ghost wires with lexical, embedding, type-compatibility, and key-role score breakdowns, and the model's pick highlighted.
- **Constraint meter** - live readout of the largest prompt in the run (fields, candidate paths, tokens) against the counterfactual token count of a both-schemas prompt.
- **Transform playground** - edit a sample source row and watch the target document render with transforms applied client-side, each output field annotated with the rule that produced it.
- **Model arena** - overlay runs from different models on one graph; agreements mute, disagreements glow, with confidence, latency, and cost deltas.
- **Agent mode toggle** - experimental pipeline-vs-agent comparison, per section 3.5.

A grid view stays available as a toggle for reading long reasoning text and exporting.

### Persistence

S3 holds full artifacts per run (mapping JSON, run report, prompt traces) and is written first. DynamoDB holds only a ~1 KB index item per run for the history list and arena comparisons, written after S3 succeeds; an indexed run whose artifacts are gone renders as expired. On-demand billing, a GSI for the recent-runs query (never `Scan`), 30-day TTL on both stores, and Streams, point-in-time recovery, and global tables left off.

## 6. Deployment

```text
   +----------------------+        +-----------------------------------+
   | Local dev            |        | Docker image                      |
   | uvicorn + .env creds |------->| python:3.12-slim +                |
   +----------------------+        | AWS Lambda Web Adapter            |
                                   +-----------------+-----------------+
                                                     | docker push
                                                     v
                                   +-----------------------------------+
                                   | Amazon ECR                        |
                                   +-----------------+-----------------+
                                                     | sam deploy
                                                     v
   +----------------------+        +-----------------------------------+
   | Testers (browser)    |<------>| Function URL (HTTPS)              |
   +----------------------+        | shared access token               |
                                   +-----------------+-----------------+
                                                     v
                                   +-----------------------------------+
                                   | AWS LAMBDA                        |
                                   | 1024 MB | 300 s | RESPONSE_STREAM |
                                   +------+---------------------+------+
                                          |                     |
                                          v                     v
                        +------------------------+   +------------------------+
                        | Amazon Bedrock         |   | CloudWatch Logs        |
                        | execution role only,   |   | 7-day retention        |
                        | allowed model ARNs     |   +------------------------+
                        +------------------------+
                                          |
                        +-----------------+-----------------+
                        v                                   v
              +--------------------+            +------------------------+
              | Amazon S3          |            | Amazon DynamoDB        |
              | run artifacts      |            | run index (on-demand)  |
              | 30-day lifecycle   |            | GSI + 30-day TTL       |
              +--------------------+            +------------------------+
```

Same graph in mermaid:

```mermaid
flowchart LR
    DEV["Local dev<br/>uvicorn + .env creds"] --> IMG["Docker image<br/>python:3.12-slim + Lambda Web Adapter"]
    IMG --> ECR["Amazon ECR"]
    ECR --> LAM["Lambda function<br/>1024 MB, 300 s timeout,<br/>InvokeMode RESPONSE_STREAM"]
    LAM --> FURL["Function URL (HTTPS)<br/>shared access token"]
    FURL --> USERS["Testers"]
    LAM -->|"IAM role, no static keys"| BR["Bedrock<br/>InvokeModel on allowed model ARNs"]
    LAM --> LOGS["CloudWatch Logs<br/>7-day retention"]

    classDef dev fill:#1e3a8a,stroke:#60a5fa,stroke-width:2px,color:#eff6ff
    classDef aws fill:#7c2d12,stroke:#fb923c,stroke-width:2px,color:#fff7ed
    classDef user fill:#4c1d95,stroke:#a78bfa,stroke-width:2px,color:#f5f3ff

    class DEV,IMG dev
    class ECR,LAM,FURL,BR,LOGS aws
    class USERS user
```

Lambda is the right fit here: the workload is bursty demo traffic, idle cost is zero, and the Lambda Web Adapter lets the identical FastAPI app run locally and in Lambda with SSE streaming intact. Deployment is a SAM template (`infra/template.yaml`) plus `sam deploy --guided`.

Prerequisites to install before deploying: AWS CLI (`winget install Amazon.AWSCLI`) and SAM CLI (`pip install aws-sam-cli`). Docker is already present.

**EC2 alternative** (documented, not default): same container on a `t4g.small` behind Caddy for TLS. Simpler to reason about and fine on free tier, but always-on and roughly $6-12/month afterwards, plus you own patching and TLS renewal.

## 7. Repository layout

```
.env.sample                      # documented placeholders, no secrets
requirements.txt
project_plans/schema_field_mapper_plan.md
project_plans/diagrams/          # architecture|pipeline|deployment .mmd + .png + .svg + render.ps1
data/schemas/legacy_hrm.mysql.json
data/schemas/people_platform.mongo.json
data/knowledge/conventions.json  # ISO standards, abbreviations, type map, approved exemplars
src/schema_mapper/
  config.py        # env, model registry + pricing, role defaults, cascade thresholds
  models.py        # pydantic contract for the output JSON
  normalize.py     # Stage 0
  candidates.py    # Stage 2
  knowledge.py     # conventions retrieval + verified-override exemplars
  prompts.py       # per-stage templates
  tools.py         # scoped tool surface: list_tables, get_source_fields, lookup_candidates
  bedrock.py       # Converse client, retries, usage capture, cache
  pipeline.py      # orchestrator, cascade, reflection, emits progress events
  agent.py         # experimental tool-calling agent loop over the same scoped tools
  validate.py      # Stage 4
  cost.py          # token -> USD ledger
  store.py         # S3 artifacts + DynamoDB run index
  cli.py           # headless run, same code path as the API
api/main.py        # FastAPI + SSE
api/static/index.html
tests/             # normalize, candidates, validate, contract, golden output
infra/Dockerfile, infra/template.yaml, infra/DEPLOY.md
outputs/           # generated mapping + run report
docs/WRITEUP.md    # required write-up
```

## 8. Secrets handling

`.env` stays gitignored (already covered by `.gitignore`) and is used for local dev only. In Lambda, Bedrock access comes from the execution role, not from keys. The current `.env` carries credentials from another project (Stripe, Google, proxy, OpenAI); since this repo will be shared with reviewers, it should be trimmed to the AWS-only values below - the user will refresh `.env` once `.env.sample` exists. A new `.env.sample` documents what is needed:

```
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=your-access-key-id          # local dev only
AWS_SECRET_ACCESS_KEY=your-secret-access-key  # local dev only
BEDROCK_MAPPER_MODEL=us.anthropic.claude-sonnet-4-6-v1:0
BEDROCK_ROUTER_MODEL=us.amazon.nova-lite-v1:0
BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
ENABLE_EMBEDDINGS=true
ENABLE_RESPONSE_CACHE=true
MAX_TOKENS_PER_RUN=120000
APP_ACCESS_TOKEN=change-me-for-shared-demo
LOG_LEVEL=INFO
```

Model IDs are validated at startup against `bedrock:ListFoundationModels` / inference profiles, so a wrong or not-yet-enabled model surfaces as a clear error in the UI rather than a runtime 400 mid-run.

## 9. Deliverables mapped to the assignment

- **Working pipeline code** - `src/schema_mapper/` plus a headless CLI and the FastAPI/UI wrapper.
- **Generated output JSON** - `outputs/mapping_legacy_hrm_to_people_platform.json`, committed. The golden test pins the exact header strings the assignment specifies - `"source": "legacy_hrm (MySQL)"`, `"destination": "people_platform (MongoDB)"`, `"mapping_version": "1.0"`, ISO-8601 `generated_at` - alongside the full structural contract, since the assignment says "matches this schema exactly".
- **Write-up** - `docs/WRITEUP.md`: prompt structure per stage, the precise constraint formulation from section 1 with numbers from the prompt trace, why retrieval is deterministic, confidence calibration, cost/model trade-offs, and what would change at production scale.

## 10. Build order

Steps 1-7 are the assignment-complete milestone: pipeline code, committed output JSON, and a drafted write-up. Everything after is showcase, cuttable without touching a graded deliverable.

0. Export the three diagrams to PNG/SVG under `project_plans/diagrams/` and embed them here.
1. Normalized schema fixtures + IR + tests.
2. Output contract models and JSON Schema validation, golden test pinning exact header strings.
3. Bedrock Converse client with usage capture, retries, cache, and cost ledger.
4. Deterministic candidate retrieval + scoped tool surface + tests.
5. Conventions knowledge pack and retrieval.
6. Prompts and orchestrator for Stages 1, 3, 4, 5, including cascade, reflection, and the reasoning-format validator; produce the committed output JSON via CLI.
7. **Draft `docs/WRITEUP.md`** while the prompt decisions are fresh - the write-up is a graded deliverable, the UI is not. Finalized in step 13.

--- assignment complete; showcase below ---

8. FastAPI API with SSE, then the SPA shell and the mapping graph.
9. Remaining UI surfaces: candidate race, constraint meter, cost panel, transform playground, grid view, exports.
10. Persistence (S3 + DynamoDB), history list, and the arena/diff view.
11. Dockerfile, SAM template, deploy notes, budget guardrails.
12. Experimental agent mode plus the pipeline-vs-agent comparison.
13. Finalize write-up (add agent-mode findings and UI notes) and README.
