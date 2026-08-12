"""The output contract, enforced rather than described.

The assignment says the produced JSON "must match this schema exactly", so this
module is deliberately strict: unknown keys are rejected, confidence is bounded,
``reasoning`` must be a single plain-English sentence, and ``notes`` is either a
meaningful string or ``null`` and never an empty string masquerading as one.

Field declaration order is the emitted key order, matching the assignment's
example, because a diff against the stated contract should be empty.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_REASONING_CHARS = 260

# Abbreviations whose periods are not sentence boundaries. Without this, a
# perfectly good one-sentence reason containing "e.g." would be rejected.
_ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "etc.",
    "vs.",
    "approx.",
    "cf.",
    "ca.",
    "no.",
    "al.",
    "ISO 4217.",
)

_SENTENCE_SPLIT = re.compile(r"[.!?]+(?:\s+|$)")


def count_sentences(text: str) -> int:
    """Sentence count that tolerates abbreviations and a missing final period."""
    scrubbed = text
    for abbrev in _ABBREVIATIONS:
        scrubbed = scrubbed.replace(abbrev, abbrev.replace(".", "\u2024"))
    # Decimal points are not sentence boundaries either (0.95, DECIMAL(12,2)).
    scrubbed = re.sub(r"(?<=\d)\.(?=\d)", "\u2024", scrubbed)
    parts = [p for p in _SENTENCE_SPLIT.split(scrubbed) if p.strip()]
    return len(parts)


def validate_one_sentence(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > MAX_REASONING_CHARS:
        raise ValueError(
            f"{label} must be one sentence of at most {MAX_REASONING_CHARS} characters "
            f"(got {len(text)})"
        )
    sentences = count_sentences(text)
    if sentences > 1:
        raise ValueError(f"{label} must be exactly one sentence (got {sentences})")
    if "\n" in text:
        raise ValueError(f"{label} must be a single line")
    return text


class FieldMapping(BaseModel):
    """One source column mapped to one destination path."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_field: str = Field(min_length=1)
    destination_field: str = Field(min_length=1)
    type_transform: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    notes: str | None = None

    @field_validator("reasoning")
    @classmethod
    def _one_sentence(cls, value: str) -> str:
        return validate_one_sentence(value, "reasoning")

    @field_validator("type_transform")
    @classmethod
    def _ascii_arrow(cls, value: str) -> str:
        """Pin the ASCII arrow used by the assignment's JSON example.

        Its prose bullets use a Unicode arrow, its JSON uses ``->``. Normalizing
        avoids a gratuitous diff against the contract that was actually stated.
        """
        return value.replace("\u2192", "->").replace("=>", "->").strip()

    @field_validator("notes")
    @classmethod
    def _notes_null_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or text.lower() in {"null", "none", "n/a", "-"}:
            # An empty or placeholder string reads as "considered and nothing to
            # say", which is a different claim from "no transform needed".
            return None
        # Models mix Unicode and ASCII arrows freely; normalize so the artifact
        # is consistent with type_transform and diffable.
        return text.replace("\u2192", "->").replace("\u2019", "'")

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, value: float) -> float:
        return round(value, 2)


class TableMapping(BaseModel):
    """One source table paired with one destination collection."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_table: str = Field(min_length=1)
    destination_collection: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    field_mappings: list[FieldMapping]
    unmapped_source_fields: list[str] = Field(default_factory=list)
    unmapped_destination_fields: list[str] = Field(default_factory=list)

    @field_validator("reasoning")
    @classmethod
    def _one_sentence(cls, value: str) -> str:
        return validate_one_sentence(value, "table reasoning")

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, value: float) -> float:
        return round(value, 2)


class MappingDocument(BaseModel):
    """The single JSON document the assignment asks for."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mapping_version: str
    source: str
    destination: str
    generated_at: str
    tables: list[TableMapping]

    @field_validator("generated_at")
    @classmethod
    def _iso8601(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"generated_at must be ISO 8601: {exc}") from exc
        return value

    # -- convenience -------------------------------------------------------

    @property
    def all_mappings(self) -> list[FieldMapping]:
        return [m for t in self.tables for m in t.field_mappings]

    def mapped_field_count(self) -> int:
        return len(self.all_mappings)

    def accounted_source_fields(self) -> int:
        return sum(
            len(t.field_mappings) + len(t.unmapped_source_fields) for t in self.tables
        )

    def confidence_histogram(self, bands: tuple[float, float] = (0.90, 0.80)) -> dict[str, int]:
        high, medium = bands
        out = {"high": 0, "medium": 0, "review": 0}
        for m in self.all_mappings:
            if m.confidence >= high:
                out["high"] += 1
            elif m.confidence >= medium:
                out["medium"] += 1
            else:
                out["review"] += 1
        return out

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def now_iso() -> str:
    """UTC timestamp with a trailing Z, the form the assignment example uses."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mapping_json_schema() -> dict[str, Any]:
    """JSON Schema for the contract, generated from the models so it cannot drift."""
    return MappingDocument.model_json_schema()
