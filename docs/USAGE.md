# Using the interface

```bash
bash scripts/dev.sh api          # http://localhost:8000
```

The bundled `legacy_hrm` and `people_platform` schemas load automatically and the committed
artifact is displayed, so the page is useful before you run anything. The same content as
this page is available in-app under the **Guide** tab.

## Layout

| Region | What it is for |
| --- | --- |
| Header | Title, byline, what the tool does, run status badges, links to the API docs and Guide |
| Input chip (top right) | What the next run will map — `database → database`, counts, `bundled` or `edited`. Click to open the input panel |
| Model row | Router, mapper, and cheap-pass model per role |
| Toggles | `cascade`, `reflect`, `offline`, and **Run pipeline** |
| Meter strip | Coverage, mean confidence, cost, LLM calls, largest prompt, both-schemas count, unmapped, duration |
| Left column | Source columns of the active table, each with a confidence dot |
| Centre canvas | One wire per decision, coloured by confidence |
| Right column | Destination leaf paths, ordered to follow the source so wires stay near-parallel |
| Table tabs | `emp_master → employees` and the other pairings |
| Bottom drawer | Guide, Input data, Decision, Constraint proof, Coverage & quality, Cost, Timeline, Mapping JSON |

## A first run

1. Tick **offline** to replay recorded exchanges — no AWS account, no spend, and the result
   matches the committed artifact.
2. Press **Run pipeline**. Wires animate in as each batch resolves; the log under
   **Timeline** narrates every stage.
3. Click any source column, destination path, or wire. The **Decision** tab shows that
   field's full provenance.
4. Take the artifact from **Mapping JSON** — copy, or download the exact file.

For a live Bedrock run, untick **offline**. Cost appears in the meter strip as it accrues.

## Reading the graph

Wire and dot colour encode the **blended** confidence, not the model's self-report:

| Band | Meaning | What to do |
| --- | --- | --- |
| Green, ≥ 0.90 | High | Spot-check |
| Amber, 0.80–0.89 | Medium | Read the reasoning |
| Red, < 0.80 | Review | Look at the candidate shortlist yourself |
| Hollow red dot | Unmapped | Check the declared justification |

Confidence is `0.6 × model confidence + 0.4 × normalized retrieval margin`, with a
type-mismatch penalty and a 0.85 cap when a required value transform cannot be expressed as
a rule. A field the model is sure about that only barely beat its runner-up lands in the
review band deliberately — that is exactly where a human should look.

Selecting a wire also labels it with its type transform.

## Giving it your own schemas

Open **Input data** (or click the input chip). Each side accepts four things:

- **Paste** into the editor.
- **Drag and drop** a file onto the box.
- **Upload file** — `.json`, `.sql`, or `.txt`, up to 200,000 characters.
- **Load sample…** — the bundled schemas plus `tiny_crm`, a small unrelated pair for
  checking that nothing is hardcoded to the HR schemas.

The format is detected from the content, so MySQL DDL, MySQL schema JSON, MongoDB schema
JSON, and `mongoexport` sample documents all work. Full details and examples:
[INPUT_FORMATS.md](INPUT_FORMATS.md).

As you type, the line under each editor reports what was understood —
`✓ MySQL CREATE TABLE · legacy_hrm · 3 tables · 34 columns` — or the exact parse error.
**Run pipeline** is disabled while the input does not parse, so a bad paste cannot waste a
paid run. **Reset to bundled schemas** restores the originals.

One limit worth knowing: **your own schemas cannot run offline.** Cassette replay is keyed
by request hash, so a schema pair with no recording needs live credentials.

## The drawer tabs

**Decision** — for the selected field: the chosen path, type transform, confidence,
reasoning, notes, the candidate shortlist it beat with per-component scores, which model
pass decided it (cheap, escalated, or revised by reflection), any knowledge snippets cited,
and the transform executed against a real row.

**Constraint proof** — the assignment forbids passing both schemas in one prompt for a
finished mapping. This restates that rule as live pass/fail checks computed from the run's
own prompt manifests: the largest number of typed source fields in any one prompt, the most
destination paths, the most mappings any single response produced, and the count of prompts
containing both full schemas (which must be zero). Every prompt in the run is listed with
its manifest.

**Coverage & quality** — every source field is either mapped or explicitly declared
unmapped; the same for destination paths. Also the confidence histogram, escalation rate,
and any repairs, tie-breaks, or corrected notes.

**Cost** — per-call token counts and USD by stage and model, cost per mapped field, cache
hits, and what the run would have cost on other models. An offline run reports the
*recorded* cost marked as not billed, rather than a misleading zero.

**Timeline** — stage durations plus the full event log.

**Mapping JSON** — the deliverable, with copy and download.

## Controls that change cost or quality

| Control | Effect |
| --- | --- |
| **Router** | Picks the table→collection pairing. A cheap model is appropriate; this is 3-way matching, not reasoning |
| **Mapper** | The strong model for semantic judgment |
| **Cheap pass** | First-pass model when cascade is on |
| **cascade** | Cheap model first, escalating only low-confidence fields. Roughly a third of the cost with no meaningful accuracy loss |
| **reflect** | A bounded critic pass over the weakest decisions. Touches a handful of fields, costs pennies |
| **offline** | Replay recordings instead of calling Bedrock. Free |

## Testing over the API instead

Everything here is an HTTP endpoint, and Swagger UI at <http://localhost:8000/docs> will
run any of them from the browser. See [API.md](API.md).
