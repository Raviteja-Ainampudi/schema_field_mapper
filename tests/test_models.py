"""The output contract. The assignment says "must match this schema exactly"."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schema_mapper.models import (
    MAX_REASONING_CHARS,
    FieldMapping,
    MappingDocument,
    TableMapping,
    count_sentences,
    mapping_json_schema,
    now_iso,
)


def a_mapping(**overrides) -> dict:
    base = {
        "source_field": "emp_cd",
        "destination_field": "employeeCode",
        "type_transform": "VARCHAR -> String",
        "confidence": 0.95,
        "reasoning": "The employee code column maps directly to the employeeCode field.",
        "notes": None,
    }
    base.update(overrides)
    return base


class TestFieldMapping:
    def test_accepts_a_well_formed_mapping(self):
        mapping = FieldMapping(**a_mapping())
        assert mapping.destination_field == "employeeCode"
        assert mapping.notes is None

    def test_emits_keys_in_the_contract_order(self):
        keys = list(FieldMapping(**a_mapping()).model_dump().keys())
        assert keys == [
            "source_field",
            "destination_field",
            "type_transform",
            "confidence",
            "reasoning",
            "notes",
        ]

    def test_rejects_unknown_keys(self):
        """Extra keys would silently drift from the stated contract."""
        with pytest.raises(ValidationError):
            FieldMapping(**a_mapping(extra_field="nope"))

    @pytest.mark.parametrize("confidence", [-0.1, 1.1, 2])
    def test_rejects_out_of_range_confidence(self, confidence):
        with pytest.raises(ValidationError):
            FieldMapping(**a_mapping(confidence=confidence))

    def test_rounds_confidence_to_two_places(self):
        assert FieldMapping(**a_mapping(confidence=0.8765)).confidence == 0.88


class TestReasoningIsOneSentence:
    def test_rejects_two_sentences(self):
        with pytest.raises(ValidationError, match="one sentence"):
            FieldMapping(
                **a_mapping(reasoning="This maps cleanly. The types also agree exactly.")
            )

    def test_rejects_empty_reasoning(self):
        with pytest.raises(ValidationError):
            FieldMapping(**a_mapping(reasoning="   "))

    def test_rejects_overlong_reasoning(self):
        with pytest.raises(ValidationError):
            FieldMapping(**a_mapping(reasoning="word " * 200))

    def test_rejects_multiline_reasoning(self):
        with pytest.raises(ValidationError):
            FieldMapping(**a_mapping(reasoning="First line\nsecond line"))

    @pytest.mark.parametrize(
        "reasoning",
        [
            "The currency code follows ISO 4217, e.g. USD, so no transform is needed.",
            "DECIMAL(12,2) is exact while Number is a double, i.e. precision may be lost.",
            "The value maps directly with no change.",
            "A status code maps to the status enum (A, I, T etc.) via a lookup.",
        ],
    )
    def test_accepts_abbreviations_and_decimals(self, reasoning):
        """Periods inside e.g., i.e., and numbers are not sentence boundaries."""
        assert FieldMapping(**a_mapping(reasoning=reasoning)).reasoning == reasoning

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("One sentence.", 1),
            ("One sentence without a period", 1),
            ("First. Second.", 2),
            ("Uses e.g. an abbreviation.", 1),
            ("Value is 12.50 exactly.", 1),
            ("Question? Answer.", 2),
        ],
    )
    def test_count_sentences(self, text, expected):
        assert count_sentences(text) == expected


class TestNormalization:
    def test_unicode_arrow_becomes_ascii_in_type_transform(self):
        """The assignment's JSON example uses ->, its prose uses an arrow glyph."""
        mapping = FieldMapping(**a_mapping(type_transform="VARCHAR \u2192 String"))
        assert mapping.type_transform == "VARCHAR -> String"

    def test_unicode_arrow_becomes_ascii_in_notes(self):
        mapping = FieldMapping(**a_mapping(notes="Transform: A \u2192 active"))
        assert mapping.notes == "Transform: A -> active"

    @pytest.mark.parametrize("blank", ["", "   ", "null", "None", "N/A", "-"])
    def test_placeholder_notes_become_null(self, blank):
        """An empty string claims something different from "no transform needed"."""
        assert FieldMapping(**a_mapping(notes=blank)).notes is None

    def test_real_notes_are_preserved(self):
        mapping = FieldMapping(**a_mapping(notes="Requires an ID remap table."))
        assert mapping.notes == "Requires an ID remap table."


class TestDocument:
    def a_document(self, **overrides) -> MappingDocument:
        table = TableMapping(
            source_table="emp_master",
            destination_collection="employees",
            confidence=0.9,
            reasoning="Both represent employees.",
            field_mappings=[FieldMapping(**a_mapping())],
            unmapped_source_fields=["dob"],
            unmapped_destination_fields=["department.code"],
        )
        payload = {
            "mapping_version": "1.0",
            "source": "legacy_hrm (MySQL)",
            "destination": "people_platform (MongoDB)",
            "generated_at": now_iso(),
            "tables": [table],
        }
        payload.update(overrides)
        return MappingDocument(**payload)

    def test_round_trips_through_json(self):
        document = self.a_document()
        assert MappingDocument.model_validate(document.to_json_dict()) == document

    def test_rejects_a_bad_timestamp(self):
        with pytest.raises(ValidationError, match="ISO 8601"):
            self.a_document(generated_at="last Tuesday")

    def test_now_iso_is_accepted_and_utc(self):
        assert now_iso().endswith("Z")
        self.a_document(generated_at=now_iso())

    def test_accounting_helpers(self):
        document = self.a_document()
        assert document.mapped_field_count() == 1
        # One mapped plus one declared unmapped: every source field accounted for.
        assert document.accounted_source_fields() == 2

    def test_confidence_histogram_uses_the_pinned_bands(self):
        document = self.a_document()
        document.tables[0].field_mappings = [
            FieldMapping(**a_mapping(confidence=0.95)),
            FieldMapping(**a_mapping(confidence=0.85)),
            FieldMapping(**a_mapping(confidence=0.60)),
        ]
        assert document.confidence_histogram() == {"high": 1, "medium": 1, "review": 1}

    def test_top_level_keys_match_the_assignment(self):
        assert list(self.a_document().to_json_dict().keys()) == [
            "mapping_version",
            "source",
            "destination",
            "generated_at",
            "tables",
        ]

    def test_table_keys_match_the_assignment(self):
        table = self.a_document().to_json_dict()["tables"][0]
        assert list(table.keys()) == [
            "source_table",
            "destination_collection",
            "confidence",
            "reasoning",
            "field_mappings",
            "unmapped_source_fields",
            "unmapped_destination_fields",
        ]


class TestGeneratedSchema:
    def test_schema_describes_the_document(self):
        schema = mapping_json_schema()
        assert schema["type"] == "object"
        assert "tables" in schema["properties"]
        assert set(schema["required"]) >= {
            "mapping_version",
            "source",
            "destination",
            "generated_at",
            "tables",
        }

    def test_reasoning_limit_is_documented_in_one_place(self):
        assert MAX_REASONING_CHARS > 100
