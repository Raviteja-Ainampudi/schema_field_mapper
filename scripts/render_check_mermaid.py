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
    # The service sits behind Cloudflare, which answers the default urllib agent
    # with 403 error 1010 - a block, not a syntax verdict.
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "image/svg+xml,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read()
        if b"<svg" not in payload[:2000]:
            return False, f"response was not an SVG: {payload[:120]!r}"
        # Don't grep the SVG for error strings: mermaid emits a stylesheet
        # containing `.error-text` in *every* diagram, valid ones included, which
        # made a content check flag everything. A syntax error is an HTTP 400
        # carrying the parse message, handled below.
        return True, f"{len(payload)} bytes"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        return False, f"HTTP {exc.code}: {detail[:160]}"
    except Exception as exc:  # noqa: BLE001 - network problems must be visible
        return False, f"{type(exc).__name__}: {exc}"


def selftest() -> int:
    """Prove the checker can tell a good diagram from a broken one.

    Without this, a detector that flags everything looks identical to a docs set
    that is entirely broken - which is exactly the confusion this ran into.
    """
    cases = [
        ("valid flowchart", "flowchart LR\n  a --> b\n", True),
        ("valid sequence", "sequenceDiagram\n  A->>B: hi\n", True),
        ("broken flowchart", "flowchart LR\n  a --> [oops\n", False),
        ("bogus type", "notADiagram LR\n  a --> b\n", False),
    ]
    failures = 0
    for label, graph, should_render in cases:
        ok, detail = render(graph)
        passed = ok is should_render
        failures += 0 if passed else 1
        print(
            f"  {'ok  ' if passed else 'FAIL'} {label:<20}"
            f"expected {'render' if should_render else 'error':<6} got "
            f"{'render' if ok else 'error':<6} {detail[:70]}"
        )
    print(f"\n{'SELFTEST PASSED' if not failures else 'SELFTEST FAILED'}")
    return 1 if failures else 0


def save_png(index: int, path: pathlib.Path) -> int:
    """Write one diagram as a PNG, for eyeballing a layout before committing it."""
    found = blocks()
    if not 0 <= index < len(found):
        print(f"no diagram {index}; there are {len(found)}")
        return 1
    doc, line, graph = found[index]
    encoded = base64.urlsafe_b64encode(graph.encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"https://mermaid.ink/img/{encoded}?type=png&bgColor=0d141c",
        headers={"User-Agent": "Mozilla/5.0 Chrome/124.0"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        path.write_bytes(response.read())
    print(f"wrote {path} from {doc}:{line} ({path.stat().st_size} bytes)")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--png" in sys.argv:
        at = sys.argv.index("--png")
        return save_png(int(sys.argv[at + 1]), pathlib.Path(sys.argv[at + 2]))
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
