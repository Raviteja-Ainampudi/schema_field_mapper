"""Headless entry point. Same code path the API uses, so they cannot diverge.

    python -m schema_mapper.cli                     # live Bedrock run
    python -m schema_mapper.cli --offline           # replay cassettes, no AWS
    python -m schema_mapper.cli --record            # live run + record cassettes

Exit codes: 0 success, 1 validation or coverage failure, 2 configuration or
Bedrock error. A non-zero exit on failed coverage is deliberate, so a broken run
cannot quietly overwrite a good committed artifact.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .bedrock import (
    BedrockClient,
    BedrockError,
    CassetteMissing,
    CassetteStore,
    OfflineClient,
)
from .config import (
    CASSETTE_DIR,
    DEFAULT_DESTINATION_SCHEMA,
    DEFAULT_SOURCE_SCHEMA,
    THRESHOLDS,
    load_settings,
    output_dir,
)
from .cost import BudgetExceeded, CostLedger
from .knowledge import load_knowledge
from .normalize import SchemaParseError, load_destination_file, load_source_file
from .pipeline import Pipeline, RunResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schema_mapper",
        description="Map every MySQL source field to its MongoDB destination path.",
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_SCHEMA)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION_SCHEMA)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Replay recorded cassettes instead of calling Bedrock. Needs no credentials.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Record this live run's exchanges as cassettes for offline replay.",
    )
    parser.add_argument("--cassette-dir", type=Path, default=CASSETTE_DIR)
    parser.add_argument("--output", type=Path, default=None, help="Directory for artifacts.")
    parser.add_argument("--router-model", default=None)
    parser.add_argument("--mapper-model", default=None)
    parser.add_argument("--cheap-mapper-model", default=None)
    parser.add_argument("--no-cascade", action="store_true")
    parser.add_argument("--no-reflection", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--batch-size", type=int, default=THRESHOLDS.batch_size)
    parser.add_argument("--top-k", type=int, default=THRESHOLDS.top_k)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _progress(quiet: bool):
    def emit(event: dict) -> None:
        if quiet:
            return
        kind = event.get("type")
        if kind == "stage_start":
            print(f"  [{event['stage']}] {event['label']}...")
        elif kind == "stage_end":
            detail = {
                k: v
                for k, v in event.items()
                if k not in {"type", "stage", "label", "duration_ms"}
            }
            print(f"  [{event['stage']}] done in {event['duration_ms']}ms {detail}")
        elif kind == "route":
            print(f"      {event['table']} -> {event['collection']} ({event['confidence']:.2f})")
        elif kind == "batch":
            print(
                f"      batch {event['batch']}/{event['of']} of {event['table']}: "
                f"{', '.join(event['fields'])}"
            )
        elif kind == "escalate":
            print(f"      escalating to strong model: {', '.join(event['fields'])}")

    return emit


def summarize(result: RunResult) -> str:
    report = result.report
    coverage = report["coverage"]
    quality = report["quality"]
    constraint = report["constraint"]
    cost = report.get("cost", {})
    bands = quality["confidence_histogram"]

    lines = [
        "",
        "=" * 68,
        f"  mode          : {report['mode']}",
        f"  tables        : {', '.join(f'{k} -> {v}' for k, v in report['routing']['pairings'].items())}",
        f"  coverage      : {coverage['source_fields_mapped']}/{coverage['source_fields_total']} "
        f"source fields mapped, {coverage['source_fields_unmapped']} unmapped "
        f"(all {coverage['accounted_source_fields']} accounted for)",
        f"  destination   : {coverage['destination_paths_targeted']}/"
        f"{coverage['destination_paths_total']} leaf paths targeted",
        f"  confidence    : mean {quality['mean_confidence']}, "
        f"high {bands['high']} / medium {bands['medium']} / review {bands['review']}",
        f"  escalation    : {quality['escalation_rate']:.0%} of fields reached the strong model",
        f"  repairs       : {quality['repaired']} reasoning/path, {quality['tie_broken']} tie-broken",
        "",
        "  constraint proof",
        f"    LLM calls                        : {constraint['total_llm_calls']}",
        f"    source tables per prompt (max)   : "
        f"{constraint['max_source_tables_in_one_prompt']} of {constraint['total_source_tables']}",
        f"    typed source fields per prompt   : "
        f"{constraint['max_typed_source_fields_in_one_prompt']} of "
        f"{constraint['total_source_fields']}",
        f"    column names only, per prompt    : "
        f"{constraint['max_named_source_fields_in_one_prompt']} of "
        f"{constraint['total_source_fields']} (routing sees names, no types)",
        f"    dest paths per prompt (max)      : "
        f"{constraint['max_destination_paths_in_one_prompt']} of "
        f"{constraint['total_destination_paths']} (candidate shortlists)",
        f"    mappings from any one call       : "
        f"{constraint['max_mappings_from_one_call']} of {coverage['source_fields_mapped']}",
        f"    prompts with both full schemas   : "
        f"{constraint['prompts_containing_both_full_schemas']}",
        f"    largest prompt                   : "
        f"{constraint['max_input_tokens_in_one_prompt']} input tokens "
        f"(vs {constraint['both_schemas_counterfactual_tokens']} to paste both schemas)",
    ]

    if cost:
        label = "cost" if cost.get("billed", True) else "cost (recorded)"
        lines += [
            "",
            f"  {label:<14}: ${cost['total_usd']:.4f} "
            f"({cost['total_input_tokens']} in / {cost['total_output_tokens']} out tokens, "
            f"{cost['billable_calls']} billable calls, {cost['cache_hits']} cache hits)",
            f"  per field     : ${cost['cost_per_mapped_field']:.5f}",
        ]
        if not cost.get("billed", True):
            lines.append("                  replayed from cassettes; this run spent nothing")

    diagnostics = report["diagnostics"]
    caught = diagnostics.get("hallucinated_paths_caught") or []
    lines.append("")
    if diagnostics["ok"]:
        lines.append("  validation    : PASS (contract, coverage, every path in schema)")
        if caught:
            lines.append(
                f"                  {len(caught)} invented path(s) caught and repaired"
            )
    else:
        lines.append("  validation    : FAIL")
        for key in ("schema_violations", "coverage_errors", "unresolved_paths"):
            for problem in diagnostics.get(key) or []:
                lines.append(f"      {key}: {problem}")
    lines.append("=" * 68)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    if args.router_model:
        settings.router_model = args.router_model
    if args.mapper_model:
        settings.mapper_model = args.mapper_model
    if args.cheap_mapper_model:
        settings.cheap_mapper_model = args.cheap_mapper_model
    if args.no_cascade:
        settings.enable_cascade = False
    if args.no_reflection:
        settings.enable_reflection = False

    try:
        source = load_source_file(args.source)
        destination = load_destination_file(args.destination)
    except (SchemaParseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(
            f"source: {source.database} ({len(source.tables)} tables, "
            f"{source.field_count} fields)"
        )
        print(
            f"destination: {destination.database} ({len(destination.collections)} collections, "
            f"{destination.field_count} leaf paths)"
        )

    ledger = CostLedger(max_tokens=settings.max_tokens_per_run)
    if args.offline:
        client = OfflineClient(
            cassettes=CassetteStore(args.cassette_dir, record=False), ledger=ledger
        )
    else:
        client = BedrockClient(
            region=settings.region,
            ledger=ledger,
            cassettes=CassetteStore(args.cassette_dir, record=args.record),
            use_cache=settings.enable_cache and not args.no_cache,
        )

    knowledge = load_knowledge()
    raw_chars = 0
    for path in (args.source, args.destination):
        try:
            raw_chars += len(Path(path).read_text(encoding="utf-8"))
        except OSError:
            pass

    pipeline = Pipeline(
        client=client,
        source=source,
        destination=destination,
        settings=settings,
        knowledge=knowledge,
        progress=_progress(args.quiet),
        raw_schema_chars=raw_chars,
    )
    pipeline.tools.batch_size = args.batch_size
    pipeline.tools.top_k = args.top_k

    try:
        result = pipeline.run()
    except CassetteMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BudgetExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BedrockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    destination_dir = Path(args.output) if args.output else output_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = (
        destination_dir / f"mapping_{source.database}_to_{destination.database}.json"
    )
    mapping_path.write_text(
        json.dumps(result.document.to_json_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (destination_dir / "run_report.json").write_text(
        json.dumps(result.report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (destination_dir / "prompt_trace.json").write_text(
        json.dumps(result.trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not args.quiet:
        print(summarize(result))
        print(f"\nwrote {mapping_path}")
        print(f"wrote {destination_dir / 'run_report.json'}")
        print(f"wrote {destination_dir / 'prompt_trace.json'}")

    return 0 if result.diagnostics.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
