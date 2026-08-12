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
probe /api/samples/legacy_hrm.ddl.sql
probe /api/samples/tiny_crm.mysql.json
probe /api/samples/people_platform.sample_docs.json
probe /api/samples/tiny_crm.mongo.json
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
python3 - <<'PY'
import json, sys

# The event name, not a field in the payload, carries the type.
final = None
event = None
for line in open("/tmp/run.sse", encoding="utf-8"):
    line = line.rstrip("\n")
    if line.startswith("event:"):
        event = line[6:].strip()
    elif line.startswith("data:"):
        payload = json.loads(line[5:])
        if event == "stage_end":
            print(f"  stage {payload['stage']:<3} {payload.get('label',''):<28} {payload['duration_ms']}ms")
        elif event == "error":
            print("  ERROR:", payload)
        elif event == "result":
            final = payload
if final is None:
    print("  NO RESULT EVENT")
    sys.exit(1)
doc = final["mapping"]
rep = final["report"]
pairs = sum(len(t["field_mappings"]) for t in doc["tables"])
# The unmapped lists are per table, matching the assignment's nesting.
unmapped_src = sum(len(t["unmapped_source_fields"]) for t in doc["tables"])
unmapped_dst = sum(len(t["unmapped_destination_fields"]) for t in doc["tables"])
print(f"\nmapping_version={doc['mapping_version']} tables={len(doc['tables'])} pairs={pairs}")
print(f"unmapped_source={unmapped_src} unmapped_dest={unmapped_dst}")
print(f"diagnostics_ok={rep['diagnostics']['ok']} both_schemas_in_a_prompt="
      f"{rep['constraint']['prompts_containing_both_full_schemas']}")
print(f"cost_usd={rep['cost']['total_usd']} billed={rep['cost']['billed']} "
      f"calls={len(rep['cost']['calls'])}")
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
