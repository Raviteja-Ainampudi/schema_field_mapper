"""Type-transform rendering, note suggestion, and an executable interpreter.

A deliberate division of labour. Type moves are mechanical: given
``TINYINT(1)`` and ``Boolean`` there is exactly one right answer, and asking a
model to restate it only creates an opportunity to write ``VARCHAR -> Number``.
So ``type_transform`` is rendered here from the real types, and the model's
proposal is kept as a cross-check rather than as the output.

What the model is genuinely needed for is the semantic half: which destination
path, why, and what value logic a migration has to write. This module supplies a
deterministic baseline for ``notes`` too, which the model can improve on but
cannot silently omit.

The interpreter at the bottom exists so claims are testable: if a mapping says
``A -> active``, we can run it on a real row and see it happen. Anything that
cannot be executed mechanically (generating an ObjectId, joining a denormalized
label) is reported as manual instead of faked.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .normalize import DestField, SourceField, is_enum_code, parse_code_legend, parse_sql_type

NESTED_SUFFIX = " (nested path)"

# Types whose parameters are meaningful in the transform label. The assignment's
# own examples show "VARCHAR -> String" but "TINYINT(1) -> Boolean", so the
# length is dropped for variable-length strings and kept where it changes meaning.
_KEEP_ARGS = {"CHAR", "TINYINT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "BIT"}


def render_source_type(src: SourceField) -> str:
    base, args = parse_sql_type(src.sql_type)
    if base in _KEEP_ARGS and args:
        return f"{base}({','.join(args)})"
    return base


def transform_rule(src: SourceField, dest: DestField) -> str:
    """Stable identifier for the transform kind, shared with the UI interpreter."""
    dest_cat = dest.category
    if dest.path == "_id" and src.is_primary_key:
        return "pk_to_objectid"
    if dest_cat == "objectid":
        return "fk_to_objectid"
    if is_enum_code(src):
        if dest_cat == "boolean":
            return "code_to_boolean"
        if dest_cat == "string":
            return "code_to_enum_string"
    if src.category == "boolean" and dest_cat == "boolean":
        return "tinyint_to_boolean"
    if src.category == "decimal" and dest_cat == "number":
        return "decimal_to_number"
    if src.category == "integer" and dest_cat == "number":
        return "int_to_number"
    if src.category in {"date", "datetime"} and dest_cat == "datetime":
        return "datetime_to_isodate"
    if dest_cat == "string":
        return "string_to_string"
    return "direct"


def render_type_transform(src: SourceField, dest: DestField) -> str:
    """The ``type_transform`` string, factual by construction."""
    source_label = render_source_type(src)
    rule = transform_rule(src, dest)

    if rule in {"pk_to_objectid", "fk_to_objectid"}:
        body = f"{source_label} -> ObjectId"
    elif rule == "code_to_enum_string":
        body = f"{source_label} code -> String enum"
    elif rule == "code_to_boolean":
        body = f"{source_label} code -> Boolean"
    else:
        body = f"{source_label} -> {dest.bson_type}"

    # Flag promotion into a sub-document, as the assignment's f_name example does.
    if "." in dest.path:
        body += NESTED_SUFFIX
    return body


def suggest_notes(src: SourceField, dest: DestField) -> str | None:
    """Deterministic baseline for ``notes``: the migration work this move implies.

    Returns ``None`` only when the move really is a straight copy, so a null in
    the output is a positive statement rather than an omission.
    """
    rule = transform_rule(src, dest)
    legend = parse_code_legend(src.comment)

    if rule == "pk_to_objectid":
        return (
            f"MongoDB generates _id; retain the original {src.name} in a legacy field so "
            "foreign keys can be remapped."
        )
    if rule == "fk_to_objectid":
        target = src.references or "the referenced table"
        return (
            f"Resolve through a legacy-to-ObjectId remap table for {target}, applied after "
            "that collection is loaded."
        )
    if rule == "code_to_enum_string":
        if legend:
            pairs = ", ".join(f"{code} -> {label.lower()}" for code, label in legend.items())
            return f"Transform: {pairs}"
        return "Requires a lookup from single-character codes to readable strings."
    if rule == "code_to_boolean":
        if legend:
            codes = list(legend)
            pairs = f"{codes[0]} -> true" + (f", {codes[1]} -> false" if len(codes) > 1 else "")
            extra = "" if len(codes) <= 2 else "; additional codes have no Boolean representation"
            return f"Lossy: {pairs}{extra}."
        return "Lossy: a multi-state code collapses to two states."
    if rule == "tinyint_to_boolean":
        return "Transform: 0 -> false, 1 -> true."
    if rule == "decimal_to_number":
        base, args = parse_sql_type(src.sql_type)
        precision = f"{base}({','.join(args)})" if args else base
        return (
            f"{precision} is exact but Number is a double; use Decimal128 or minor units for "
            "exact currency arithmetic."
        )
    if rule == "datetime_to_isodate":
        if src.category == "date":
            return "Date-only value becomes midnight UTC; confirm the intended timezone."
        return "MySQL DATETIME carries no timezone; values are assumed to be UTC."
    return None


# ---------------------------------------------------------------------------
# Executable interpreter
# ---------------------------------------------------------------------------


@dataclass
class TransformResult:
    value: Any
    rule: str
    manual: bool = False
    detail: str | None = None


def _stable_object_id(table: str, key: Any) -> str:
    """Deterministic stand-in for a generated ObjectId.

    Real migrations let MongoDB generate these. A stable hash keeps the preview
    reproducible and makes it obvious the value is synthetic, not authoritative.
    """
    digest = hashlib.sha1(f"{table}:{key}".encode("utf-8")).hexdigest()
    return digest[:24]


# Notes that must mention the real mechanism, keyed by transform rule. An
# ObjectId cannot be derived from an integer, so a note claiming otherwise is
# not a style problem, it is wrong and would mislead whoever writes the migration.
_REQUIRED_NOTE_TERMS: dict[str, tuple[str, ...]] = {
    "pk_to_objectid": ("remap", "legacy", "generate", "generated", "retain", "preserve"),
    "fk_to_objectid": ("remap", "lookup", "mapping table", "legacy", "resolve"),
    "code_to_enum_string": ("->", "transform", "lookup", "map"),
    "code_to_boolean": ("->", "true", "false", "lossy"),
}


def notes_are_sound(src: SourceField, dest: DestField, notes: str | None) -> bool:
    """Does this note describe the mechanism the transform actually requires?

    Only applied to rules where a plausible-sounding note can be materially
    wrong. Everything else is left to the model's own words.
    """
    rule = transform_rule(src, dest)
    required = _REQUIRED_NOTE_TERMS.get(rule)
    if required is None:
        return True
    if not notes:
        return False
    lowered = notes.lower()
    if rule in {"pk_to_objectid", "fk_to_objectid"} and "wrap" in lowered:
        return False
    return any(term in lowered for term in required)


def apply_transform(src: SourceField, dest: DestField, value: Any) -> TransformResult:
    """Execute a mapping against one source value."""
    rule = transform_rule(src, dest)

    if value is None:
        return TransformResult(None, rule)

    if rule in {"pk_to_objectid", "fk_to_objectid"}:
        table = (src.references or src.table).split(".", 1)[0]
        return TransformResult(
            {"$oid": _stable_object_id(table, value)},
            rule,
            manual=True,
            detail=f"ObjectId generated at migration time; legacy key {value!r} preserved.",
        )

    if rule == "code_to_enum_string":
        legend = parse_code_legend(src.comment)
        label = legend.get(str(value))
        if label is None:
            return TransformResult(
                str(value),
                rule,
                manual=True,
                detail=f"Code {value!r} is not in the documented legend.",
            )
        return TransformResult(label.lower(), rule)

    if rule == "code_to_boolean":
        legend = parse_code_legend(src.comment)
        codes = list(legend)
        if not codes:
            return TransformResult(bool(value), rule, manual=True)
        if str(value) == codes[0]:
            return TransformResult(True, rule)
        if len(codes) > 1 and str(value) == codes[1]:
            return TransformResult(False, rule)
        return TransformResult(
            False,
            rule,
            manual=True,
            detail=f"Code {value!r} has no Boolean equivalent; defaulted to false.",
        )

    if rule == "tinyint_to_boolean":
        return TransformResult(bool(int(value)), rule)

    if rule == "decimal_to_number":
        return TransformResult(float(value), rule)

    if rule == "int_to_number":
        return TransformResult(int(value), rule)

    if rule == "datetime_to_isodate":
        return TransformResult({"$date": _to_iso(value)}, rule)

    return TransformResult(value, rule)


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "") + "Z"
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.isoformat() + "Z"
    return text


def build_document(
    row: dict[str, Any],
    src_fields: dict[str, SourceField],
    mappings: list[tuple[str, str]],
    dest_lookup: dict[str, DestField],
) -> tuple[dict[str, Any], dict[str, TransformResult]]:
    """Assemble a destination document from one source row.

    Proves the mapping end to end: nested paths are actually created, and each
    leaf records which rule produced it so the UI can annotate every value.
    """
    document: dict[str, Any] = {}
    annotations: dict[str, TransformResult] = {}

    for source_field, destination_path in mappings:
        src = src_fields.get(source_field)
        dest = dest_lookup.get(destination_path)
        if src is None or dest is None or source_field not in row:
            continue
        result = apply_transform(src, dest, row[source_field])
        annotations[destination_path] = result

        cursor = document
        parts = destination_path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):  # pragma: no cover - defensive
                break
        if isinstance(cursor, dict):
            cursor[parts[-1]] = result.value

    return document, annotations
