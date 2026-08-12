#!/usr/bin/env bash
# Verify a deployed (or locally containerized) instance.
#
# The interesting check is the last one. Everything else would pass just as well
# in BUFFERED invoke mode, where Lambda holds the whole Server-Sent Event stream
# and delivers it at the end - the app looks fine and the live graph sits empty
# for the length of the run. So this measures *when* each event arrives, not just
# that they all arrive.
#
# Usage:
#   bash scripts/smoke_deployed.sh https://xxxx.lambda-url.us-east-1.on.aws/
#   bash scripts/smoke_deployed.sh http://127.0.0.1:8081   # the same image locally
set -uo pipefail

BASE="${1:-}"
[ -z "$BASE" ] && { echo "usage: bash scripts/smoke_deployed.sh <base-url>" >&2; exit 2; }
BASE="${BASE%/}"
cd "$(dirname "$0")/.."

python3 - "$BASE" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
# No PEP 585 annotations anywhere below: this runs under the system python3,
# which on Ubuntu 20.04 is 3.8 and cannot subscript builtin types at runtime.
problems = []


def call(path: str, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"content-type": "application/json"} if data else {}
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
        try:
            return response.status, json.loads(body)
        except json.JSONDecodeError:
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' -> ' + str(detail)) if detail else ''}")
    if not ok:
        problems.append(label)


print("=== reachable and healthy")
status, health = call("/api/health")
check("GET /api/health", status == 200, status)
if isinstance(health, dict):
    where = "lambda" if health.get("lambda") else "container or local"
    print(f"       running as: {where}, region {health.get('region')}")
    check(
        "offline replay is available (cassettes shipped in the image)",
        bool(health.get("offline_available")),
        f"{health.get('cassette_count')} cassettes",
    )
    if health.get("auth_required"):
        print("       note: APP_ACCESS_TOKEN is set, so the browser UI cannot start runs")

print()
print("=== the page opens on something real")
status, page = call("/")
check("GET / serves the interface", status == 200 and "<div id=\"app\"" in str(page), status)
status, artifact = call("/api/latest_artifact")
served = isinstance(artifact, dict) and artifact.get("mapping")
check(
    "GET /api/latest_artifact returns the committed mapping",
    bool(served),
    artifact.get("source") if isinstance(artifact, dict) else artifact,
)
if served:
    tables = artifact["mapping"].get("tables") or []
    check("the artifact has tables to draw", bool(tables), f"{len(tables)} tables")

print()
print("=== free endpoints work without spending anything")
status, parsed = call(
    "/api/parse",
    {"source_text": "CREATE TABLE t (id INT PRIMARY KEY, nm VARCHAR(50));", "destination_text": ""},
)
ok = status == 200 and isinstance(parsed, dict) and parsed.get("source", {}).get("ok")
check("POST /api/parse validates a schema", ok, status)
status, samples = call("/api/samples/library_legacy.ddl.sql")
check("GET /api/samples/<name> serves a bundled sample", status == 200, status)

print()
print("=== a full run, and whether its progress actually streams")
payload = {"offline": True}
request = urllib.request.Request(
    base + "/api/run",
    data=json.dumps(payload).encode(),
    headers={"content-type": "application/json", "accept": "text/event-stream"},
)
timeline = []
start = time.monotonic()
final_ok = None
try:
    with urllib.request.urlopen(request, timeout=300) as response:
        check("POST /api/run accepted", response.status == 200, response.status)
        for raw in response:  # iterating the response yields lines as they arrive
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("event:"):
                timeline.append((time.monotonic() - start, line.split(":", 1)[1].strip()))
            elif line.startswith("data:") and '"ok"' in line:
                try:
                    final_ok = json.loads(line.split(":", 1)[1].strip()).get("ok")
                except json.JSONDecodeError:
                    pass
except Exception as exc:  # noqa: BLE001
    check("POST /api/run streamed to completion", False, f"{type(exc).__name__}: {exc}")

total = time.monotonic() - start
kinds = [name for _, name in timeline]
check("the run emitted events", len(timeline) > 3, f"{len(timeline)} events in {total:.1f}s")
check("the run ended", "run_end" in kinds or "result" in kinds, kinds[-1] if kinds else "none")

if len(timeline) > 3:
    first, last = timeline[0][0], timeline[-1][0]
    spread = last - first
    # Buffered mode delivers every event in one burst, so all arrival times
    # collapse to roughly the same instant. Streaming spreads them out.
    streaming = spread > 0.15 and len({round(t, 1) for t, _ in timeline}) > 2
    print(f"       first event at {first:.2f}s, last at {last:.2f}s, spread {spread:.2f}s")
    check(
        "progress arrives incrementally (InvokeMode is RESPONSE_STREAM)",
        streaming,
        "buffered: every event landed at once" if not streaming else f"{spread:.2f}s spread",
    )

print()
if problems:
    print("PROBLEMS:")
    for problem in problems:
        print(" -", problem)
    sys.exit(1)
print("DEPLOYED INSTANCE PASSED ALL CHECKS")
PY
