#!/usr/bin/env python3
"""Print a mapping document as a readable table.

Reviewing a 300-line JSON artifact in a terminal is miserable; this is for
eyeballing a run's decisions, sorted so the ones needing attention come last.

    python scripts/show_mapping.py outputs/mapping_legacy_hrm_to_people_platform.json
    python scripts/show_mapping.py <path> --review-only
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

BANDS = ((0.90, "high"), (0.80, "med "), (0.0, "REVIEW"))


def band(confidence: float) -> str:
    for floor, label in BANDS:
        if confidence >= floor:
            return label
    return "?"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--review-only", action="store_true", help="Only show fields below the medium threshold."
    )
    parser.add_argument("--notes", action="store_true", help="Include notes and reasoning.")
    args = parser.parse_args()

    document = json.loads(args.path.read_text(encoding="utf-8"))
    print(f"v{document['mapping_version']}  {document['source']} -> "
          f"{document['destination']}  ({document['generated_at']})")

    for table in document["tables"]:
        print(
            f"\n=== {table['source_table']} -> {table['destination_collection']} "
            f"({table['confidence']:.2f})"
        )
        rows = table["field_mappings"]
        if args.review_only:
            rows = [m for m in rows if (m["confidence"] or 0) < 0.80]
        for mapping in sorted(rows, key=lambda m: m["confidence"] or 0, reverse=True):
            confidence = mapping["confidence"] or 0.0
            print(
                f"  {band(confidence)} {confidence:.2f}  {mapping['source_field']:<13}"
                f"-> {mapping['destination_field'] or '(none)':<33}"
                f"{(mapping['type_transform'] or '')[:46]}"
            )
            if args.notes:
                print(f"          why : {mapping['reasoning']}")
                if mapping["notes"]:
                    print(f"          note: {mapping['notes']}")
        if table["unmapped_source_fields"]:
            print(f"  unmapped source: {', '.join(table['unmapped_source_fields'])}")
        if table["unmapped_destination_fields"]:
            print(f"  untargeted dest: {', '.join(table['unmapped_destination_fields'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
