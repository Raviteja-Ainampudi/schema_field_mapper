#!/usr/bin/env bash
# Executes the examples the documentation promises, so the docs cannot drift from
# the API. Also checks that every relative link in docs/ and README.md resolves.
# Usage: bash scripts/check_docs.sh [base_url]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
cd "$(dirname "$0")/.."

echo "=== relative links resolve"
python3 - <<'PY'
import pathlib
import re
import sys

root = pathlib.Path(".")
broken = []
for doc in [root / "README.md", *sorted((root / "docs").rglob("*.md"))]:
    text = doc.read_text(encoding="utf-8")
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = (doc.parent / target.split("#")[0]).resolve()
        if not path.exists():
            broken.append(f"{doc}: [{label}]({target})")
print(f"  checked {len(list((root / 'docs').rglob('*.md'))) + 1} files")
for item in broken:
    print("  BROKEN", item)
sys.exit(1 if broken else 0)
PY
LINKS=$?

echo
echo "=== documented API examples actually work"
python3 - "$BASE" <<'PY'
import json
import sys
import urllib.error
import urllib.request

base = sys.argv[1]
failures = 0


def call(method, path, payload=None):
    """Returns (status, parsed-json-or-raw-text). /docs serves HTML, not JSON."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:200]
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode(errors="replace")[:200]


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(' -> ' + str(detail)) if detail else ''}")
    if not ok:
        failures += 1


# README + API.md: parse a pasted DDL snippet.
code, body = call("POST", "/api/parse", {
    "source_text": 'CREATE TABLE t (id INT PRIMARY KEY, f_name VARCHAR(60) COMMENT "given name");'
})
check("POST /api/parse (DDL snippet)",
      code == 200 and body["source"]["ok"] and body["source"]["format"] == "mysql_ddl",
      f"{body['source'].get('fields')} fields")

# INPUT_FORMATS.md: the terse MongoDB form from the assignment's Dataset B.
terse = {
    "collections": {
        "employees": {
            "_id": "ObjectId",
            "employeeCode": "String     -- unique human-readable ID",
            "fullName": {"firstName": "String", "lastName": "String"},
            "employment": {
                "status": "String        -- active / inactive / terminated",
                "managerId": "ObjectId   -- ref -> employees._id",
            },
        }
    }
}
code, body = call("POST", "/api/parse", {"destination_text": json.dumps(terse)})
side = body["destination"] if code == 200 else {}
# _id, employeeCode, fullName.firstName, fullName.lastName,
# employment.status, employment.managerId - containers are not leaves.
check("POST /api/parse (terse Dataset B form)",
      code == 200 and side.get("ok") and side.get("fields") == 6,
      f"{side.get('fields')} leaf paths, format {side.get('format')}")

# INPUT_FORMATS.md: the source shorthand form.
shorthand = {"tables": {"emp_master": {
    "emp_id": "INT PRIMARY KEY",
    "office_loc_id": "INT FK -> locations.loc_id",
    "rec_stat": "CHAR(1) NOT NULL  -- A=Active, I=Inactive, T=Terminated",
}}}
code, body = call("POST", "/api/parse", {"source_text": json.dumps(shorthand)})
check("POST /api/parse (source shorthand form)",
      code == 200 and body["source"]["ok"] and body["source"]["fields"] == 3,
      body["source"].get("fields"))

# API.md: bad input is rejected with a message, not a 500.
code, body = call("POST", "/api/parse", {"source_text": "not a schema"})
check("POST /api/parse rejects junk with a message",
      code == 200 and body["source"]["ok"] is False and "error" in body["source"],
      body["source"].get("error"))

# API.md: candidates uses top_k, and requires table/field/collection.
code, body = call("GET", "/api/candidates?table=dept_info&field=dept_stat"
                         "&collection=departments&top_k=4")
check("GET /api/candidates (top_k=4)", code == 200 and len(body["candidates"]) == 4,
      f"{len(body['candidates']) if code == 200 else body} candidates")

# API.md: preview derives the transform from types, needing only the pair.
code, body = call("POST", "/api/preview", {
    "table": "emp_master", "collection": "employees",
    "row": {"rec_stat": "A", "is_remote": 1},
    "mappings": [
        {"source_field": "rec_stat", "destination_field": "employment.status"},
        {"source_field": "is_remote", "destination_field": "employment.isRemote"},
    ],
})
built = body.get("document", {}).get("employment", {}) if code == 200 else {}
check("POST /api/preview applies transforms",
      code == 200 and built.get("status") == "active" and built.get("isRemote") is True,
      built)

# USAGE.md / QUICKSTART.md: the interactive references exist.
for path in ("/docs", "/redoc", "/openapi.json"):
    code, _ = call("GET", path)
    check(f"GET {path}", code == 200, code)

# README.md: the offline run over the API.
code, _ = call("POST", "/api/run", {"offline": True})
check("POST /api/run (offline)", code == 200, code)

sys.exit(1 if failures else 0)
PY
EXAMPLES=$?

echo
if [ "$LINKS" = "0" ] && [ "$EXAMPLES" = "0" ]; then
  echo "DOCS CHECKS PASSED"
  exit 0
fi
echo "DOCS CHECKS FAILED (links=$LINKS examples=$EXAMPLES)"
exit 1
