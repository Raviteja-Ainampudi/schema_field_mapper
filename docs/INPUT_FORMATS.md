# Input formats

Four formats are accepted, two per side. The format is **detected from the content**, so
you paste or upload whatever you already have and never pick a type from a menu.

Limits: **200,000 characters** per side, extensions `.json`, `.sql`, `.txt`. The extension
only has to be readable text — detection reads the content, except that `.sql` is always
treated as DDL.

Validate anything before running it, free and without a model call:

```bash
curl -X POST localhost:8000/api/parse -H 'content-type: application/json' \
  -d '{"source_text": "CREATE TABLE t (id INT PRIMARY KEY);"}'
```

In the interface, this runs automatically as you type and the result appears under each
editor: `✓ MySQL CREATE TABLE · legacy_hrm · 3 tables · 34 columns`, or the exact parse
error. **Run pipeline** stays disabled while the input does not parse.

## Source side (MySQL)

### MySQL DDL

Detected by a `.sql` extension or a `CREATE TABLE` statement. Sample:
[`data/samples/legacy_hrm.ddl.sql`](../data/samples/legacy_hrm.ddl.sql).

```sql
CREATE TABLE emp_master (
  emp_id      INT AUTO_INCREMENT PRIMARY KEY,
  emp_cd      VARCHAR(20)  NOT NULL COMMENT 'Unique employee code',
  rec_stat    CHAR(1)      NOT NULL COMMENT 'A=Active, I=Inactive, T=Terminated',
  office_loc_id INT,
  CONSTRAINT fk_emp_loc FOREIGN KEY (office_loc_id) REFERENCES locations(loc_id)
);
```

Understood: column names and types, `NOT NULL`, `PRIMARY KEY` and `AUTO_INCREMENT`,
`FOREIGN KEY` targets (inline `REFERENCES` or a table-level constraint), and `COMMENT`
text. `USE db;` or `CREATE DATABASE db;` sets the database name; without one it is
reported as `source`.

### MySQL schema JSON

Detected by a top-level `tables` key. Sample:
[`data/samples/tiny_crm.mysql.json`](../data/samples/tiny_crm.mysql.json).

```json
{
  "database": "legacy_hrm",
  "type": "MySQL (Relational)",
  "tables": {
    "emp_master": {
      "columns": [
        { "name": "emp_id", "type": "INT", "key": "PK", "nullable": false },
        { "name": "rec_stat", "type": "CHAR(1)", "comment": "A=Active, I=Inactive" },
        { "name": "office_loc_id", "type": "INT", "references": "locations.loc_id" }
      ]
    }
  }
}
```

Per column, only `name` is required; `type` defaults to `VARCHAR(255)`. A primary key is
`"key": "PK"` or `"primary_key": true`, a foreign key is `"key": "FK"` or any
`"references"` value, and `nullable` defaults to false for a primary key and true
otherwise. `unique` and `comment` are optional. Keys beginning with `_` are ignored, so a
`_comment` note in your file is harmless.

A table may also be a bare list of those column objects, and a column may be written as a
**shorthand type string** — which is what you get by quoting the assignment's pseudo-JSON,
so pasting the assignment directly works:

```json
{
  "tables": {
    "emp_master": {
      "emp_id":        "INT PRIMARY KEY",
      "office_loc_id": "INT FK -> locations.loc_id",
      "rec_stat":      "CHAR(1) NOT NULL  -- A=Active, I=Inactive, T=Terminated"
    }
  }
}
```

The shorthand understands `PRIMARY KEY`, `UNIQUE`, `NOT NULL`, `REFERENCES table(col)`,
the `FK -> table.col` arrow, and a trailing `--` comment.

## Destination side (MongoDB)

### MongoDB schema JSON

Detected by a top-level `collections` key whose values are objects. Sample:
[`data/samples/tiny_crm.mongo.json`](../data/samples/tiny_crm.mongo.json).

```json
{
  "database": "people_platform",
  "collections": {
    "employees": {
      "fields": [
        { "name": "_id", "type": "ObjectId" },
        { "name": "employeeCode", "type": "String" },
        {
          "name": "fullName",
          "type": "Object",
          "fields": [
            { "name": "firstName", "type": "String" },
            { "name": "lastName", "type": "String" }
          ]
        },
        { "name": "managerId", "type": "ObjectId", "references": "employees._id" },
        { "name": "status", "type": "String", "comment": "active / inactive / terminated" }
      ]
    }
  }
}
```

A collection may also be written as a plain nested object, which is what you get by
quoting **the assignment's own Dataset B** — sub-documents inline, types as strings, and a
trailing `--` comment:

```json
{
  "collections": {
    "employees": {
      "_id": "ObjectId",
      "employeeCode": "String     -- unique human-readable ID",
      "fullName": { "firstName": "String", "lastName": "String" },
      "employment": {
        "status": "String        -- active / inactive / terminated",
        "managerId": "ObjectId   -- ref -> employees._id"
      }
    }
  }
}
```

Both forms produce identical leaf paths, which a test pins. The `--` comment is kept (it
is load-bearing for matching, see below) and a `ref -> collection.path` comment is read as
a reference.

One rule resolves the ambiguity between the two forms: a JSON object is treated as a
**field spec** if it carries any of `name`, `type`, `bson_type`, `fields`, `comment`, or
`references`, and as an **inline sub-document** otherwise. So
`{"amount": {"type": "Number"}}` is one field named `amount`, while
`{"fullName": {"firstName": "String"}}` is a sub-document with one leaf.

Nesting is flattened to dot-notation leaf paths during normalization, which is what makes
`fullName.firstName` a first-class mapping target rather than something a model has to
invent. **Container objects are never mapping targets**: `fullName` and `employment` do
not appear as a `destination_field` or in `unmapped_destination_fields`. That is why this
schema is 40 paths, not 40 plus its eight container names.

### MongoDB sample documents

Detected by a `collections` key whose values are *arrays*. This is `mongoexport` output,
so you can point it at a real database dump and skip writing a schema. Sample:
[`data/samples/people_platform.sample_docs.json`](../data/samples/people_platform.sample_docs.json).

```json
{
  "collections": {
    "employees": [
      {
        "_id": { "$oid": "66a1..." },
        "employeeCode": "E-1001",
        "fullName": { "firstName": "Ada", "lastName": "Lovelace" },
        "employment": { "startDate": { "$date": "2019-04-01T00:00:00Z" }, "isRemote": true }
      }
    ]
  }
}
```

Types are inferred from the values, and Extended JSON wrappers are read as their BSON
types (`$oid` → `ObjectId`, `$date` → `ISODate`, `$numberDecimal` → `Decimal128`).
Multiple documents per collection are merged, so a field absent from the first document is
still discovered. Give it several documents when fields are optional.

## Why comments matter

Column comments are frequently the only signal that connects a cryptic legacy name to a
readable destination path, so keep them in your DDL or JSON:

- `rec_stat CHAR(1) COMMENT 'A=Active, I=Inactive, T=Terminated'` reaches
  `employment.status` on comment agreement with the destination enum. The identifiers
  alone share nothing.
- `dept_stat` reaches `departments.isActive` **only** because its comment legend mentions
  "Active".

Comment-to-name similarity turned out to be the strongest non-obvious signal in the
deterministic scorer. Without it, both of those fields fall out of the shortlist entirely.

## Testing with your own schema

`data/samples/tiny_crm.mysql.json` and `tiny_crm.mongo.json` are a small unrelated pair
(`legacy_crm` → `revenue_platform`, 9 columns to 10 paths) kept precisely so you can check
that nothing is hardcoded to the HR schemas. Load one on each side and run.

Note that a schema pair with no recorded cassettes **cannot** run offline — replay is
keyed by request hash, so a new schema needs live Bedrock credentials.
