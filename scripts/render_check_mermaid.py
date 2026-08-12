#!/usr/bin/env python3
"""Validate every Mermaid diagram in docs/ against a real renderer.

scripts/check_docs.sh catches structural mistakes offline, but only a renderer
proves a diagram draws. This posts each block to mermaid.ink, which is exactly
what GitHub-style rendering will do to it, and reports the first error line.

Needs network. Not part of check_docs.sh for that reason - run it after editing
diagrams.

    python scripts/render_check_mermaid.py
"""

from __future__ import annotations

import base64
import pathlib
import re
import sys
import urllib.error
import urllib.request

FENCE = re.compile(r"^```mermaid\s*$")


def blocks() -> list[tuple[str, int, str]]:
    found = []
    roots = sorted(pathlib.Path("docs").rglob("*.md")) + [pathlib.Path("README.md")]
    for doc in roots:
        if not doc.is_file():
            continue
        lines = doc.read_text(encoding="utf-8").splitlines()
        inside, body, start = False, [], 0
        for number, line in enumerate(lines, start=1):
            if not inside and FENCE.match(line.strip()):
                inside, body, start = True, [], number
            elif inside and line.strip() == "```":
                found.append((str(doc), start, "\n".join(body)))
                inside = False
            elif inside:
                body.append(line)
    return found


def render(graph: str) -> tuple[bool, str]:
    encoded = base64.urlsafe_b64encode(graph.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/svg/{encoded}"
    try:
        with urllib.request.urlopen(url, timeout=45) as response:
            payload = response.read()
        if b"<svg" not in payload[:2000]:
            return False, "response was not an SVG"
        if b"Syntax error" in payload or b"error-icon" in payload:
            return False, "renderer drew a syntax-error diagram"
        return True, f"{len(payload)} bytes"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        return False, f"HTTP {exc.code}: {detail[:160]}"
    except Exception as exc:  # noqa: BLE001 - network problems must be visible
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    failures = 0
    for doc, line, graph in blocks():
        kind = graph.strip().splitlines()[0][:28] if graph.strip() else "(empty)"
        ok, detail = render(graph)
        print(f"  {'ok  ' if ok else 'FAIL'} {doc}:{line:<4} {kind:<30}{detail}")
        failures += 0 if ok else 1
    print(f"\n{'ALL DIAGRAMS RENDER' if not failures else f'{failures} FAILED TO RENDER'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
