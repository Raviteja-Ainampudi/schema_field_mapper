"""Type rendering, note soundness, and the executable interpreter.

The interpreter tests are what make the mapping's claims checkable: if a mapping
says ``A -> active``, running it on a real row has to produce ``active``.
"""

from __future__ import annotations

import json

import pytest

from schema_mapper.transforms import (
    apply_transform,
    build_document,
    notes_are_sound,
    render_source_type,
    render_type_transform,
    suggest_notes,
    transform_rule,
)

from .conftest import SAMPLES


class TestTypeTransformRendering:
    @pytest.mark.parametrize(
        "table,field,collection,path,expected",
        [
            # These four appear verbatim in the assignment's own example output.
            ("emp_master", "emp_id", "employees", "_id", "INT -> ObjectId"),
            (
                "emp_master",
                "f_name",
                "employees",
                "fullName.firstName",
                "VARCHAR -> String (nested path)",
            ),
            (
                "emp_master",
                "rec_stat",
                "employees",
                "employment.status",
                "CHAR(1) code -> String enum (nested path)",
            ),
            (
                "emp_master",
                "is_remote",
                "employees",
                "employment.isRemote",
                "TINYINT(1) -> Boolean (nested path)",
            ),
            (
                "emp_master",
                "base_sal",
                "employees",
                "compensation.baseSalary",
                "DECIMAL(12,2) -> Number (nested path)",
            ),
            (
                "emp_master",
                "hire_dt",
                "employees",
                "employment.startDate",
                "DATETIME -> ISODate (nested path)",
            ),
            ("locations", "country_cd", "locations", "country", "CHAR(2) -> String"),
            ("dept_info", "dept_stat", "departments", "isActive", "CHAR(1) code -> Boolean"),
            (
                "dept_info",
                "parent_dept_id",
                "departments",
                "parentDepartmentId",
                "INT -> ObjectId",
            ),
        ],
    )
    def test_renders_the_expected_label(
        self, field_of, dest_of, table, field, collection, path, expected
    ):
        assert render_type_transform(field_of(table, field), dest_of(collection, path)) == expected

    def test_nested_marker_only_for_nested_paths(self, field_of, dest_of):
        flat = render_type_transform(
            field_of("locations", "city"), dest_of("locations", "city")
        )
        assert "(nested path)" not in flat

    def test_variable_length_strings_drop_their_length(self, field_of):
        """Matching the assignment's "VARCHAR -> String", not "VARCHAR(80)"."""
        assert render_source_type(field_of("emp_master", "f_name")) == "VARCHAR"

    def test_meaningful_parameters_are_kept(self, field_of):
        assert render_source_type(field_of("emp_master", "is_remote")) == "TINYINT(1)"
        assert render_source_type(field_of("emp_master", "base_sal")) == "DECIMAL(12,2)"
        assert render_source_type(field_of("emp_master", "rec_stat")) == "CHAR(1)"


class TestTransformRules:
    @pytest.mark.parametrize(
        "table,field,collection,path,rule",
        [
            ("emp_master", "emp_id", "employees", "_id", "pk_to_objectid"),
            ("emp_master", "dept_id", "employees", "department.departmentId", "fk_to_objectid"),
            ("emp_master", "rec_stat", "employees", "employment.status", "code_to_enum_string"),
            ("dept_info", "dept_stat", "departments", "isActive", "code_to_boolean"),
            ("emp_master", "is_remote", "employees", "employment.isRemote", "tinyint_to_boolean"),
            ("emp_master", "base_sal", "employees", "compensation.baseSalary", "decimal_to_number"),
            ("emp_master", "hire_dt", "employees", "employment.startDate", "datetime_to_isodate"),
            ("locations", "city", "locations", "city", "string_to_string"),
        ],
    )
    def test_rule_identification(self, field_of, dest_of, table, field, collection, path, rule):
        assert transform_rule(field_of(table, field), dest_of(collection, path)) == rule


class TestSuggestedNotes:
    def test_enum_legend_becomes_an_explicit_mapping(self, field_of, dest_of):
        notes = suggest_notes(
            field_of("emp_master", "rec_stat"), dest_of("employees", "employment.status")
        )
        assert notes is not None
        for pair in ("A -> active", "I -> inactive", "T -> terminated"):
            assert pair in notes

    def test_boolean_collapse_is_flagged_as_lossy(self, field_of, dest_of):
        notes = suggest_notes(field_of("dept_info", "dept_stat"), dest_of("departments", "isActive"))
        assert notes is not None and "Lossy" in notes

    def test_primary_key_note_mentions_retention(self, field_of, dest_of):
        notes = suggest_notes(field_of("emp_master", "emp_id"), dest_of("employees", "_id"))
        assert notes is not None and "legacy" in notes.lower()

    def test_foreign_key_note_mentions_remapping(self, field_of, dest_of):
        notes = suggest_notes(
            field_of("emp_master", "office_loc_id"), dest_of("employees", "location.locationId")
        )
        assert notes is not None and "remap" in notes.lower()

    def test_decimal_precision_is_called_out(self, field_of, dest_of):
        notes = suggest_notes(
            field_of("emp_master", "base_sal"), dest_of("employees", "compensation.baseSalary")
        )
        assert notes is not None and "Decimal128" in notes

    def test_straight_copies_have_no_notes(self, field_of, dest_of):
        """A null here is a positive statement, not an omission."""
        assert suggest_notes(field_of("locations", "city"), dest_of("locations", "city")) is None
        assert (
            suggest_notes(field_of("emp_master", "emp_cd"), dest_of("employees", "employeeCode"))
            is None
        )


class TestNotesSoundness:
    def test_rejects_deriving_an_objectid_from_an_integer(self, field_of, dest_of):
        """An ObjectId is 12 bytes generated by MongoDB; you cannot wrap an INT.

        A plausible-sounding but wrong note would mislead whoever writes the
        migration, so it is replaced rather than kept.
        """
        assert not notes_are_sound(
            field_of("locations", "loc_id"),
            dest_of("locations", "_id"),
            "Convert INT to ObjectId by wrapping the integer value.",
        )

    def test_accepts_a_note_describing_the_real_mechanism(self, field_of, dest_of):
        assert notes_are_sound(
            field_of("locations", "loc_id"),
            dest_of("locations", "_id"),
            "MongoDB generates _id; retain the original loc_id for remapping.",
        )

    def test_missing_note_is_unsound_where_work_is_required(self, field_of, dest_of):
        assert not notes_are_sound(
            field_of("emp_master", "dept_id"), dest_of("employees", "department.departmentId"), None
        )

    def test_straight_copies_need_no_note(self, field_of, dest_of):
        assert notes_are_sound(field_of("locations", "city"), dest_of("locations", "city"), None)


class TestInterpreter:
    def test_code_lookup_produces_the_readable_value(self, field_of, dest_of):
        result = apply_transform(
            field_of("emp_master", "rec_stat"), dest_of("employees", "employment.status"), "A"
        )
        assert result.value == "active"
        assert not result.manual

    def test_undocumented_code_is_reported_not_guessed(self, field_of, dest_of):
        result = apply_transform(
            field_of("emp_master", "rec_stat"), dest_of("employees", "employment.status"), "Z"
        )
        assert result.manual
        assert "legend" in (result.detail or "")

    def test_two_state_code_becomes_boolean(self, field_of, dest_of):
        src, dest = field_of("dept_info", "dept_stat"), dest_of("departments", "isActive")
        assert apply_transform(src, dest, "A").value is True
        assert apply_transform(src, dest, "I").value is False

    def test_tinyint_becomes_boolean(self, field_of, dest_of):
        src, dest = field_of("emp_master", "is_remote"), dest_of("employees", "employment.isRemote")
        assert apply_transform(src, dest, 1).value is True
        assert apply_transform(src, dest, 0).value is False

    def test_decimal_becomes_a_number(self, field_of, dest_of):
        result = apply_transform(
            field_of("emp_master", "base_sal"),
            dest_of("employees", "compensation.baseSalary"),
            "125000.50",
        )
        assert result.value == pytest.approx(125000.50)

    def test_datetime_becomes_extended_json(self, field_of, dest_of):
        result = apply_transform(
            field_of("emp_master", "hire_dt"),
            dest_of("employees", "employment.startDate"),
            "2019-04-01 09:00:00",
        )
        assert result.value == {"$date": "2019-04-01T09:00:00Z"}

    def test_date_only_becomes_midnight(self, field_of, dest_of):
        result = apply_transform(
            field_of("emp_master", "dob"), dest_of("employees", "employment.startDate"), "1990-07-14"
        )
        assert result.value == {"$date": "1990-07-14T00:00:00Z"}

    def test_null_stays_null(self, field_of, dest_of):
        result = apply_transform(
            field_of("emp_master", "term_dt"), dest_of("employees", "employment.endDate"), None
        )
        assert result.value is None

    def test_objectid_is_flagged_manual_and_deterministic(self, field_of, dest_of):
        src, dest = field_of("emp_master", "emp_id"), dest_of("employees", "_id")
        first = apply_transform(src, dest, 1001)
        second = apply_transform(src, dest, 1001)
        assert first.manual, "a generated ObjectId is migration-time work, not a cast"
        assert first.value == second.value, "preview values must be reproducible"
        assert len(first.value["$oid"]) == 24
        assert apply_transform(src, dest, 1002).value != first.value


class TestBuildDocument:
    @staticmethod
    @pytest.fixture(scope="class")
    def rows():
        payload = json.loads((SAMPLES / "emp_master_rows.json").read_text(encoding="utf-8"))
        return payload["emp_master"]

    def test_builds_nested_paths_from_a_real_row(self, rows, source_schema, dest_schema):
        row = rows[0]
        src_fields = {f.name: f for f in source_schema.table("emp_master")}
        dest_lookup = {f.path: f for f in dest_schema.collection("employees")}
        mappings = [
            ("f_name", "fullName.firstName"),
            ("l_name", "fullName.lastName"),
            ("is_remote", "employment.isRemote"),
            ("rec_stat", "employment.status"),
            ("base_sal", "compensation.baseSalary"),
        ]
        document, annotations = build_document(row, src_fields, mappings, dest_lookup)

        assert document["fullName"]["firstName"] == row["f_name"]
        assert isinstance(document["employment"]["isRemote"], bool)
        assert document["employment"]["status"] in {"active", "inactive", "terminated"}
        assert isinstance(document["compensation"]["baseSalary"], float)
        assert set(annotations) == {path for _, path in mappings}

    def test_unknown_fields_are_skipped_not_invented(self, rows, source_schema, dest_schema):
        row = rows[0]
        src_fields = {f.name: f for f in source_schema.table("emp_master")}
        dest_lookup = {f.path: f for f in dest_schema.collection("employees")}
        document, _ = build_document(
            row, src_fields, [("not_a_column", "fullName.firstName")], dest_lookup
        )
        assert document == {}
