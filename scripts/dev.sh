#!/usr/bin/env bash
# Task runner for Linux / WSL / macOS. One entry point so every command uses the
# project venv and the src/ layout without callers having to remember PYTHONPATH.
#
#   bash scripts/dev.sh check      # dependency + import sanity check
#   bash scripts/dev.sh eval       # Stage 2 shortlist recall against the oracle
#   bash scripts/dev.sh test       # full offline test suite
#   bash scripts/dev.sh bedrock    # verify credentials and model access
#   bash scripts/dev.sh run        # live Bedrock run
#   bash scripts/dev.sh record     # live run, recording cassettes
#   bash scripts/dev.sh offline    # replay cassettes, no AWS needed
#   bash scripts/dev.sh api        # local API + UI on http://localhost:8000
#   bash scripts/dev.sh lint       # compile-check every module
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  if [[ -x ".venv/Scripts/python.exe" ]]; then
    echo "Found a Windows venv but running under Linux/WSL." >&2
    echo "Run: bash scripts/setup_venv.sh" >&2
  else
    echo "No venv found. Run: bash scripts/setup_venv.sh" >&2
  fi
  exit 1
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

cmd="${1:-check}"
shift || true

case "$cmd" in
  check)
    "$PY" -V
    "$PY" - <<'PYEOF'
import importlib
missing = []
for name in ("boto3", "pydantic", "jsonschema", "fastapi", "uvicorn", "dotenv", "pytest"):
    try:
        importlib.import_module(name)
    except ImportError:
        missing.append(name)
if missing:
    raise SystemExit("missing dependencies: " + ", ".join(missing) + "\nRun: bash scripts/setup_venv.sh")
for name in (
    "schema_mapper.config",
    "schema_mapper.normalize",
    "schema_mapper.knowledge",
    "schema_mapper.candidates",
    "schema_mapper.models",
    "schema_mapper.transforms",
    "schema_mapper.cost",
    "schema_mapper.bedrock",
    "schema_mapper.prompts",
    "schema_mapper.tools",
    "schema_mapper.validate",
    "schema_mapper.pipeline",
    "schema_mapper.cli",
):
    importlib.import_module(name)
print("dependencies ok, all modules import")
PYEOF
    ;;
  lint)
    "$PY" -m compileall -q src api scripts >/dev/null
    echo "compile check passed"
    ;;
  eval)
    "$PY" scripts/eval_retrieval.py "$@"
    ;;
  test)
    "$PY" -m pytest -q "$@"
    ;;
  bedrock)
    "$PY" scripts/check_bedrock.py
    ;;
  run)
    "$PY" -m schema_mapper.cli "$@"
    ;;
  record)
    "$PY" -m schema_mapper.cli --record "$@"
    ;;
  offline)
    "$PY" -m schema_mapper.cli --offline "$@"
    ;;
  api)
    "$PY" -m uvicorn api.main:app --reload --port "${PORT:-8000}"
    ;;
  *)
    echo "unknown command: $cmd" >&2
    sed -n '2,20p' "$0" >&2
    exit 2
    ;;
esac
