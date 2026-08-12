# Documentation

Schema Field Mapper maps every field of a legacy MySQL schema (`legacy_hrm`) to its
semantic equivalent in a MongoDB schema (`people_platform`), emitting one reviewable
JSON document.

Developed by Raviteja Ainampudi.

## Start here

| If you want to | Read |
| --- | --- |
| Run it in under two minutes, no AWS account | [QUICKSTART.md](QUICKSTART.md) |
| Drive the web interface | [USAGE.md](USAGE.md) |
| Know what files and formats it accepts | [INPUT_FORMATS.md](INPUT_FORMATS.md) |
| Call the pipeline over HTTP or from the CLI | [API.md](API.md) |
| See how the system is put together | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Follow the workflow stage by stage | [PIPELINE.md](PIPELINE.md) |
| Put it on AWS behind a shareable URL | [DEPLOY.md](DEPLOY.md) |
| Understand the design and the assignment constraint | [../project_plans/schema_field_mapper_plan.md](../project_plans/schema_field_mapper_plan.md) |
| See the UI design rationale | [superpowers/specs/2026-08-11-schema-mapper-ui-design.md](superpowers/specs/2026-08-11-schema-mapper-ui-design.md) |

Diagrams are Mermaid and live in [ARCHITECTURE.md](ARCHITECTURE.md) (system context,
components, data model, deployment, modes) and [PIPELINE.md](PIPELINE.md) (six stages, a run
sequence, per-field decision states, confidence, pre-run checks, cost control, dev loop).
`scripts/check_docs.sh` lints them offline and `scripts/render_check_mermaid.py` renders each
one to prove it draws.

The interface also carries its own guide: the **Guide** tab in the bottom drawer covers
the same ground as `USAGE.md` and `INPUT_FORMATS.md` without leaving the page.

## What is in the box

- `src/schema_mapper/` — the pipeline: normalize, route, shortlist, adjudicate, validate, assemble.
- `api/` — FastAPI app serving the JSON API and the single-page interface.
- `outputs/` — the committed deliverable: `mapping_legacy_hrm_to_people_platform.json`, plus the run report and prompt trace.
- `tests/` — 245 tests, including the semantic oracle and the machine-checked constraint assertions.
- `data/samples/` — extra input files in every accepted format, for testing.

## Not yet written

- `DEPLOY.md` — arrives with `infra/` (Dockerfile and SAM template) in the deploy step. The
  target and cost model are already specified in section 6 of the plan.
- `WRITEUP.md` — the assignment's write-up deliverable.
