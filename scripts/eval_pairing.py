#!/usr/bin/env python3
"""Calibrate the pairing check against every bundled pair, matched and crossed.

Thresholds picked by eye are thresholds that fail on the first new schema. This
prints the score for every source x destination combination available, so the gap
between "same domain" and "crossed domain" is visible and the constants in
pairing.py can be justified.

    python scripts/eval_pairing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from schema_mapper.config import DATA_DIR  # noqa: E402
from schema_mapper.knowledge import load_knowledge  # noqa: E402
from schema_mapper.normalize import load_destination, load_source  # noqa: E402
from schema_mapper.pairing import ALIGNED_AT, UNRELATED_BELOW, assess_pair  # noqa: E402

# Which source belongs with which destination.
TRUE_PAIRS = {
    "legacy_hrm": "people_platform",
    # legacy_hrm.ddl.sql declares no database, so it normalizes to "source".
    "source": "people_platform",
    "legacy_library": "library_platform",
    "legacy_sis": "school_platform",
    "legacy_crm": "revenue_platform",
}


def main() -> int:
    knowledge = load_knowledge()
    sources, destinations = {}, {}

    for path in sorted((DATA_DIR / "samples").glob("*")):
        if path.suffix.lower() not in {".json", ".sql"} or path.name.endswith("_rows.json"):
            continue
        text = path.read_text(encoding="utf-8")
        name = path.name.lower()
        try:
            if "mysql" in name or "ddl" in name:
                schema = load_source(text)
                sources[schema.database] = schema
            else:
                schema = load_destination(text)
                destinations[schema.database] = schema
        except Exception as exc:  # noqa: BLE001 - a bad sample should be loud
            print(f"  skip {path.name}: {exc}")

    # The assignment schemas live outside samples/ in their canonical form.
    for path, kind in (
        (DATA_DIR / "schemas" / "legacy_hrm.mysql.json", "source"),
        (DATA_DIR / "schemas" / "people_platform.mongo.json", "destination"),
    ):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if kind == "source":
            schema = load_source(text)
            sources[schema.database] = schema
        else:
            schema = load_destination(text)
            destinations[schema.database] = schema

    print(f"sources: {sorted(sources)}")
    print(f"destinations: {sorted(destinations)}")
    print(f"\nthresholds: aligned >= {ALIGNED_AT}, unrelated < {UNRELATED_BELOW}\n")
    print(f"{'source':<16}{'destination':<20}{'score':>7}  {'verdict':<10}{'expected':<10}")
    print("-" * 72)

    matched, crossed, wrong = [], [], 0
    for src_name, source in sorted(sources.items()):
        for dst_name, destination in sorted(destinations.items()):
            assessment = assess_pair(source, destination, knowledge)
            is_true_pair = TRUE_PAIRS.get(src_name) == dst_name
            expected = "aligned" if is_true_pair else "not aligned"
            correct = assessment.ok if is_true_pair else not assessment.ok
            wrong += 0 if correct else 1
            (matched if is_true_pair else crossed).append(assessment.score)
            flag = " " if correct else "  <-- MISCLASSIFIED"
            print(
                f"{src_name:<16}{dst_name:<20}{assessment.score:>7.3f}  "
                f"{assessment.verdict:<10}{expected:<10}{flag}"
            )
            if "--tables" in sys.argv:
                for pairing in assessment.pairings:
                    print(
                        f"    {pairing.table:<12} -> {pairing.collection:<14}"
                        f"affinity {pairing.affinity:.3f}  "
                        f"placed {pairing.placed_fields}/{pairing.fields}"
                    )

    print("-" * 72)
    if matched and crossed:
        print(f"true pairs    : min {min(matched):.3f}  max {max(matched):.3f}")
        print(f"crossed pairs : min {min(crossed):.3f}  max {max(crossed):.3f}")
        margin = min(matched) - max(crossed)
        print(f"separation    : {margin:+.3f} ({'clean' if margin > 0 else 'OVERLAPPING'})")
    print(f"misclassified : {wrong}")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
