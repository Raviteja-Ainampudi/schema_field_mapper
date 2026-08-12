"""Prompt templates and tool schemas, one per pipeline stage.

Kept in a dedicated module rather than inline so the exact text a reviewer is
asked to judge is readable in one place, and so the constraint argument can be
checked by reading four short templates.

Two deliberate choices worth stating:

* **No retrieval scores are shown to the model.** Stage 2 knows how strongly
  each candidate scored, but including that in the prompt would anchor the model
  to the top-ranked option. Keeping the model's judgment independent of the
  lexical ranking is what makes blending the two into a confidence value
  meaningful rather than circular.
* **Every prompt names its own boundary.** Stage 1 sees column names with no
  types; Stage 3 sees one batch of fields and only their own candidates. The
  templates cannot be widened by accident, because the data they interpolate is
  assembled by the caller from a scoped tool surface.
"""

from __future__ import annotations

from typing import Any

from .candidates import Candidate
from .knowledge import Snippet
from .normalize import DestField, SourceField

# ---------------------------------------------------------------------------
# Stage 1: table routing (one call per source table)
# ---------------------------------------------------------------------------

ROUTE_SYSTEM = (
    "You are a data architect matching a legacy relational table to its equivalent "
    "collection in a document database. You see column names only, never types or "
    "sample data. Answer with the single best collection."
)

ROUTE_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination_collection": {
            "type": "string",
            "description": "Exactly one of the offered collection names.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1, how certain this pairing is.",
        },
        "reasoning": {
            "type": "string",
            "description": "One plain-English sentence explaining the pairing.",
        },
    },
    "required": ["destination_collection", "confidence", "reasoning"],
}


def route_prompt(table: str, column_names: list[str], collections: list[str]) -> str:
    return (
        f"Legacy table: {table}\n"
        f"Its column names: {', '.join(column_names)}\n\n"
        f"Candidate destination collections: {', '.join(collections)}\n\n"
        "Which collection represents the same entity as this table? "
        "Reply through the tool with the collection name, a confidence between 0 and 1, "
        "and exactly one sentence of reasoning."
    )


# ---------------------------------------------------------------------------
# Stage 3: field adjudication (one call per batch of fields)
# ---------------------------------------------------------------------------

ADJUDICATE_SYSTEM = (
    "You map columns from a legacy MySQL table to field paths in a MongoDB collection.\n"
    "\n"
    "Rules:\n"
    "1. For each source field, choose the single best destination path from the candidate "
    "list given for that field, or null if none is a genuine semantic match.\n"
    "2. Never invent a path. Only paths listed as candidates for that field are valid.\n"
    "3. Prefer null over a weak guess. A wrong mapping is more expensive than an "
    "acknowledged gap.\n"
    "4. reasoning must be exactly one plain-English sentence explaining the match.\n"
    "5. notes must describe any value-transform logic a migration would have to write "
    "(code lookups, boolean conversion, ID remapping, precision loss), or be null when the "
    "value moves across unchanged.\n"
    "6. confidence is your own certainty from 0 to 1, judged on semantics rather than on "
    "how similar the names look."
)

ADJUDICATE_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "description": "One entry per source field, in the order given.",
            "items": {
                "type": "object",
                "properties": {
                    "source_field": {"type": "string"},
                    "destination_field": {
                        "type": ["string", "null"],
                        "description": "A candidate path for this field, or null for no match.",
                    },
                    "confidence": {"type": "number"},
                    "reasoning": {
                        "type": "string",
                        "description": "Exactly one plain-English sentence.",
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": "Value-transform logic required, or null.",
                    },
                },
                "required": [
                    "source_field",
                    "destination_field",
                    "confidence",
                    "reasoning",
                    "notes",
                ],
            },
        }
    },
    "required": ["mappings"],
}


def _render_candidates(candidates: list[Candidate]) -> str:
    if not candidates:
        return "      (no plausible destination path found for this field)"
    lines = []
    for candidate in candidates:
        dest = candidate.field
        detail = dest.bson_type
        if dest.references:
            detail += f", references {dest.references}"
        if dest.comment:
            detail += f", {dest.comment}"
        lines.append(f"      - {dest.path} ({detail})")
    return "\n".join(lines)


def adjudicate_prompt(
    table: str,
    collection: str,
    fields: list[SourceField],
    shortlists: dict[str, list[Candidate]],
    snippets: list[Snippet] | None = None,
    exemplars: list[dict[str, Any]] | None = None,
) -> str:
    parts: list[str] = [
        f"Source table `{table}` maps to destination collection `{collection}`.",
        "",
    ]

    if snippets:
        parts.append("Relevant conventions:")
        for snippet in snippets:
            parts.append(f"  [{snippet.id}] {snippet.text}")
        parts.append("")

    if exemplars:
        parts.append("Previously human-verified mappings for this schema:")
        for ex in exemplars:
            parts.append(
                f"  {ex.get('source_field')} -> {ex.get('destination_field')}"
                + (f" ({ex['notes']})" if ex.get("notes") else "")
            )
        parts.append("")

    parts.append(f"Map these {len(fields)} source fields:")
    for index, fld in enumerate(fields, start=1):
        parts.append(f"  {index}. {fld.describe()}")
        parts.append("    candidate destination paths:")
        parts.append(_render_candidates(shortlists.get(fld.name, [])))
    parts.append("")
    parts.append(
        "Return one entry per source field through the tool, using the exact source field "
        "names given above."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Stage 3c: evaluator-optimizer reflection (one low-confidence field)
# ---------------------------------------------------------------------------

REFLECT_SYSTEM = (
    "You are reviewing one uncertain schema-mapping decision made by another model. "
    "Confirm it or replace it with a better choice from the same candidate list. "
    "Be willing to answer null if no candidate is a genuine match."
)

REFLECT_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination_field": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string", "description": "Exactly one sentence."},
        "notes": {"type": ["string", "null"]},
        "changed": {
            "type": "boolean",
            "description": "True if this differs from the original decision.",
        },
    },
    "required": ["destination_field", "confidence", "reasoning", "notes", "changed"],
}


def reflect_prompt(
    fld: SourceField,
    candidates: list[Candidate],
    original_path: str | None,
    original_reasoning: str,
    original_confidence: float,
) -> str:
    return "\n".join(
        [
            f"Source field: {fld.describe()}",
            "",
            "Candidate destination paths:",
            _render_candidates(candidates),
            "",
            f"Original decision: {original_path or 'null (no match)'}",
            f"Original confidence: {original_confidence:.2f}",
            f"Original reasoning: {original_reasoning}",
            "",
            "Review this decision. Reply with the destination path you would keep or choose "
            "instead (or null), your confidence, one sentence of reasoning, any required value "
            "transform in notes, and whether you changed the decision.",
        ]
    )


# ---------------------------------------------------------------------------
# Stage 4: collision tie-break (exactly two competing source fields)
# ---------------------------------------------------------------------------

TIEBREAK_SYSTEM = (
    "Two source columns have been mapped to the same destination path, which can hold only "
    "one of them. Decide which column is the better owner of that path."
)

TIEBREAK_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "description": "The source field that keeps the path."},
        "reasoning": {"type": "string", "description": "Exactly one sentence."},
    },
    "required": ["winner", "reasoning"],
}


def tiebreak_prompt(
    destination: DestField, first: SourceField, second: SourceField
) -> str:
    return "\n".join(
        [
            f"Destination path: {destination.describe()}",
            "",
            "Competing source columns:",
            f"  A. {first.describe()}",
            f"  B. {second.describe()}",
            "",
            "Which column should own this destination path? Reply with its exact name and one "
            "sentence of reasoning.",
        ]
    )


# ---------------------------------------------------------------------------
# Stage 4: reasoning repair (format only, never content)
# ---------------------------------------------------------------------------

REWRITE_SYSTEM = (
    "You compress schema-mapping explanations to exactly one plain-English sentence, "
    "preserving the original meaning and adding nothing."
)

REWRITE_TOOL: dict[str, Any] = {
    "type": "object",
    "properties": {"reasoning": {"type": "string"}},
    "required": ["reasoning"],
}


def rewrite_prompt(text: str, limit: int) -> str:
    return (
        f"Rewrite this as exactly one sentence of at most {limit} characters, keeping the "
        f"same meaning:\n\n{text}"
    )
