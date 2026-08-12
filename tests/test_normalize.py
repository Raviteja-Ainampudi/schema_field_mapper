"""Stage 0: parsing four input formats into one intermediate representation.

The parity tests are the important ones. The pipeline accepts a schema as
structured JSON, as MySQL DDL, or as sample MongoDB documents, and downstream
stages must not be able to tell which was used.
"""

from __future__ import annotations

import pytest

from schema_mapper.normalize import (
    SchemaParseError,
    bson_category,
    detect_format,
    is_enum_code,
    load_destination_file,
    load_source_file,
    parse_code_legend,
    parse_sql_type,
    sql_category,
)

from .conftest import SAMPLES


class TestSourceSchema:
    def test_counts_match_the_assignment(self, source_schema):
        assert source_schema.database == "legacy_hrm"
        assert source_schema.table_names == ["emp_master", "dept_info", "locations"]
        assert source_schema.field_count == 34
        assert len(source_schema.table("emp_master")) == 19
        assert len(source_schema.table("dept_info")) == 7
        assert len(source_schema.table("locations")) == 8

    def test_keys_and_references(self, field_of):
        emp_id = field_of("emp_master", "emp_id")
        assert emp_id.is_primary_key
        assert not emp_id.is_foreign_key

        dept_id = field_of("emp_master", "dept_id")
        assert dept_id.is_foreign_key
        assert dept_id.references == "dept_info.dept_id"

        emp_cd = field_of("emp_master", "emp_cd")
        assert emp_cd.is_unique

    def test_type_categories(self, field_of):
        assert field_of("emp_master", "emp_id").category == "integer"
        assert field_of("emp_master", "base_sal").category == "decimal"
        assert field_of("emp_master", "f_name").category == "string"
        assert field_of("emp_master", "hire_dt").category == "datetime"
        assert field_of("emp_master", "dob").category == "date"
        # TINYINT(1) is MySQL's boolean idiom and must not read as an integer.
        assert field_of("emp_master", "is_remote").category == "boolean"

    def test_enum_code_detection(self, field_of):
        rec_stat = field_of("emp_master", "rec_stat")
        assert is_enum_code(rec_stat)
        assert parse_code_legend(rec_stat.comment) == {
            "A": "Active",
            "I": "Inactive",
            "T": "Terminated",
        }
        # A CHAR(3) currency code is not a single-character enum.
        assert not is_enum_code(field_of("emp_master", "sal_currency"))
        assert not is_enum_code(field_of("emp_master", "emp_cd"))

    def test_describe_is_single_line(self, source_schema):
        for fld in source_schema.fields():
            assert "\n" not in fld.describe()
            assert fld.name in fld.describe()


class TestDestinationSchema:
    def test_leaf_paths_only(self, dest_schema):
        assert dest_schema.database == "people_platform"
        assert dest_schema.collection_names == ["employees", "departments", "locations"]
        assert dest_schema.field_count == 40
        assert len(dest_schema.collection("employees")) == 25
        assert len(dest_schema.collection("departments")) == 7
        assert len(dest_schema.collection("locations")) == 8

    def test_container_objects_are_not_destinations(self, dest_schema):
        paths = dest_schema.path_set("employees")
        assert "fullName.firstName" in paths
        assert "fullName.lastName" in paths
        # The container itself cannot receive a scalar column.
        assert "fullName" not in paths
        assert "employment" not in paths
        assert "meta" not in paths

    def test_nested_dot_paths(self, dest_schema):
        paths = dest_schema.path_set("employees")
        for expected in (
            "employment.startDate",
            "compensation.baseSalary",
            "contact.email",
            "department.departmentId",
            "location.timezone",
            "meta.createdAt",
        ):
            assert expected in paths

    def test_references_are_captured(self, dest_of):
        dept = dest_of("employees", "department.departmentId")
        assert dept.is_ref
        assert dept.references == "departments._id"
        assert dest_of("employees", "contact.email").is_ref is False

    def test_bson_categories(self, dest_of):
        assert dest_of("employees", "_id").category == "objectid"
        assert dest_of("employees", "employment.startDate").category == "datetime"
        assert dest_of("employees", "employment.isRemote").category == "boolean"
        assert dest_of("employees", "compensation.baseSalary").category == "number"


class TestDDLParity:
    """The DDL sample must produce the same IR as the JSON form.

    This is why `data/samples/legacy_hrm.ddl.sql` exists: it is a parser fixture,
    not a migration script, and nothing ever executes it against a database.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def from_ddl():
        return load_source_file(SAMPLES / "legacy_hrm.ddl.sql")

    def test_same_tables_and_columns_in_order(self, from_ddl, source_schema):
        assert from_ddl.table_names == source_schema.table_names
        assert from_ddl.field_count == source_schema.field_count
        for table in source_schema.table_names:
            assert [f.name for f in from_ddl.table(table)] == [
                f.name for f in source_schema.table(table)
            ]

    def test_same_categories_keys_and_comments(self, from_ddl, source_schema):
        for table in source_schema.table_names:
            for ddl_field, json_field in zip(from_ddl.table(table), source_schema.table(table)):
                assert ddl_field.category == json_field.category, ddl_field.name
                assert ddl_field.is_primary_key == json_field.is_primary_key, ddl_field.name
                assert ddl_field.is_foreign_key == json_field.is_foreign_key, ddl_field.name
                assert ddl_field.references == json_field.references, ddl_field.name
                assert bool(ddl_field.comment) == bool(json_field.comment), ddl_field.name

    def test_enum_legend_survives_ddl_comments(self, from_ddl):
        rec_stat = next(f for f in from_ddl.table("emp_master") if f.name == "rec_stat")
        assert parse_code_legend(rec_stat.comment) == {
            "A": "Active",
            "I": "Inactive",
            "T": "Terminated",
        }


class TestDocumentInference:
    """Inferring the destination schema from sample documents."""

    @staticmethod
    @pytest.fixture(scope="class")
    def from_docs():
        return load_destination_file(SAMPLES / "people_platform.sample_docs.json")

    def test_same_collections_and_paths(self, from_docs, dest_schema):
        assert from_docs.collection_names == dest_schema.collection_names
        for collection in dest_schema.collection_names:
            assert from_docs.path_set(collection) == dest_schema.path_set(collection)

    def test_types_inferred_from_extended_json(self, from_docs):
        assert from_docs.lookup("employees", "_id").category == "objectid"
        assert from_docs.lookup("employees", "employment.startDate").category == "datetime"
        assert from_docs.lookup("employees", "employment.isRemote").category == "boolean"
        assert from_docs.lookup("employees", "compensation.baseSalary").category == "number"

    def test_nullable_fields_still_appear(self, from_docs):
        """A field that is null in one document and set in another must survive.

        Only merging every sample document produces the full path set; taking the
        first document alone would silently drop optional fields.
        """
        assert "employment.endDate" in from_docs.path_set("employees")


class TestFormatDetection:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("legacy_hrm.ddl.sql", "mysql_ddl"),
            ("tiny_crm.mysql.json", "mysql_json"),
            ("tiny_crm.mongo.json", "mongo_json"),
            ("people_platform.sample_docs.json", "mongo_documents"),
        ],
    )
    def test_detects_each_supported_format(self, path, expected):
        text = (SAMPLES / path).read_text(encoding="utf-8")
        assert detect_format(text) == expected

    def test_unparseable_input_raises_clearly(self):
        with pytest.raises(SchemaParseError):
            detect_format("this is not a schema in any supported format")


class TestFingerprints:
    def test_stable_and_distinct(self, source_schema, dest_schema):
        assert source_schema.fingerprint() == source_schema.fingerprint()
        assert source_schema.fingerprint() != dest_schema.fingerprint()
        assert len(source_schema.fingerprint()) >= 8

    def test_ddl_and_json_share_a_fingerprint(self, source_schema):
        """Same schema, different notation, same identity."""
        assert load_source_file(SAMPLES / "legacy_hrm.ddl.sql").fingerprint() == (
            source_schema.fingerprint()
        )


class TestTypeHelpers:
    @pytest.mark.parametrize(
        "sql,base,args",
        [
            ("VARCHAR(80)", "VARCHAR", ["80"]),
            ("DECIMAL(12,2)", "DECIMAL", ["12", "2"]),
            ("TINYINT(1)", "TINYINT", ["1"]),
            ("DATETIME", "DATETIME", []),
        ],
    )
    def test_parse_sql_type(self, sql, base, args):
        assert parse_sql_type(sql) == (base, args)

    @pytest.mark.parametrize(
        "sql,category",
        [
            ("INT", "integer"),
            ("BIGINT", "integer"),
            ("TINYINT(1)", "boolean"),
            ("DECIMAL(12,2)", "decimal"),
            ("VARCHAR(10)", "string"),
            ("CHAR(2)", "string"),
            ("DATE", "date"),
            ("DATETIME", "datetime"),
            ("TEXT", "string"),
        ],
    )
    def test_sql_category(self, sql, category):
        assert sql_category(sql) == category

    @pytest.mark.parametrize(
        "bson,category",
        [
            ("ObjectId", "objectid"),
            ("String", "string"),
            ("Number", "number"),
            ("Boolean", "boolean"),
            ("ISODate", "datetime"),
            ("Object", "object"),
        ],
    )
    def test_bson_category(self, bson, category):
        assert bson_category(bson) == category
