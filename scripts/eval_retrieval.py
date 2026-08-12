"""Measure Stage 2 shortlist quality against the expected-mapping oracle.

Recall@K is the number that matters: it is the ceiling on final accuracy, since
the adjudicating model can only choose from what retrieval offered. Rank@1 is
informative but not required - deciding between close candidates is exactly the
job Stage 3 exists to do.

Runs offline, costs nothing, and finishes in under a second, so it is the first
thing to check when mapping quality drops.

Usage:  python scripts/eval_retrieval.py [--top-k 6] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from schema_mapper.candidates import shortlist_field  # noqa: E402
from schema_mapper.config import (  # noqa: E402
    DEFAULT_DESTINATION_SCHEMA,
    DEFAULT_SOURCE_SCHEMA,
    THRESHOLDS,
)
from schema_mapper.knowledge import load_knowledge  # noqa: E402
from schema_mapper.normalize import load_destination_file, load_source_file  # noqa: E402

ORACLE = ROOT / "tests" / "fixtures" / "expected_mapping.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=THRESHOLDS.top_k)
    parser.add_argument("--min-score", type=float, default=THRESHOLDS.min_candidate_score)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    source = load_source_file(os.getenv("SOURCE_SCHEMA", DEFAULT_SOURCE_SCHEMA))
    destination = load_destination_file(os.getenv("DEST_SCHEMA", DEFAULT_DESTINATION_SCHEMA))
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))
    knowledge = load_knowledge()

    routing = {t["source_table"]: t["destination_collection"] for t in oracle["tables"]}

    print(f"source: {source.database}  {len(source.tables)} tables, {source.field_count} fields")
    print(
        f"destination: {destination.database}  {len(destination.collections)} collections, "
        f"{destination.field_count} leaf paths"
    )
    print(f"top_k={args.top_k}  min_score={args.min_score}\n")

    ranks: list[int] = []
    misses: list[str] = []
    chosen_scores: list[float] = []
    unmapped_best: dict[str, float] = {}

    for table_spec in oracle["tables"]:
        table = table_spec["source_table"]
        collection = table_spec["destination_collection"]
        dest_fields = destination.collection(collection)
        expected = table_spec["mappings"]

        print(f"[{table} -> {collection}]")
        for src in source.table(table):
            candidates = shortlist_field(
                src,
                dest_fields,
                knowledge,
                top_k=args.top_k,
                min_score=args.min_score,
                ref_collections=routing,
            )
            paths = [c.path for c in candidates]
            want = expected.get(src.name)

            if want is None:
                best = candidates[0].score if candidates else 0.0
                unmapped_best[f"{table}.{src.name}"] = best
                print(
                    f"  {src.name:<15} EXPECTED UNMAPPED   best={best:.3f} "
                    f"top={paths[0] if paths else '-'}"
                )
                continue

            if want in paths:
                rank = paths.index(want) + 1
                ranks.append(rank)
                score = candidates[rank - 1].score
                chosen_scores.append(score)
                flag = "  " if rank == 1 else "~ "
                print(f"  {flag}{src.name:<15} rank {rank}  score={score:.3f}  -> {want}")
            else:
                misses.append(f"{table}.{src.name} -> {want}")
                print(f"  XX {src.name:<15} MISSING from top-{args.top_k}  -> {want}")

            if args.verbose:
                for c in candidates:
                    s = c.scores
                    print(
                        f"        {c.path:<26} {c.score:.3f}  "
                        f"lex={s.lexical:.2f} fuz={s.fuzzy:.2f} typ={s.type_compat:.2f} "
                        f"key={s.key_role:.2f} cmt={s.comment:.2f}"
                    )
        print()

    total = len(ranks) + len(misses)
    recall = len(ranks) / total if total else 0.0
    rank1 = sum(1 for r in ranks if r == 1) / total if total else 0.0

    print("=" * 64)
    print(f"expected mappings      : {total}")
    print(f"recall@{args.top_k}              : {recall:.1%}  ({len(ranks)}/{total})")
    print(f"rank@1                 : {rank1:.1%}")
    if ranks:
        print(f"mean rank              : {statistics.mean(ranks):.2f}")
        print(f"weakest true match     : {min(chosen_scores):.3f}")
    for name, best in unmapped_best.items():
        print(f"best score for {name} (expected unmapped): {best:.3f}")
    if misses:
        print("\nMISSES (these are unwinnable downstream):")
        for miss in misses:
            print(f"  {miss}")
        return 1

    if chosen_scores and unmapped_best:
        weakest_true = min(chosen_scores)
        strongest_false = max(unmapped_best.values())
        verdict = "separable" if strongest_false < weakest_true else "OVERLAPPING"
        print(
            f"\nunmapped/mapped score separation: {strongest_false:.3f} < {weakest_true:.3f} "
            f"= {verdict}"
        )

    print("\nrecall is 100%: every expected destination is reachable by the adjudicator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
