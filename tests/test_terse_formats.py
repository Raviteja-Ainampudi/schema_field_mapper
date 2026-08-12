"""Pasting the assignment's own pseudo-JSON must work.

A reviewer's most likely first action is to copy Dataset A and Dataset B out of
the assignment, quote them into valid JSON, and paste them into the UI. Dataset B
writes sub-documents inline (``"fullName": {"firstName": String}``) rather than as
``fields`` lists, so this file pins that the terse form yields the same leaf paths
as the bundled schema rather than collapsing each sub-document into one leaf.
"""

from __future__ import annotations

import json

import pytest

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
