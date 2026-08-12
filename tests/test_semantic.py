"""Is the committed mapping actually *right*?

Shape tests prove the artifact is well formed, which a confidently wrong mapping
would also pass. These assertions compare it against the hand-derived oracle in
`tests/fixtures/expected_mapping.json`: 33 expected pairs, one justified unmapped
source column, seven denormalized destination paths with no source counterpart.
"""

from __future__ import annotations

import pytest

from schema_mapper.config import THRESHOLDS


@pytest.fixture(scope="module")
def by_table(mapping_document):
    return {t.source_table: t for t in mapping_document.tables}


@pytest.fixture(scope="module")
def oracle_by_table(oracle):
    return {t["source_table"]: t for t in oracle["tables"]}


class TestStructure:
    def test_every_source_table_is_present(self, by_table, oracle_by_table):
        assert set(by_table) == set(oracle_by_table)

    def test_tables_are_paired_with_the_right_collections(self, by_table, oracle_by_table):
        for table, spec in oracle_by_table.items():
            assert by_table[table].destination_collection == spec["destination_collection"]

    def test_totals_match_the_oracle(self, mapping_document, oracle):
        counts = oracle["counts"]
        assert mapping_document.mapped_field_count() == counts["mappings"]
        assert mapping_document.accounted_source_fields() == counts["source_fields"]

    def test_document_labels_name_both_systems(self, mapping_document):
        assert "legacy_hrm" in mapping_document.source
        assert "MySQL" in mapping_document.source
        assert "people_platform" in mapping_document.destination
        assert "MongoDB" in mapping_document.destination


class TestFieldMappingsAreCorrect:
    def test_every_expected_pair_is_present_and_exact(self, by_table, oracle_by_table):
        """The core accuracy assertion: all 33 pairs, exactly as expected."""
        wrong: list[str] = []
        for table, spec in oracle_by_table.items():
            actual = {m.source_field: m.destination_field for m in by_table[table].field_mappings}
            for source_field, expected in spec["mappings"].items():
                got = actual.get(source_field)
                if got != expected:
                    wrong.append(f"{table}.{source_field}: expected {expected}, got {got}")
        assert not wrong, "incorrect mappings:\n" + "\n".join(wrong)

    def test_no_unexpected_mappings(self, by_table, oracle_by_table):
        extra: list[str] = []
        for table, spec in oracle_by_table.items():
            for m in by_table[table].field_mappings:
                if m.source_field not in spec["mappings"]:
                    extra.append(f"{table}.{m.source_field} -> {m.destination_field}")
        assert not extra, "mappings the oracle does not expect:\n" + "\n".join(extra)

    def test_each_destination_path_has_one_owner(self, by_table):
        for table, mapped in by_table.items():
            paths = [m.destination_field for m in mapped.field_mappings]
            assert len(paths) == len(set(paths)), f"{table} maps two columns to one path"


class TestUnmappedFieldsAreDeclaredAndJustified:
    def test_unmapped_source_fields_match_the_oracle(self, by_table, oracle_by_table):
        for table, spec in oracle_by_table.items():
            assert set(by_table[table].unmapped_source_fields) == set(
                spec["unmapped_source_fields"]
            ), table

    def test_dob_is_the_only_unmapped_source_field(self, mapping_document):
        """Not a failure to map, but the correct answer: the target has no birth date."""
        unmapped = [
            (t.source_table, name)
            for t in mapping_document.tables
            for name in t.unmapped_source_fields
        ]
        assert unmapped == [("emp_master", "dob")]

    def test_unmapped_destination_fields_match_the_oracle(self, by_table, oracle_by_table):
        for table, spec in oracle_by_table.items():
            assert set(by_table[table].unmapped_destination_fields) == set(
                spec["unmapped_destination_fields"]
            ), table

    def test_denormalized_paths_are_the_unmapped_ones(self, by_table):
        """The seven untargeted paths are joined copies, not oversights."""
        unmapped = set(by_table["emp_master"].unmapped_destination_fields)
        assert unmapped == {
            "department.code",
            "department.name",
            "location.code",
            "location.name",
            "location.city",
            "location.country",
            "location.timezone",
        }

    def test_lookup_tables_leave_nothing_unmapped(self, by_table):
        for table in ("dept_info", "locations"):
            assert by_table[table].unmapped_source_fields == []
            assert by_table[table].unmapped_destination_fields == []

    def test_report_explains_every_unmapped_field(self, run_report):
        coverage = run_report["coverage"]
        assert "emp_master.dob" in coverage["unmapped_source_explanations"]
        explanations = coverage["unmapped_destination_explanations"]
        for path in ("employees.department.code", "employees.location.timezone"):
            assert path in explanations
            assert "denormalized" in explanations[path].lower()


class TestTransformsHaveSubstance:
    def test_required_type_transforms_are_present(self, by_table, oracle):
        """A type_transform must state the real type move, not just be non-empty."""
        problems: list[str] = []
        for key, expectation in oracle["transform_expectations"].items():
            table, source_field = key.split(".", 1)
            found = next(
                (m for m in by_table[table].field_mappings if m.source_field == source_field), None
            )
            if found is None:
                problems.append(f"{key} is missing from the output")
                continue
            for fragment in expectation.get("type_transform_contains", []):
                if fragment not in found.type_transform:
                    problems.append(
                        f"{key}: type_transform '{found.type_transform}' lacks '{fragment}'"
                    )
        assert not problems, "\n".join(problems)

    def test_notes_are_present_where_migration_work_is_required(self, by_table, oracle):
        """A null note on an ID remap would hide real work from the migration."""
        problems: list[str] = []
        for key, expectation in oracle["transform_expectations"].items():
            if not expectation.get("notes_required"):
                continue
            table, source_field = key.split(".", 1)
            found = next(
                (m for m in by_table[table].field_mappings if m.source_field == source_field), None
            )
            if found is None or not (found.notes or "").strip():
                problems.append(f"{key}: notes are required but missing")
                continue
            wanted = expectation.get("notes_contains_any")
            if wanted and not any(term.lower() in found.notes.lower() for term in wanted):
                problems.append(f"{key}: notes do not mention any of {wanted}: {found.notes!r}")
        assert not problems, "\n".join(problems)

    def test_enum_notes_spell_out_the_value_mapping(self, by_table):
        rec_stat = next(
            m for m in by_table["emp_master"].field_mappings if m.source_field == "rec_stat"
        )
        assert rec_stat.notes is not None
        lowered = rec_stat.notes.lower()
        assert "active" in lowered and "terminated" in lowered

    def test_straight_copies_do_not_invent_work(self, by_table):
        """city -> city needs no note; a fabricated one would be noise."""
        city = next(m for m in by_table["locations"].field_mappings if m.source_field == "city")
        assert city.notes is None

    def test_no_unicode_arrows_survive(self, mapping_document):
        for m in mapping_document.all_mappings:
            assert "\u2192" not in m.type_transform
            assert "\u2192" not in (m.notes or "")

    def test_ascii_arrow_present_in_every_transform(self, mapping_document):
        for m in mapping_document.all_mappings:
            assert "->" in m.type_transform, m.source_field


class TestConfidenceIsDefensible:
    def test_all_confidences_are_in_range(self, mapping_document):
        for m in mapping_document.all_mappings:
            assert 0.0 <= m.confidence <= 1.0
        for t in mapping_document.tables:
            assert 0.0 <= t.confidence <= 1.0

    def test_manual_transforms_are_never_reported_as_certain(self, mapping_document):
        """ObjectId generation and lossy code collapses always need review."""
        for m in mapping_document.all_mappings:
            if "ObjectId" in m.type_transform or "code -> Boolean" in m.type_transform:
                assert m.confidence <= THRESHOLDS.manual_transform_cap, m.source_field

    def test_exact_name_matches_are_confident(self, by_table):
        """city -> city should not be hedged."""
        city = next(m for m in by_table["locations"].field_mappings if m.source_field == "city")
        assert city.confidence >= THRESHOLDS.medium_confidence

    def test_most_mappings_clear_the_review_threshold(self, mapping_document):
        bands = mapping_document.confidence_histogram()
        total = mapping_document.mapped_field_count()
        assert (bands["high"] + bands["medium"]) / total >= 0.80

    def test_reasoning_mentions_something_concrete(self, mapping_document):
        """Guards against filler like "this maps to that"."""
        for m in mapping_document.all_mappings:
            assert len(m.reasoning.split()) >= 6, m.source_field
