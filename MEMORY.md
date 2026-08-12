# MEMORY.md — durable agent notes

Append-only working memory for this project. Agents: add dated bullets; do not delete history unless correcting a factual error. Never store secrets here.

## Project facts

- Interview challenge: map MySQL `legacy_hrm` → MongoDB `people_platform` field-by-field.
- Critical constraint: cannot dump both full schemas into one LLM call for the complete mapping.
- Assignment source of truth: `InterviewAssignment.txt`.
- Local venv: `.venv` (Python 3.12). Platform-specific — run `scripts/setup_venv.ps1` (Windows) or `scripts/setup_venv.sh` (WSL/Linux) when switching OS. WSL Ubuntu 20.04 needs deadsnakes `python3.12`.

## Decisions

- 2026-08-11: Agent docs approach = `AGENTS.md` (stable rules) + `MEMORY.md` (living notes) + human `README.md`. No `.cursor/rules/` yet.
- 2026-08-11: Dual local environment. Windows venv for the day-to-day loop; WSL2 Ubuntu for Linux-parity checks (container build, Lambda RIE, case-sensitive filesystem) before deploying. A `.venv` is not portable between the two, so `scripts/setup_venv.ps1` and `scripts/setup_venv.sh` each replace a foreign venv rather than half-installing into it.
- 2026-08-11: Lambda constraints are centralized in `src/schema_mapper/config.py`, not scattered. Read-only assets resolve relative to the package (`ASSET_ROOT`), every write goes through `writable_dir()` which returns `/tmp/schema_mapper` when `AWS_LAMBDA_FUNCTION_NAME` is set. `.gitattributes` pins `eol=lf`.
- 2026-08-11: Confidence is a blend, not the model's self-report: `0.6 * model_confidence + 0.4 * normalized_retrieval_margin`, type-mismatch penalty, and a 0.85 cap when a required value transform is not mechanically expressible. Table confidence is the coverage-scaled mean of its fields. Pinned in `config.Thresholds`.
- 2026-08-11: Destination "fields" means leaf paths only (40 for this schema); container objects like `fullName` are never mapping targets. `type_transform` uses the ASCII `->` from the assignment's JSON example.
- 2026-08-11: Four accepted input formats so the same pipeline handles a pasted assignment snippet or a real dump: MySQL JSON, MySQL DDL, MongoDB JSON, MongoDB sample documents (Extended JSON). `data/samples/legacy_hrm.ddl.sql` exists as a parser fixture, not a migration script; no MySQL server is ever run.

## Experiments

- 2026-08-11: Bedrock access verified in `us-east-1` with `scripts/check_bedrock.py` (one-token Converse probe per model, since a model can be listed and still return `AccessDeniedException`). Usable: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-haiku-4-5-20251001-v1:0`, `us.amazon.nova-lite-v1:0`, `us.amazon.nova-micro-v1:0`, `us.amazon.nova-pro-v1:0`. `list_foundation_models` returns 0 items for this account but 63 inference profiles are visible, so model IDs must use the `us.` inference-profile prefix.
- 2026-08-11: `us.anthropic.claude-sonnet-4-6-v1:0` does not exist (`ValidationException`). Claude 3.5/3.7 IDs return `ResourceNotFoundException` in this account. `.env` corrected to the Sonnet 4.5 ID.
- 2026-08-11: Embeddings left off by default (`ENABLE_EMBEDDINGS=false`). The deterministic scorer must reach 100% shortlist recall unaided; Titan is an optional accuracy assist, not a dependency.

## Open issues

- Titan embeddings not yet access-verified; only matters if embeddings are switched on.

## Mapping / prompt notes

_None yet._
