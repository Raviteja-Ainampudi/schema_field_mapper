"""Stage 4: validation, repair, and the guarantees the assignment asks for.

Guardrails live here as Python rather than as prompt instructions, because a
prompt can be ignored and a validator cannot:

* **Hallucinated-path guard.** A destination path that is not in the allowlist
  built by Stage 0 is rejected outright.
* **Collision resolution.** Two source fields cannot own one destination path.
* **Coverage assertion.** Every source field is either mapped or listed as
  unmapped, and every destination path is either a target or listed as unmapped.
  This is what makes "covers every field across all three source tables"
  checkable rather than hopeful.
* **Reasoning format.** The assignment asks for one plain-English sentence, so
  multi-sentence reasoning is detected and flagged for a cheap rewrite.
* **Contract validation.** The emitted document is checked against the JSON
  Schema generated from the pydantic models, so shape drift cannot ship.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .models import MappingDocument, count_sentences, MAX_REASONING_CHARS, mapping_json_schema
from .normalize import DestinationSchema, SourceSchema


@dataclass
class Diagnostics:
    """Everything that went wrong or needed fixing, surfaced rather than swallowed."""

    schema_violations: list[str] = field(default_factory=list)
    # Invented paths the model *proposed*. Informational: a path that was caught
    # and repaired is the guard working, not a failed run.
    hallucinated_paths: list[str] = field(default_factory=list)
    # Invented paths that survived into the assembled document. Always a failure.
    unresolved_paths: list[str] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    coverage_errors: list[str] = field(default_factory=list)
    reasoning_repairs: list[str] = field(default_factory=list)
    notes_corrections: list[dict[str, Any]] = field(default_factory=list)
    forced_nulls: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.schema_violations or self.coverage_errors or self.unresolved_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema_violations": self.schema_violations,
            "hallucinated_paths_caught": self.hallucinated_paths,
            "unresolved_paths": self.unresolved_paths,
            "collisions": self.collisions,
            "coverage_errors": self.coverage_errors,
            "reasoning_repairs": self.reasoning_repairs,
            "notes_corrections": self.notes_corrections,
            "forced_nulls": self.forced_nulls,
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def is_no_match(value: Any) -> bool:
    """Recognize every way a model says "nothing fits"."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "null", "none", "n/a", "no match", "-"}
    return False


def check_path(path: str, legal: Iterable[str]) -> bool:
    return path in set(legal)


def needs_reasoning_repair(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return count_sentences(stripped) > 1 or len(stripped) > MAX_REASONING_CHARS or "\n" in stripped


def validate_contract(document: dict[str, Any]) -> list[str]:
    """JSON Schema check against the contract, returning readable violations."""
    validator = Draft202012Validator(mapping_json_schema())
    violations: list[str] = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in error.path) or "(root)"
        violations.append(f"{location}: {error.message}")
    return violations


def find_collisions(mappings: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Destination paths claimed by more than one source field."""
    owners: dict[str, list[str]] = {}
    for source_field, destination_path in mappings:
        owners.setdefault(destination_path, []).append(source_field)
    return {path: fields for path, fields in owners.items() if len(fields) > 1}


def check_coverage(
    document: MappingDocument,
    source: SourceSchema,
    destination: DestinationSchema,
) -> list[str]:
    """The assignment's "every field" requirement, as an assertion.

    Reports specifics rather than a boolean, so a failure names the field.
    """
    errors: list[str] = []

    for table in document.tables:
        if table.source_table not in source.tables:
            errors.append(f"unknown source table '{table.source_table}'")
            continue
        expected = {f.name for f in source.table(table.source_table)}
        mapped = {m.source_field for m in table.field_mappings}
        unmapped = set(table.unmapped_source_fields)

        missing = expected - mapped - unmapped
        if missing:
            errors.append(
                f"{table.source_table}: {len(missing)} source field(s) neither mapped nor "
                f"declared unmapped: {', '.join(sorted(missing))}"
            )
        both = mapped & unmapped
        if both:
            errors.append(
                f"{table.source_table}: field(s) both mapped and declared unmapped: "
                f"{', '.join(sorted(both))}"
            )
        unknown = (mapped | unmapped) - expected
        if unknown:
            errors.append(
                f"{table.source_table}: unknown source field(s): {', '.join(sorted(unknown))}"
            )

        if table.destination_collection not in destination.collections:
            errors.append(f"unknown destination collection '{table.destination_collection}'")
            continue
        dest_paths = destination.path_set(table.destination_collection)
        targeted = {m.destination_field for m in table.field_mappings}
        declared_unmapped = set(table.unmapped_destination_fields)

        missing_dest = dest_paths - targeted - declared_unmapped
        if missing_dest:
            errors.append(
                f"{table.destination_collection}: {len(missing_dest)} destination path(s) "
                f"neither targeted nor declared unmapped: {', '.join(sorted(missing_dest))}"
            )
        unknown_dest = (targeted | declared_unmapped) - dest_paths
        if unknown_dest:
            errors.append(
                f"{table.destination_collection}: unknown destination path(s): "
                f"{', '.join(sorted(unknown_dest))}"
            )

    covered_tables = {t.source_table for t in document.tables}
    for table in source.tables:
        if table not in covered_tables:
            errors.append(f"source table '{table}' is missing from the output entirely")

    return errors


def table_confidence(field_confidences: list[float], mapped: int, total: int) -> float:
    """Deterministic table-level confidence.

    The mean of its field confidences, scaled by source coverage, so a table
    cannot report 0.97 while leaving half its columns unmapped. Computed rather
    than asked for, because an aggregate is arithmetic and not a judgment.
    """
    if not field_confidences or total == 0:
        return 0.0
    mean = sum(field_confidences) / len(field_confidences)
    coverage = mapped / total
    return round(mean * (0.7 + 0.3 * coverage), 2)


def blend_confidence(
    model_confidence: float,
    retrieval_margin: float,
    model_weight: float,
    retrieval_weight: float,
    type_penalty: float = 0.0,
    cap: float | None = None,
) -> float:
    """Blend the model's certainty with how decisively retrieval agreed.

    Two weakly correlated signals beat either alone: a model is poorly calibrated
    about itself, and lexical scoring knows nothing about semantics. The cap
    applies where a required transform cannot be expressed mechanically, since
    those always need human review regardless of how obvious the pairing is.
    """
    blended = model_weight * model_confidence + retrieval_weight * retrieval_margin
    blended -= type_penalty
    if cap is not None:
        blended = min(blended, cap)
    return round(max(0.0, min(1.0, blended)), 2)


def full_validation(
    document: MappingDocument,
    source: SourceSchema,
    destination: DestinationSchema,
) -> Diagnostics:
    """Every deterministic check, run against an assembled document."""
    diagnostics = Diagnostics()
    diagnostics.schema_violations = validate_contract(document.to_json_dict())
    diagnostics.coverage_errors = check_coverage(document, source, destination)

    for table in document.tables:
        legal = destination.path_set(table.destination_collection)
        for mapping in table.field_mappings:
            if not check_path(mapping.destination_field, legal):
                diagnostics.unresolved_paths.append(
                    f"{table.source_table}.{mapping.source_field} -> {mapping.destination_field}"
                )
        pairs = [(m.source_field, m.destination_field) for m in table.field_mappings]
        for path, owners in find_collisions(pairs).items():
            diagnostics.collisions.append(
                {"destination_field": path, "claimed_by": owners, "resolved": False}
            )

    return diagnostics
