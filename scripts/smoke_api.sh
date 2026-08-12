#!/usr/bin/env bash
# Smoke-tests a running API instance end to end, including a full offline
# pipeline run over the SSE stream. Usage: bash scripts/smoke_api.sh [base_url]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
FAILED=0

hr() { printf '\n=== %s\n' "$1"; }

# code path label -> prints status and byte count, flags anything non-200
probe() {
  local path="$1"
  local out
  out=$(curl -sS -o /tmp/probe.out -w '%{http_code} %{size_download}' "$BASE$path" 2>/tmp/probe.err)
  local code="${out%% *}"
  printf '%-34s %s bytes\n' "$path" "$out"
  if [ "$code" != "200" ]; then
    FAILED=1
    head -c 400 /tmp/probe.out
    head -c 200 /tmp/probe.err
    printf '\n'
  fi
}

hr "static + metadata"
probe /
probe /static/app.css
probe /static/app.js
probe /api/health
probe /api/models
probe /api/schemas
probe /api/contract
probe /api/samples/mysql_information_schema.json
probe /api/samples/legacy_hrm.ddl.sql
probe /api/samples/people_platform.jsonschema.json
probe /api/sample_rows
probe /api/runs

hr "health payload"
curl -sS "$BASE/api/health" | python3 -m json.tool

hr "candidates for dept_stat (the hardest field)"
curl -sS "$BASE/api/candidates?field=dept_info.dept_stat&k=4" | python3 -m json.tool

hr "transform preview"
curl -sS -X POST "$BASE/api/preview" \
  -H 'content-type: application/json' \
  -d '{"source_field":"emp_master.rec_stat","destination_field":"employees.employmentStatus","value":"A"}' \
  | python3 -m json.tool

hr "offline run over SSE"
curl -sS -N -X POST "$BASE/api/run" \
  -H 'content-type: application/json' \
  -d '{"offline":true}' > /tmp/run.sse
printf 'stream bytes: %s\n' "$(wc -c < /tmp/run.sse)"
printf 'event types seen:\n'
grep '^event:' /tmp/run.sse | sort | uniq -c
printf 'stage events:\n'
grep '^data:' /tmp/run.sse | python3 - <<'PY'
import json, sys
final = None
for line in sys.stdin:
    payload = json.loads(line[5:])
    kind = payload.get("type")
    if kind == "stage":
        print(f"  {payload['stage']:<12} {payload.get('detail','')}")
    elif kind == "error":
        print("  ERROR:", payload)
    elif kind == "result":
        final = payload
if final is None:
    print("  NO RESULT EVENT")
    sys.exit(1)
doc = final["document"]
rep = final["report"]
pairs = sum(len(t["field_mappings"]) for t in doc["tables"])
print(f"\nmapping_version={doc['mapping_version']} tables={len(doc['tables'])} pairs={pairs}")
print(f"unmapped_source={len(doc['unmapped_source_fields'])} unmapped_dest={len(doc['unmapped_destination_fields'])}")
print(f"coverage_ok={rep['validation']['ok']} constraint={rep['constraint']['holds']}")
print(f"cost_usd={rep['cost']['total_usd']} billed={rep['cost']['billed']} calls={rep['cost']['calls']}")
print(f"run_id={final['run_id']}")
PY

hr "history after run"
curl -sS "$BASE/api/runs" | python3 -m json.tool | head -30

hr "result"
if [ "$FAILED" = "0" ]; then
  echo "ALL PROBES 200"
else
  echo "SOME PROBES FAILED"
fi
exit "$FAILED"
