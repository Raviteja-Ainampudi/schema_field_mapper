# Schema Field Mapper - UI Design Spec

Date: 2026-08-11
Status: awaiting user review
Related: [project_plans/schema_field_mapper_plan.md](../../../project_plans/schema_field_mapper_plan.md)

## 1. Purpose and audience

The UI serves two audiences at once, so transparency is the default and editing is available:

- **Interview reviewers** need to see that this is a genuine decomposed pipeline, not one large prompt. The UI must make stages, prompts, candidate shortlists, confidence, and coverage visible and verifiable.
- **Data engineers** need to bring their own schemas, correct mappings by hand, verify transforms against a sample row, and export migration-ready artifacts.

Success criteria: a reviewer reaches a complete, correct mapping in two clicks from a cold load; the "no single prompt" constraint is provable from the UI without reading code; and a mapping decision can be traced to the exact prompt, candidate set, and model that produced it.

## 2. Core concept: the mapping graph

The centerpiece is an **interactive bipartite mapping graph**, not a data grid. Source fields on the left, the destination schema on the right as a collapsible tree that shows real nesting (`fullName` expanding into `firstName` and `lastName`), with wires between them.

- Wire color and thickness encode confidence, so the quality of an entire mapping is readable at a glance.
- Dashed wires mean a value transform is required; solid means a straight type-compatible move.
- Because results arrive over SSE, **wires animate into place as Stage 3 batches return**. The reviewer watches the pipeline resolve field by field, which demonstrates the decomposition far better than prose.
- A grid view remains available as a toggle, because reading long `reasoning` text and exporting are better in a table.

### Layout

```text
 +--------------------------------------------------------------------------------+
 | Schema Field Mapper    [Run]   $0.11 | 14.2k/4.8k tok | constraint: 412 tok max|
 +-----------+--------------------------------------------------+-----------------+
 | INPUT     |          MAPPING GRAPH        [graph | grid]     | FIELD DETAIL    |
 | +-------+ |                                                  |                 |
 | |source | | emp_master            employees                  | rec_stat        |
 | |schema | |  emp_cd    =========> employeeCode        0.99    |  CHAR(1)        |
 | |3 tbl  | |  f_name    =====\                                 | -> employment   |
 | |34 col | |  l_name    ===\  \==> fullName                    |    .status 0.95 |
 | +-------+ |  dob        ==\ \===>   .firstName       0.98     |                 |
 | +-------+ |  rec_stat  ~~~~\ \==>   .lastName        0.98     | candidates:     |
 | |dest   | |  is_remote  ==\ \===> employment          --      |  .status  0.91  |
 | |schema | |                \ \==>   .status          0.95     |  .jobLevel 0.22 |
 | |3 coll | |                 \===>   .isRemote        0.99     |  .isRemote 0.11 |
 | |40 path| |                                                   |                 |
 | +-------+ | thick/green = high confidence                     | transform:      |
 | [sample]  | thin/amber  = review     ~~ = value transform     |  A -> active    |
 |           |                                                   |  I -> inactive  |
 | MODELS    | STAGE RAIL  [0]-[1]-[2]-[3 3/5]-[4]-[5]           |  T -> terminated|
 | router    |                                                   |                 |
 | mapper    | ARENA: Sonnet vs Haiku  -> 31/33 agree, 2 differ   | [verify][edit]  |
 | tiebreak  |                                                   |                 |
 +-----------+--------------------------------------------------+-----------------+
 | Prompt trace | Constraint proof | Cost | Transform playground | History | Diff  |
 +--------------------------------------------------------------------------------+
```

Three panes (Input, Graph, Field Detail) with a tab strip along the bottom for the deeper views. Cold load lands on Input with a prominent "Load assignment sample" so a reviewer never has to find or paste data.

## 3. Signature features

### 3.1 Candidate race view

Selecting a source field renders its top-6 shortlist as competing ghost wires, each with a score breakdown: lexical similarity, embedding similarity, type compatibility, key-role bonus. The model's chosen wire is highlighted. This makes the division of labor explicit - deterministic code narrows the field, the LLM adjudicates - and it is the clearest possible answer to "how do you know it isn't hallucinating?"

### 3.2 Constraint meter

A persistent readout of the largest prompt in the run: field count, candidate-path count, token count, alongside the counterfactual token count of a both-schemas prompt. The assignment constraint becomes a number in the header rather than a claim in a README.

### 3.3 Transform playground

A sample `emp_master` row can be edited, and the resulting MongoDB document renders live with transforms actually applied client-side (`A` to `active`, `0/1` to boolean, `DATETIME` to ISODate, `DECIMAL` to Number). Each output field is annotated with the rule that produced it. Anything not mechanically executable (for example ObjectId generation or denormalized lookups) is flagged as manual rather than faked.

### 3.4 Model arena

Runs of the same schema pair under different models overlay on one graph: agreeing wires stay muted, disagreements glow, and a footer shows confidence, latency, and cost deltas. This is the most decision-useful view for both audiences, and it is what justifies persisting runs.

### 3.5 AI-architecture surfaces

The pipeline's architecture decisions need UI representation, otherwise they are invisible:

- **Cascade indicator.** Each field shows which pass decided it - cheap model, escalated to the strong model, or revised by reflection - with the run-level escalation rate displayed alongside cost. This makes the cost lever legible.
- **Reflection badge.** Fields revised by the critic pass show both the original and revised decision, so a reviewer can judge whether reflection helped.
- **Knowledge citations.** When a decision draws on the conventions pack (ISO 4217, `snake_case` to `camelCase`, a prior verified override), the field detail lists which snippets were retrieved.
- **Agent mode toggle.** Runs the same schemas through the experimental tool-calling agent over the identical scoped tools, then renders the result in the arena beside the pipeline run with tokens, latency, confidence, and disagreement count. Labelled experimental, and never the source of the committed output. Its tool-call sequence is viewable in the prompt trace, along with the context reset between bounded subtasks - the reset, not the tool scoping, is what keeps agent mode inside the constraint, since a continuous loop would accumulate both schemas in one context window.

## 4. Data inventory

### Input and parse metadata

Raw source and destination schema text; detected format (MySQL DDL, MySQL JSON, MongoDB JSON, sample document); parse summary (table and column counts, collection and flattened-path counts, max nesting depth); per-error line and column for parse failures; a schema fingerprint hash used by the cache and history.

### Run configuration

Model per role (router, mapper, tie-break) with friendly name, Bedrock model ID, and per-token price; region; embeddings toggle and embedding model; response cache toggle; batch size; top-K; temperature (pinned to 0 and displayed); max token budget; pre-run cost estimate derived from candidate counts.

### Live telemetry

Per-stage status, start time, duration, model; Stage 3 batch progress (table i of n, fields x to y); each mapping as it resolves, for wire animation; running input and output token counters; running USD; cache hit count; retry and repair events with reasons; a raw log stream.

### Table-level results

`source_table`, `destination_collection`, table `confidence`, table `reasoning`, mapped-over-total source fields, covered-over-total destination paths, both unmapped lists with counts.

### Field-level record

Contract fields (`source_field`, `destination_field`, `type_transform`, `confidence`, `reasoning`, `notes`) plus review context: source SQL type, nullability, key role (PK, FK with target, unique), column comment; destination BSON type and whether it is a reference; confidence band for coloring; whether a value transform exists (drives dashed wires).

### Decision provenance

Top-6 candidates with component scores (lexical, embedding, type compatibility, key-role); model that produced the decision; which cascade pass decided it (cheap, escalated, reflection-revised) with the prior decision retained when revised; retrieved knowledge-pack snippet IDs; batch and prompt identifier; flags for repaired, tie-broken, human-overridden.

### Constraint proof

Per prompt, the recorded manifest: source-table count, source-field count, candidate-path count, token count, and schema fingerprints touched. Run-level maxima for each, total LLM call count, and the both-schemas counterfactual token count. The panel renders pass or fail against the assertions in plan section 8.5 (no request holding all 34 source fields or all 40 destination paths), so the constraint reads as a verified check rather than a displayed statistic.

### Cost accounting

Per stage: model, call count, input and output tokens, USD. Run totals and cost per mapped field. A what-if projection repricing the recorded token counts against every other model in the registry.

### Validation diagnostics

JSON Schema pass or fail with violations; hallucinated paths caught and retried; collisions detected and their resolution; coverage assertion result; confidence distribution histogram; a reason for every unmapped field (no candidate above threshold, target-generated such as `_id`, denormalized copy such as `department.name`, or genuinely absent).

### History and arena

DynamoDB index fields: run ID, timestamp, models per role, execution mode (pipeline or agent), schema names and fingerprints, field count, mean confidence, coverage, escalation rate, tokens, cost, duration. For comparisons: per-field agreement matrix across runs with confidence and cost deltas.

### Transform playground

Sample source row (prefilled with realistic values), interpreter output document, per-field applied-rule annotations, manual-only markers.

### Exports

Exact-contract mapping JSON, run report JSON, CSV of field mappings, shareable run link.

## 4.1 Thresholds and definitions

Fixed so the UI, the pipeline, and the write-up agree on one set of numbers:

- **Confidence** is a blend, not the model's self-report: `0.6 * model_confidence + 0.4 * normalized_retrieval_margin`, with a type-incompatibility penalty and a 0.85 cap on non-expressible transforms. The field detail shows both components so the number is inspectable. Table confidence is the mean of its field confidences scaled by source-field coverage.
- **Confidence bands** (wire color, grid badges): high is 0.90 and above, medium is 0.80 to 0.89, review is below 0.80.
- **Cascade escalation**: any field below 0.80 on the cheap pass is re-adjudicated by the strong model - that is, exactly the "review" band.
- **Reflection**: any field still below 0.75 after escalation gets one critic pass. No second iteration.
- **Collision tie-break**: confidences within 0.05 of each other go to a tie-break call; otherwise the higher confidence wins outright.
- **Candidate shortlist**: top 6 per field, and a candidate must clear a minimum score of 0.15 to be offered at all. A field with no qualifying candidate is reported unmapped rather than forced.

## 5. Persistence

- **S3** holds full artifacts per run: mapping JSON, run report, prompt traces. Written first.
- **DynamoDB** holds only the index item (about 1 KB) for the history list and arena comparisons. Written after S3 succeeds; a run present in the index but missing in S3 renders as expired.
- On-demand billing (`PAY_PER_REQUEST`), a GSI queried for the recent-runs list (never `Scan`), 30-day TTL on the table and a matching S3 lifecycle rule. Point-in-time recovery, Streams, and global tables stay off.
- **Verified overrides are exempt from expiry.** They live under a separate, non-expiring S3 prefix, because their entire value is being retrieved as exemplars on later runs. Only run artifacts age out.
- **The response cache is S3-backed**, keyed by content hash, not local disk. A Lambda instance's `/tmp` does not survive cold starts, so a disk cache would silently break both cache hits and the resume-from-stage recovery path. Local development still uses a disk cache behind the same interface.
- Cost at demo volume: roughly $0.001 per thousand runs in writes plus fractions of a cent in storage and reads. Negligible against Bedrock tokens.

## 6. States and error handling

- **Empty**: Input pane focused, sample loader prominent, Run disabled until both schemas parse.
- **Parse error**: inline messages with line and column; Run stays disabled so no tokens are spent on a bad paste.
- **Running**: partial results remain on screen; a later-stage failure preserves everything already produced.
- **`AccessDeniedException`**: rendered as "model not enabled in this account", with the Bedrock model-access console link and a suggestion to switch models.
- **`ThrottlingException`**: automatic retry with visible backoff countdown.
- **Timeout or crash**: offer resume-from-stage, which is nearly free because completed stages are cached.
- **Budget exceeded**: stop before the next call, show what was completed, and keep partial output exportable.

Dark mode is the default. The graph is keyboard navigable with a focus ring walking source fields, and the grid view is the accessible fallback for anything the SVG cannot express. Target: usable on a 1366-wide laptop, with the Field Detail pane collapsing to a drawer below that.

## 7. Technical approach

- No build step: React 18 and Tailwind from CDN, served as static assets by the same FastAPI app, so the deployable artifact stays a single Lambda container.
- The graph is hand-rolled SVG with bezier wires and no charting dependency, keeping bundle size and complexity down.
- SSE drives every live element; the client holds run state in memory and mirrors it to `localStorage` so a refresh mid-run does not lose completed stages.
- The transform interpreter is a small pure-JS module sharing rule names with the Python `type_transform` renderer, so playground output matches what the pipeline claims.
- Editing a mapping updates local state immediately and posts the override asynchronously; marking one verified writes it to the non-expiring override prefix, from which the knowledge pack picks it up on the next run.

## 8. Testing

- Python unit tests: normalize (nested flattening, type parsing, key roles), candidates (ranking, abbreviation expansion, type compatibility), validate (collision resolution, coverage assertion, hallucinated-path rejection).
- Golden-output test pinning the committed mapping JSON against the contract, plus the semantic oracle (33 expected pairs, `dob` unmapped, seven denormalized destination paths) and the shortlist-recall gate from plan section 8.5.
- Prompt-manifest assertions proving no single request saw all 34 source fields or all 40 destination paths.
- Bedrock stubbed from recorded cassettes so the suite runs offline and free, and the same cassettes drive `cli --offline` for reviewers without AWS credentials.
- Playwright smoke test: load the sample, run against a mocked SSE stream, assert wires render and export downloads.

## 9. Out of scope

Authentication beyond a shared access token, multi-user accounts, real database connections (schemas are pasted, never introspected live), automated migration execution, and mappings across more than one schema pair per run.
