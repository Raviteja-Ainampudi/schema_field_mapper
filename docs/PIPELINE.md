# Pipeline and workflows

What happens between "here are two schemas" and "here is a mapping you can review." For the
component structure see [ARCHITECTURE.md](ARCHITECTURE.md).

## The six stages

```mermaid
flowchart TD
    input["Source schema + destination schema<br/>4 accepted formats"]

    s0["Stage 0 - Normalize<br/><b>no model</b>"]
    s1["Stage 1 - Route<br/><b>1 call per table</b>"]
    s2["Stage 2 - Shortlist<br/><b>no model</b>"]
    s3["Stage 3 - Adjudicate<br/><b>1 call per batch of 8</b>"]
    s3c["Stage 3c - Reflect<br/><b>1 call per weak field</b>"]
    s4["Stage 4 - Validate<br/><b>no model</b>"]
    s5["Stage 5 - Assemble<br/><b>no model</b>"]

    out1["mapping.json<br/>the deliverable"]
    out2["run_report.json<br/>coverage, cost, proof"]
    out3["prompt_trace.json<br/>every prompt + manifest"]

    input --> s0
    s0 -->|"flat dot-path IR"| s1
    s1 -->|"table to collection"| s2
    s2 -->|"top 6 candidates per field"| s3
    s3 -->|"decisions below 0.75 only"| s3c
    s3c --> s4
    s4 --> s5
    s5 --> out1
    s5 --> out2
    s5 --> out3
```

| Stage | Sees | Produces | Model |
| --- | --- | --- | --- |
| 0 Normalize | Raw schema text | Flat `SourceField` and `DestField` lists, dot paths | — |
| 1 Route | One table's **column names**, all collection names | Table to collection pairing + confidence | Cheap |
| 2 Shortlist | One field, one collection's paths | Top 6 candidates with score components | — |
| 3 Adjudicate | 8 fields, each with its own 6 candidates | Chosen path, transform, confidence, reasoning, notes | Cheap, escalating |
| 3c Reflect | One weak decision and its shortlist | Revised decision or the same one confirmed | Strong |
| 4 Validate | Everything | Repairs, tie-breaks, forced unmapped, diagnostics | — |
| 5 Assemble | Decisions | The three artifacts | — |

Stage 1 seeing **names without types** is deliberate. Routing is a three-way choice that
needs vocabulary, not data types, and withholding types keeps the prompt small enough that
the constraint proof stays comfortably clear of any threshold.

## A run, end to end

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as FastAPI
    participant W as Pipeline thread
    participant R as Retrieval
    participant M as Bedrock

    B->>A: POST /api/run
    A->>W: start worker, open SSE stream
    W-->>B: hello, parsed schemas and models
    W->>W: Stage 0 normalize
    loop each source table
        W->>M: route prompt, column names only
        M-->>W: collection + confidence
        W-->>B: route event
    end
    W->>R: Stage 2 shortlist every field
    R-->>W: top 6 candidates each
    loop each batch of 8 fields
        W->>M: adjudicate prompt, fields + own candidates
        M-->>W: decisions as constrained JSON
        W-->>B: mapping events, wires animate
        alt confidence below escalate threshold
            W->>M: same batch, strong model
            M-->>W: revised decisions
            W-->>B: escalate event
        end
    end
    loop each decision below reflect threshold
        W->>M: critic prompt
        M-->>W: confirm or revise
        W-->>B: reflect event
    end
    W->>W: Stage 4 validate and repair
    W->>W: Stage 5 assemble
    W-->>B: result, mapping + report + decisions
    W-->>B: run_end
```

The pipeline is synchronous, so it runs on a worker thread and publishes events through a
queue while the request handler forwards them. That keeps the pipeline free of any web
concerns — the CLI runs the identical code with an event callback that prints instead.

## How one field is decided

```mermaid
stateDiagram-v2
    [*] --> Shortlisted
    Shortlisted --> Unmapped: nothing clears the score floor
    Shortlisted --> CheapPass: candidates offered

    CheapPass --> Escalated: confidence below 0.80
    CheapPass --> Blended: confidence at or above 0.80
    Escalated --> Blended

    Blended --> Reflected: blended below 0.75
    Reflected --> Blended: revised or confirmed
    Blended --> Validated

    Validated --> Repaired: path not in schema
    Validated --> TieBroken: path already claimed
    Repaired --> Mapped
    TieBroken --> Mapped
    Validated --> Mapped: path is in schema and free
    Validated --> Unmapped: model declined, no honest match

    Mapped --> [*]
    Unmapped --> [*]
```

Every transition is recorded on the `Decision`, which is what the **Decision** tab reads. A
field that was escalated and then revised by reflection shows all three passes with their
individual answers, so a disagreement between models is visible rather than averaged away.

## Confidence

```mermaid
flowchart LR
    mc["Model confidence"] --> blend["blend<br/>0.6 model + 0.4 retrieval"]
    margin["Retrieval margin<br/>how decisively the winner won"] --> blend
    blend --> pen{"type mismatch?"}
    pen -->|"yes"| sub["subtract penalty"]
    pen -->|"no"| cap
    sub --> cap{"needs manual<br/>value transform?"}
    cap -->|"yes"| clamp["cap at 0.85"]
    cap -->|"no"| band
    clamp --> band["band it"]
    band --> high[">= 0.90 high"]
    band --> med["0.80-0.89 medium"]
    band --> low["under 0.80 review"]
```

A model's self-reported confidence is nearly useless alone — it reports 0.99 for almost
everything, including the wrong answers. Blending it with the retrieval margin fixes the
common failure: a field whose top two candidates scored 0.44 and 0.43 is genuinely ambiguous
no matter how sure the model sounds, and it lands in the review band where a human will look.

## Pre-run checks, in cost order

```mermaid
flowchart TD
    paste["Input changes"] --> parse["Parse each side<br/>free"]
    parse --> ok{"both parse?"}
    ok -->|"no"| block["Show the error<br/>disable Run"]
    ok -->|"yes"| pair["Assess the pairing<br/>free"]
    pair --> verdict{"verdict"}
    verdict -->|"aligned"| ready["Ready"]
    verdict -->|"weak or unrelated"| warn["Warn, name the forced tables<br/>never block"]
    warn --> ready
    ready --> replay{"offline + edited?"}
    replay -->|"yes"| nocass["Warn: no recording exists"]
    replay -->|"no"| run["Run pipeline"]
```

Everything that can be checked for free is checked before anything is spent. The ordering is
the point: a format error, a mismatched pair, and a missing recording are all knowable without
a single model call, and each one used to be discovered only *after* paying for a run.

## Cost control

```mermaid
flowchart LR
    field["Field to decide"] --> cheap["Cheap model<br/>Haiku"]
    cheap --> conf{"confident?"}
    conf -->|"yes"| done["Keep"]
    conf -->|"no"| strong["Strong model<br/>Sonnet"]
    strong --> done
    done --> ledger["Cost ledger<br/>tokens x price"]
    ledger --> budget{"over budget?"}
    budget -->|"yes"| stop["BudgetExceeded<br/>stop the run"]
    budget -->|"no"| next["Next field"]
```

Four mechanisms, in order of how much they save: the cascade (only weak fields reach the
strong model), response caching within a run, batching eight fields per call, and routing on
names alone. A full live run of the assignment schemas costs about **$0.04**; the library test
pair cost **$0.065** cold and **$0.02** when re-run with cache hits.

## Development workflow

```mermaid
flowchart LR
    edit["Edit code"] --> tests["dev.sh test<br/>281 tests, offline"]
    tests --> offline["dev.sh offline<br/>replay a full run"]
    offline --> checks["check_ui, check_docs<br/>smoke_input, smoke_api"]
    checks --> live{"prompts changed?"}
    live -->|"yes"| record["dev.sh record<br/>re-record cassettes"]
    live -->|"no"| commit["Commit"]
    record --> commit
```

The loop that matters: **change a prompt and the cassettes go stale**, because replay is keyed
by request hash. An offline run then fails with `CassetteMissing`, which is the intended
signal to re-record against live Bedrock rather than a bug.

## Evaluation

Each of these fails loudly rather than printing a number to interpret:

| Command | Asserts |
| --- | --- |
| `bash scripts/dev.sh test` | Full suite, no AWS needed |
| `python scripts/eval_retrieval.py` | Stage 2 shortlist contains the oracle's answer |
| `python scripts/eval_pairing.py` | Every true schema pair outscores every crossed pair |
| `bash scripts/smoke_input.sh` | Samples parse, bad input is refused, mismatches are flagged |
| `bash scripts/smoke_api.sh` | Every endpoint plus a full SSE run |
| `bash scripts/check_docs.sh` | Documented examples actually run; links resolve |
| `bash scripts/check_ui.sh` | UI components and endpoints are wired |
| `python scripts/show_mapping.py <artifact>` | Reads a mapping as a table for eyeballing |
