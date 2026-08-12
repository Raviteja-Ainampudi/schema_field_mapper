"""Pasting the assignment's own pseudo-JSON must work.

A reviewer's most likely first action is to copy Dataset A and Dataset B out of
the assignment, quote them into valid JSON, and paste them into the UI. Dataset B
writes sub-documents inline (``"fullName": {"firstName": String}``) rather than as
``fields`` lists, so this file pins that the terse form yields the same leaf paths
as the bundled schema rather than collapsing each sub-document into one leaf.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schema_mapper.config import DATA_DIR
from schema_mapper.normalize import SchemaParseError, load_destination, load_source

TERSE_DEST = json.dumps(
    {
        "database": "people_platform",
        "type": "MongoDB (Document)",
        "collections": {
            "employees": {
                "_id": "ObjectId",
                "employeeCode": "String           -- unique human-readable ID",
                "fullName": {"firstName": "String", "lastName": "String"},
                "employment": {
                    "startDate": "ISODate",
                    "status": "String           -- active / inactive / terminated",
                    "isRemote": "Boolean",
                    "managerId": "ObjectId         -- ref -> employees._id",
                },
            }
        },
    }
)

TERSE_SOURCE = json.dumps(
    {
        "database": "legacy_hrm",
        "tables": {
            "emp_master": {
                "emp_id": "INT PRIMARY KEY",
                "f_name": "VARCHAR(60) NOT NULL",
                "office_loc_id": "INT FK -> locations.loc_id",
                "rec_stat": "CHAR(1) NOT NULL  -- A=Active, I=Inactive, T=Terminated",
            }
        },
    }
)


class TestTerseDestination:
    @staticmethod
    @pytest.fixture(scope="class")
    def schema():
        return load_destination(TERSE_DEST)

    def test_inline_subdocuments_flatten_to_dot_paths(self, schema):
        paths = [f.path for f in schema.collection("employees")]
        assert "fullName.firstName" in paths
        assert "fullName.lastName" in paths
        assert "employment.managerId" in paths

    def test_container_names_are_not_leaves(self, schema):
        paths = {f.path for f in schema.collection("employees")}
        assert "fullName" not in paths
        assert "employment" not in paths

    def test_leaf_count_counts_leaves_only(self, schema):
        # 2 scalars + 2 in fullName + 4 in employment
        assert schema.field_count == 8

    def test_types_survive_the_terse_form(self, schema):
        types = {f.path: f.bson_type for f in schema.collection("employees")}
        assert types["_id"] == "ObjectId"
        assert types["fullName.firstName"] == "String"
        assert types["employment.isRemote"] == "Boolean"

    def test_trailing_comment_is_kept(self, schema):
        comments = {f.path: f.comment for f in schema.collection("employees")}
        assert comments["employeeCode"] == "unique human-readable ID"
        # This comment is what lets rec_stat reach employment.status at all.
        assert comments["employment.status"] == "active / inactive / terminated"

    def test_comment_reference_becomes_a_reference(self, schema):
        managerId = next(
            f for f in schema.collection("employees") if f.path == "employment.managerId"
        )
        assert managerId.references == "employees._id"

    def test_explicit_field_spec_still_wins_over_container_reading(self):
        # A dict carrying spec keys is a field, not a sub-document.
        schema = load_destination(
            json.dumps({"collections": {"c": {"amount": {"type": "Number", "comment": "cents"}}}})
        )
        fields = schema.collection("c")
        assert [f.path for f in fields] == ["amount"]
        assert fields[0].bson_type == "Number"
        assert fields[0].comment == "cents"


class TestSpecKeysAsChildNames:
    """A sub-document whose child is called `name`, `type`, or `comment`.

    These collide with the field-spec vocabulary. `author.name` is the realistic
    case and it regressed once: the sub-document was read as a field spec and
    collapsed to a single leaf called `author`, which then reached the model as a
    candidate path that does not exist in the real schema.
    """

    def test_child_named_name_stays_a_leaf(self):
        schema = load_destination(
            json.dumps({"collections": {"books": {"author": {"name": "String"}}}})
        )
        assert [f.path for f in schema.collection("books")] == ["author.name"]

    def test_child_named_name_keeps_its_type_and_comment(self):
        schema = load_destination(
            json.dumps(
                {"collections": {"books": {"author": {"name": "String  -- full name"}}}}
            )
        )
        field = schema.collection("books")[0]
        assert field.bson_type == "String"
        assert field.comment == "full name"

    def test_subdocument_with_a_type_child_is_not_a_field_spec(self):
        # `type` is a spec key, but `amount` is not, so this is a sub-document.
        schema = load_destination(
            json.dumps(
                {"collections": {"c": {"payment": {"type": "String", "amount": "Number"}}}}
            )
        )
        assert [f.path for f in schema.collection("c")] == ["payment.type", "payment.amount"]

    def test_name_plus_a_spec_key_is_still_a_field_spec(self):
        schema = load_destination(
            json.dumps({"collections": {"c": {"isbn": {"name": "isbn", "type": "String"}}}})
        )
        fields = schema.collection("c")
        assert [f.path for f in fields] == ["isbn"]
        assert fields[0].bson_type == "String"

    def test_bundled_list_form_is_unaffected(self, dest_schema):
        # The list form takes `name` from the item itself, so it must not change.
        paths = [f.path for f in dest_schema.collection("employees")]
        assert "fullName.firstName" in paths
        assert "employment.managerId" in paths


class TestTerseSource:
    @staticmethod
    @pytest.fixture(scope="class")
    def schema():
        return load_source(TERSE_SOURCE)

    def test_shorthand_columns_parse(self, schema):
        assert [f.name for f in schema.table("emp_master")] == [
            "emp_id",
            "f_name",
            "office_loc_id",
            "rec_stat",
        ]

    def test_key_roles_and_comment(self, schema):
        by_name = {f.name: f for f in schema.table("emp_master")}
        assert by_name["emp_id"].is_primary_key
        assert by_name["office_loc_id"].references == "locations.loc_id"
        assert by_name["rec_stat"].comment == "A=Active, I=Inactive, T=Terminated"
        assert by_name["f_name"].nullable is False


class TestBundledSamplesFlattenCleanly:
    """No container may leak into the leaf set, in any shipped sample.

    The leaf count alone cannot catch this: reading `author.name` as `author`
    keeps the total identical, which is exactly how it went unnoticed. The
    structural invariant does catch it - a real leaf is never a prefix of
    another leaf.
    """

    @staticmethod
    def destination_samples() -> list[Path]:
        return [
            path
            for path in sorted((DATA_DIR / "samples").glob("*.json"))
            if "mysql" not in path.name.lower() and not path.name.endswith("_rows.json")
        ]

    def test_there_are_destination_samples_to_check(self):
        assert self.destination_samples()

    def test_no_leaf_is_a_prefix_of_another_leaf(self):
        offenders = []
        for path in self.destination_samples():
            schema = load_destination(path.read_text(encoding="utf-8"))
            for collection in schema.collection_names:
                paths = [f.path for f in schema.collection(collection)]
                for candidate in paths:
                    prefix = f"{candidate}."
                    if any(other.startswith(prefix) for other in paths):
                        offenders.append(f"{path.name}:{collection}.{candidate}")
        assert not offenders, f"container leaked as a leaf: {offenders}"

    def test_library_sample_keeps_the_author_subdocument(self):
        schema = load_destination(
            (DATA_DIR / "samples" / "library_platform.mongo.json").read_text(encoding="utf-8")
        )
        paths = [f.path for f in schema.collection("books")]
        assert "author.name" in paths
        assert "author" not in paths


class TestTerseParity:
    """The terse form and the bundled `fields`-list form must agree exactly."""

    def test_same_paths_as_the_bundled_schema(self, dest_schema):
        subset = json.dumps(
            {
                "collections": {
                    "departments": {
                        "_id": "ObjectId",
                        "code": "String",
                        "name": "String",
                        "parentDepartmentId": "ObjectId         -- self-ref",
                        "headEmployeeId": "ObjectId         -- ref -> employees._id",
                        "costCenterCode": "String",
                        "isActive": "Boolean",
                    }
                }
            }
        )
        terse = load_destination(subset)
        assert [f.path for f in terse.collection("departments")] == [
            f.path for f in dest_schema.collection("departments")
        ]

    def test_empty_object_is_still_an_error(self):
        with pytest.raises(SchemaParseError):
            load_destination(json.dumps({"collections": {"employees": {}}}))
