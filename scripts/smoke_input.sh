#!/usr/bin/env bash
# Checks the UI's input path: sample listing, sample fetch, and the free
# validation endpoint for good and bad payloads.
# Usage: bash scripts/smoke_input.sh [base_url]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"

echo "=== samples offered to the UI"
curl -sS "$BASE/api/schemas" | python3 -c 'import json,sys; print(json.load(sys.stdin)["samples"])'

echo
echo "=== every offered sample is fetchable and parses"
curl -sS "$BASE/api/schemas" > /tmp/schemas.json
python3 - "$BASE" <<'PY'
import json, sys, urllib.request

base = sys.argv[1]
samples = json.load(open("/tmp/schemas.json"))["samples"]
failures = 0
for sample in samples:
    name = sample["name"]
    with urllib.request.urlopen(f"{base}/api/samples/{name}") as response:
        text = json.load(response)["text"]
    key = "source_text" if sample["kind"] == "source" else "destination_text"
    payload = json.dumps({key: text}).encode()
    request = urllib.request.Request(
        f"{base}/api/parse", data=payload, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        result = json.load(response)
    side = result[sample["kind"]]
    state = "ok " if side["ok"] else "FAIL"
    detail = (
        f"{side.get('format')} {side.get('database')} "
        f"{len(side.get('containers') or {})} containers, {side.get('fields')} fields"
        if side["ok"]
        else side.get("error")
    )
    print(f"  {state} {name:<38} {detail}")
    failures += 0 if side["ok"] else 1
sys.exit(1 if failures else 0)
PY
GOOD=$?

echo
echo "=== a bad paste is rejected with a useful message"
curl -sS -X POST "$BASE/api/parse" -H 'content-type: application/json' \
  -d '{"source_text":"this is not a schema"}' | python3 -m json.tool

echo
echo "=== truncated JSON reports line and column"
curl -sS -X POST "$BASE/api/parse" -H 'content-type: application/json' \
  -d '{"destination_text":"{\"collections\": {\"a\": "}' | python3 -m json.tool

echo
echo "=== empty means bundled defaults"
curl -sS -X POST "$BASE/api/parse" -H 'content-type: application/json' \
  -d '{}' | python3 -m json.tool

exit "$GOOD"
