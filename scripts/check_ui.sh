#!/usr/bin/env bash
# Best-effort static check of the browser bundle. The system node here is too
# old for ES modules and optional chaining, so this validates what can be
# checked without a JS engine: every htm component referenced is defined, every
# drawer tab has a panel, and every API path the UI calls answers on the server.
# Usage: bash scripts/check_ui.sh [base_url]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
cd "$(dirname "$0")/.."

python3 - "$BASE" <<'PY'
import json
import re
import sys
import urllib.error
import urllib.request

base = sys.argv[1]
text = open("api/static/app.js", encoding="utf-8").read()
problems = []

# Components used as <${Name} must be defined.
defined = set(re.findall(r"function\s+([A-Z]\w+)", text))
used = set(re.findall(r"<\$\{(\w+)\}", text))
for name in sorted(used - defined):
    problems.append(f"component used but not defined: {name}")

# Tab keys rendered must have a matching panel branch.
tab_keys = set(re.findall(r'\["(\w+)",\s*"[^"]+"\]', text))
handled = set(re.findall(r'tab === "(\w+)"', text))
for key in sorted(tab_keys - handled):
    problems.append(f"tab '{key}' has no panel branch")

print(f"components: {len(used)} used, all defined" if not (used - defined) else "")
print(f"tabs: {sorted(tab_keys)}")

# Every state setter called must come from a useState declaration.
declared = set(re.findall(r"const \[\w+, (set\w+)\]", text))
local = set(re.findall(r"const (set\w+) = useCallback", text))
builtin = {"setTimeout", "setInterval"}
for setter in sorted(set(re.findall(r"\b(set[A-Z]\w+)\(", text)) - declared - local - builtin):
    problems.append(f"setter called but never declared: {setter}")

# Every fetched path must answer.
paths = sorted(set(re.findall(r'(?:getJSON|fetch)\(\s*"(/api/[^"?]+)', text)))
posts = {"/api/run", "/api/parse", "/api/preview"}
print("\nendpoint reachability:")
for path in paths:
    if path in posts:
        body = json.dumps({} if path != "/api/run" else {"offline": True}).encode()
        request = urllib.request.Request(
            base + path, data=body, headers={"content-type": "application/json"}
        )
    else:
        request = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        code = str(exc)
    ok = code == 200 or (path == "/api/preview" and code == 422)
    print(f"  {'ok ' if ok else 'FAIL'} {path} -> {code}")
    if not ok:
        problems.append(f"{path} returned {code}")

if problems:
    print("\nPROBLEMS:")
    for problem in problems:
        print(" -", problem)
    sys.exit(1)
print("\nUI STATIC CHECKS PASSED")
PY
