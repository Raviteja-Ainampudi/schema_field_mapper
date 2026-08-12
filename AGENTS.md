# AGENTS.md — Schema Field Mapper

Guidance for coding agents working in this repository.

## Goal

Build an AI pipeline that maps every field from a MySQL source schema (`legacy_hrm`) to a MongoDB destination schema (`people_platform`), emitting one JSON document in the assignment’s expected format.

Human-facing assignment text: `InterviewAssignment.txt` — present locally, deliberately
untracked (it is not ours to publish), so do not expect it in a fresh clone.

## Hard constraints

- **Do not** send both full schemas to an LLM in a single prompt and treat that as the finished mapping.
- Prefer a multi-stage pipeline (e.g. normalize → candidate retrieve/match → LLM refine/score → validate/assemble).
- Cover **every** source field across `emp_master`, `dept_info`, and `locations`.
- Output must match the assignment JSON contract (`mapping_version`, `tables`, `field_mappings`, confidence, reasoning, notes, unmapped lists, etc.).

## Environment

- Use the project venv: `.venv` (Python 3.12). **Not portable** between Windows and WSL/Linux — recreate on each OS via `scripts/setup_venv.ps1` (Windows) or `scripts/setup_venv.sh` (WSL/Linux/macOS).
- Windows activate: `.\.venv\Scripts\Activate.ps1`
- WSL/Linux/macOS activate: `source .venv/bin/activate`
- WSL Ubuntu 20.04 needs Python 3.12 from deadsnakes (`python3.12`, `python3.12-venv`); default `python3` is too old.
- Load secrets from `.env` only; **never** commit `.env` or paste keys into docs/code/logs.
- Keep `.env` scoped to **this** project (LLM/provider keys as needed). Do not reuse unrelated app credentials.

## Repo conventions (as code appears)

- Small, testable modules per pipeline stage.
- Keep prompts in dedicated files (e.g. `prompts/`), not buried as giant inline strings.
- Schemas/fixtures under something like `data/`; generated mapping JSON under `output/` (or equivalent).
- Prefer deterministic validation of the output shape before claiming success.

## Working with MEMORY.md

- `MEMORY.md` is the living scratchpad for durable decisions, experiments, and open issues.
- When you learn something lasting (model choice, chunking strategy, prompt pattern that worked/failed), **append** a dated note there.
- Do **not** store secrets, API keys, or raw `.env` contents in `MEMORY.md`.

## Safety / quality bar

- No silent swallowing of LLM or parse failures; surface errors clearly.
- Prefer measurable confidence and plain-English reasoning per mapping.
- Before calling work “done”: run the pipeline in `.venv`, produce output JSON, and sanity-check coverage (no unexpected empty `unmapped_source_fields` unless justified in notes/write-up).
