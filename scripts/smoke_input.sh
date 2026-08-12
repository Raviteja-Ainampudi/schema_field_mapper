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

echo
echo "=== a mismatched pair is flagged before a run is spent"
# The reported case: legacy_library source against the bundled people_platform
# destination. Both halves parse, both are valid, and the run succeeds - it just
# maps books onto departments. Only a pairing check can catch this.
python3 - "$BASE" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1]
failures = 0


def parse(payload):
    request = urllib.request.Request(
        f"{base}/api/parse",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' -> ' + str(detail)) if detail else ''}")
    if not ok:
        failures += 1


with urllib.request.urlopen(f"{base}/api/samples/library_legacy.ddl.sql") as response:
    library = json.load(response)["text"]

# Crossed: library source, bundled HR destination.
crossed = parse({"source_text": library})
pairing = crossed.get("pairing") or {}
check(
    "crossed pair is flagged, while both sides still parse",
    crossed["ok"] and pairing.get("verdict") != "aligned",
    f"{pairing.get('verdict')} at {pairing.get('score')}",
)
check(
    "the homeless table is named",
    "bk_master" in (pairing.get("detail") or ""),
    (pairing.get("detail") or "")[:80],
)

# Matched: the bundled pair must not warn, or the warning is noise.
aligned = parse({}).get("pairing") or {}
check(
    "bundled pair is reported aligned",
    aligned.get("verdict") == "aligned",
    f"{aligned.get('verdict')} at {aligned.get('score')}",
)
sys.exit(1 if failures else 0)
PY
PAIRING=$?

echo
echo "=== a new schema cannot run offline, and says so plainly"
# The single most likely surprise for someone testing their own file: replay is
# keyed by request hash, so an unrecorded schema has no cassette to replay. That
# must arrive as a named error event, not a hang or a half-written artifact.
python3 - "$BASE" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1]
payload = json.dumps(
    {
        "offline": True,
        "source_text": "CREATE TABLE t (id INT PRIMARY KEY, f_name VARCHAR(60));",
    }
).encode()
request = urllib.request.Request(
    f"{base}/api/run", data=payload, headers={"content-type": "application/json"}
)
kinds, message = [], ""
with urllib.request.urlopen(request, timeout=120) as response:
    event = ""
    for raw in response:
        line = raw.decode().rstrip("\n")
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event:
            body = json.loads(line.split(":", 1)[1].strip())
            kinds.append(event)
            if event == "error":
                message = f"{body.get('kind')}: {body.get('message', '')[:90]}"

if "error" in kinds and "CassetteMissing" in message:
    print(f"  ok   run refused with a named error -> {message}")
    sys.exit(0)
print(f"  FAIL expected a CassetteMissing error event, got {kinds[-3:]} {message}")
sys.exit(1)
PY
OFFLINE_NEW=$?

if [ "$GOOD" = "0" ] && [ "$OFFLINE_NEW" = "0" ] && [ "$PAIRING" = "0" ]; then
  exit 0
fi
exit 1
