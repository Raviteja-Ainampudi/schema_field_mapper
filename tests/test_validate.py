"""Stage 4 guardrails: coverage, collisions, confidence arithmetic, contract."""

from __future__ import annotations

import pytest

from schema_mapper.config import THRESHOLDS
from schema_mapper.models import FieldMapping, MappingDocument, TableMapping, now_iso
from schema_mapper.validate import (
    blend_confidence,
    check_coverage,
    check_path,
    find_collisions,
    full_validation,
    is_no_match,
    needs_reasoning_repair,
    table_confidence,
    validate_contract,
)


def mapping(source_field: str, destination_field: str, confidence: float = 0.9) -> FieldMapping:
    return FieldMapping(
        source_field=source_field,
        destination_field=destination_field,
        type_transform="VARCHAR -> String",
        confidence=confidence,
        reasoning="A single explanatory sentence.",
        notes=None,
    )


def one_table_document(
    mappings: list[FieldMapping],
    unmapped_source: list[str],
    unmapped_dest: list[str],
    table: str = "locations",
    collection: str = "locations",
) -> MappingDocument:
    return MappingDocument(
        mapping_version="1.0",
        source="legacy_hrm (MySQL)",
        destination="people_platform (MongoDB)",
        generated_at=now_iso(),
        tables=[
            TableMapping(
                source_table=table,
                destination_collection=collection,
                confidence=0.9,
                reasoning="Same entity.",
                field_mappings=mappings,
                unmapped_source_fields=unmapped_source,
                unmapped_destination_fields=unmapped_dest,
            )
        ],
    )


class TestNoMatchDetection:
    @pytest.mark.parametrize("value", [None, "", "  ", "null", "NULL", "None", "n/a", "no match", "-"])
    def test_recognizes_every_way_a_model_says_nothing_fits(self, value):
        assert is_no_match(value)

    @pytest.mark.parametrize("value", ["_id", "employment.status", "code"])
    def test_real_paths_are_not_no_match(self, value):
        assert not is_no_match(value)


class TestPathGuard:
    def test_accepts_known_paths_and_rejects_invented_ones(self, dest_schema):
        legal = dest_schema.path_set("employees")
        assert check_path("employment.status", legal)
        assert not check_path("employment.statuses", legal)
        # A container object is not a legal destination for a scalar column.
        assert not check_path("employment", legal)


class TestCoverage:
    def test_passes_when_every_field_is_accounted_for(self, source_schema, dest_schema):
        """A complete locations table with all eight columns mapped.

        The document intentionally covers one table, so the only errors expected
        are the two about the other source tables being absent.
        """
        pairs = [
            ("loc_id", "_id"),
            ("loc_cd", "code"),
            ("loc_nm", "name"),
            ("city", "city"),
            ("state_prov", "stateOrProvince"),
            ("country_cd", "country"),
            ("postal_cd", "postalCode"),
            ("tz_cd", "timezone"),
        ]
        document = one_table_document([mapping(s, d) for s, d in pairs], [], [])
        errors = check_coverage(document, source_schema, dest_schema)
        assert [e for e in errors if "missing from the output" not in e] == []
        assert len(errors) == 2  # emp_master and dept_info

    def test_detects_a_silently_dropped_source_field(self, source_schema, dest_schema):
        """The assignment's "every field" requirement, as a failing assertion."""
        document = one_table_document([mapping("loc_id", "_id")], [], [])
        errors = check_coverage(document, source_schema, dest_schema)
        assert any("neither mapped nor declared unmapped" in e for e in errors)
        assert any("city" in e for e in errors)

    def test_detects_a_field_both_mapped_and_declared_unmapped(self, source_schema, dest_schema):
        document = one_table_document([mapping("city", "city")], ["city"], [])
        errors = check_coverage(document, source_schema, dest_schema)
        assert any("both mapped and declared unmapped" in e for e in errors)

    def test_detects_an_unknown_source_field(self, source_schema, dest_schema):
        document = one_table_document([mapping("not_a_column", "city")], [], [])
        errors = check_coverage(document, source_schema, dest_schema)
        assert any("unknown source field" in e for e in errors)

    def test_detects_a_missing_source_table(self, source_schema, dest_schema):
        document = one_table_document([mapping("city", "city")], [], [])
        errors = check_coverage(document, source_schema, dest_schema)
        assert any("emp_master" in e and "missing from the output" in e for e in errors)

    def test_committed_artifact_has_full_coverage(
        self, mapping_document, source_schema, dest_schema
    ):
        assert check_coverage(mapping_document, source_schema, dest_schema) == []


class TestCollisions:
    def test_finds_a_contested_destination(self):
        collisions = find_collisions(
            [("f_name", "fullName.firstName"), ("l_name", "fullName.firstName")]
        )
        assert collisions == {"fullName.firstName": ["f_name", "l_name"]}

    def test_distinct_targets_are_not_collisions(self):
        assert find_collisions([("a", "x"), ("b", "y")]) == {}

    def test_committed_artifact_has_no_collisions(self, mapping_document):
        for table in mapping_document.tables:
            pairs = [(m.source_field, m.destination_field) for m in table.field_mappings]
            assert find_collisions(pairs) == {}, table.source_table


class TestReasoningFormat:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "Two sentences here. And a second one.",
            "A line\nbreak",
            "x" * 400,
        ],
    )
    def test_flags_non_compliant_reasoning(self, text):
        assert needs_reasoning_repair(text)

    def test_accepts_a_single_sentence(self):
        assert not needs_reasoning_repair("One clear explanatory sentence about the mapping.")

    def test_committed_artifact_needs_no_repair(self, mapping_document):
        for table in mapping_document.tables:
            assert not needs_reasoning_repair(table.reasoning), table.source_table
            for m in table.field_mappings:
                assert not needs_reasoning_repair(m.reasoning), m.source_field


class TestConfidenceArithmetic:
    def test_table_confidence_is_scaled_by_coverage(self):
        """A table cannot report near-perfect confidence while leaving gaps."""
        full = table_confidence([0.9, 0.9], mapped=2, total=2)
        partial = table_confidence([0.9, 0.9], mapped=2, total=4)
        assert full > partial
        assert partial < 0.9

    def test_no_mappings_means_no_confidence(self):
        assert table_confidence([], mapped=0, total=5) == 0.0

    def test_blend_weights_both_signals(self):
        high_agreement = blend_confidence(0.9, 0.9, 0.6, 0.4)
        model_only = blend_confidence(0.9, 0.1, 0.6, 0.4)
        assert high_agreement > model_only
        assert 0.0 <= model_only <= 1.0

    def test_type_penalty_lowers_confidence(self):
        clean = blend_confidence(0.9, 0.9, 0.6, 0.4)
        penalized = blend_confidence(0.9, 0.9, 0.6, 0.4, type_penalty=0.05)
        assert penalized == pytest.approx(clean - 0.05, abs=0.011)

    def test_manual_transform_cap_forces_review(self):
        """Work a human must write cannot be reported as a certainty."""
        capped = blend_confidence(1.0, 1.0, 0.6, 0.4, cap=THRESHOLDS.manual_transform_cap)
        assert capped == THRESHOLDS.manual_transform_cap
        assert capped < THRESHOLDS.high_confidence

    def test_result_is_always_in_range(self):
        assert blend_confidence(1.0, 1.0, 0.6, 0.4) <= 1.0
        assert blend_confidence(0.0, 0.0, 0.6, 0.4, type_penalty=0.5) >= 0.0


class TestContractValidation:
    def test_valid_document_has_no_violations(self, mapping_document):
        assert validate_contract(mapping_document.to_json_dict()) == []

    def test_reports_a_readable_location_for_violations(self):
        broken = {
            "mapping_version": "1.0",
            "source": "a",
            "destination": "b",
            "generated_at": now_iso(),
            "tables": [{"source_table": "t"}],
        }
        violations = validate_contract(broken)
        assert violations
        assert any("tables" in v for v in violations)

    def test_full_validation_of_the_committed_artifact(
        self, mapping_document, source_schema, dest_schema
    ):
        diagnostics = full_validation(mapping_document, source_schema, dest_schema)
        assert diagnostics.ok, diagnostics.as_dict()
        assert diagnostics.hallucinated_paths == []
        assert diagnostics.collisions == []

    def test_full_validation_catches_an_invented_path(self, source_schema, dest_schema):
        document = one_table_document(
            [mapping("city", "cityName")],
            ["loc_id", "loc_cd", "loc_nm", "state_prov", "country_cd", "postal_cd", "tz_cd"],
            [],
        )
        diagnostics = full_validation(document, source_schema, dest_schema)
        # An invented path found in an assembled document is unresolved, not a
        # caught proposal, so it fails the run.
        assert diagnostics.unresolved_paths == ["locations.city -> cityName"]
        assert not diagnostics.ok
