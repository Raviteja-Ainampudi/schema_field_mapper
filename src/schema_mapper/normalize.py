"""Stage 0: parse both schemas into one intermediate representation. No LLM.

This stage does the single most important structural job in the pipeline:
MongoDB nesting is flattened to dot-notation paths up front, so
``fullName.firstName`` is a real candidate the model selects from a list rather
than a string it has to invent. The set of legal destination paths produced
here is later used as an allowlist, which is what makes a hallucinated path
impossible rather than merely unlikely.

Four input formats are accepted so the same pipeline works on a pasted
assignment snippet, a real dump, or a mongoexport sample:

* MySQL as structured JSON (``data/schemas/legacy_hrm.mysql.json``)
* MySQL as DDL (``data/samples/legacy_hrm.ddl.sql``)
* MongoDB as structured JSON (``data/schemas/people_platform.mongo.json``)
* MongoDB as sample documents in Extended JSON
  (``data/samples/people_platform.sample_docs.json``)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# Intermediate representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceField:
    table: str
    name: str
    sql_type: str
    base_type: str
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    is_unique: bool = False
    references: str | None = None
    comment: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def category(self) -> str:
        return sql_category(self.sql_type)

    def key_role(self) -> str:
        if self.is_primary_key:
            return "PK"
        if self.is_foreign_key:
            return "FK"
        if self.is_unique:
            return "UNIQUE"
        return ""

    def describe(self) -> str:
        """One compact line for prompts. Deliberately terse: prompt size is the
        constraint we are managing, so every character has to earn its place."""
        bits = [self.sql_type]
        if role := self.key_role():
            bits.append(role)
        if self.references:
            bits.append(f"-> {self.references}")
        if not self.nullable:
            bits.append("NOT NULL")
        if self.comment:
            bits.append(f"// {self.comment}")
        return f"{self.name} ({', '.join(bits)})"


@dataclass(frozen=True)
class DestField:
    collection: str
    path: str
    bson_type: str
    comment: str | None = None
    references: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.collection}.{self.path}"

    @property
    def leaf(self) -> str:
        return self.path.rsplit(".", 1)[-1]

    @property
    def parent(self) -> str:
        return self.path.rsplit(".", 1)[0] if "." in self.path else ""

    @property
    def is_ref(self) -> bool:
        return self.bson_type == "ObjectId" and self.path != "_id"

    @property
    def category(self) -> str:
        return bson_category(self.bson_type)

    def describe(self) -> str:
        bits = [self.bson_type]
        if self.references:
            bits.append(f"ref {self.references}")
        if self.comment:
            bits.append(f"// {self.comment}")
        return f"{self.path} ({', '.join(bits)})"


@dataclass
class SourceSchema:
    database: str
    dialect: str
    tables: dict[str, list[SourceField]] = field(default_factory=dict)

    def fields(self) -> Iterator[SourceField]:
        for columns in self.tables.values():
            yield from columns

    @property
    def field_count(self) -> int:
        return sum(len(cols) for cols in self.tables.values())

    @property
    def table_names(self) -> list[str]:
        return list(self.tables)

    def table(self, name: str) -> list[SourceField]:
        return self.tables[name]

    def fingerprint(self) -> str:
        return _fingerprint(
            [
                [f.table, f.name, f.sql_type, f.nullable, f.key_role(), f.references, f.comment]
                for f in self.fields()
            ]
        )


@dataclass
class DestinationSchema:
    database: str
    dialect: str
    collections: dict[str, list[DestField]] = field(default_factory=dict)

    def fields(self) -> Iterator[DestField]:
        for paths in self.collections.values():
            yield from paths

    @property
    def field_count(self) -> int:
        return sum(len(paths) for paths in self.collections.values())

    @property
    def collection_names(self) -> list[str]:
        return list(self.collections)

    def collection(self, name: str) -> list[DestField]:
        return self.collections[name]

    def path_set(self, collection: str) -> set[str]:
        """Legal destination paths for one collection: the hallucination allowlist."""
        return {f.path for f in self.collections.get(collection, [])}

    def lookup(self, collection: str, path: str) -> DestField | None:
        for f in self.collections.get(collection, []):
            if f.path == path:
                return f
        return None

    def fingerprint(self) -> str:
        return _fingerprint(
            [
                [f.collection, f.path, f.bson_type, f.references, f.comment]
                for f in self.fields()
            ]
        )


def _fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Type categories
# ---------------------------------------------------------------------------

_SQL_CATEGORIES: dict[str, str] = {
    "INT": "integer",
    "INTEGER": "integer",
    "BIGINT": "integer",
    "SMALLINT": "integer",
    "MEDIUMINT": "integer",
    "TINYINT": "integer",
    "DECIMAL": "decimal",
    "NUMERIC": "decimal",
    "FLOAT": "decimal",
    "DOUBLE": "decimal",
    "VARCHAR": "string",
    "CHAR": "string",
    "TEXT": "string",
    "TINYTEXT": "string",
    "MEDIUMTEXT": "string",
    "LONGTEXT": "string",
    "ENUM": "string",
    "DATE": "date",
    "DATETIME": "datetime",
    "TIMESTAMP": "datetime",
    "TIME": "string",
    "YEAR": "integer",
    "BOOL": "boolean",
    "BOOLEAN": "boolean",
    "JSON": "object",
    "BLOB": "binary",
}

_BSON_CATEGORIES: dict[str, str] = {
    "OBJECTID": "objectid",
    "STRING": "string",
    "NUMBER": "number",
    "INT32": "number",
    "INT64": "number",
    "DOUBLE": "number",
    "DECIMAL128": "number",
    "ISODATE": "datetime",
    "DATE": "datetime",
    "TIMESTAMP": "datetime",
    "BOOLEAN": "boolean",
    "BOOL": "boolean",
    "OBJECT": "object",
    "ARRAY": "array",
    "NULL": "unknown",
    "UNKNOWN": "unknown",
}


def parse_sql_type(sql_type: str) -> tuple[str, list[str]]:
    """``DECIMAL(12,2)`` -> ``("DECIMAL", ["12", "2"])``."""
    match = re.match(r"\s*([A-Za-z_]+)\s*(?:\(([^)]*)\))?", sql_type or "")
    if not match:
        return (sql_type or "").strip().upper(), []
    base = match.group(1).upper()
    args = [a.strip() for a in (match.group(2) or "").split(",") if a.strip()]
    return base, args


def sql_category(sql_type: str) -> str:
    """Map a MySQL type to a coarse category used for compatibility scoring.

    ``TINYINT(1)`` is special-cased to boolean: it is the MySQL boolean idiom and
    treating it as an integer would rank ``isRemote`` below numeric fields.
    """
    base, args = parse_sql_type(sql_type)
    if base == "TINYINT" and args and args[0] == "1":
        return "boolean"
    return _SQL_CATEGORIES.get(base, "string")


def bson_category(bson_type: str) -> str:
    return _BSON_CATEGORIES.get((bson_type or "").strip().upper(), "unknown")


def is_enum_code(fld: SourceField) -> bool:
    """A single-character code column carrying a legend in its comment.

    These are the fields that need a value-transform lookup rather than a cast,
    so they are detected structurally instead of by asking the model.
    """
    base, args = parse_sql_type(fld.sql_type)
    if base != "CHAR" or not args or args[0] != "1":
        return False
    return bool(fld.comment and "=" in fld.comment)


def parse_code_legend(comment: str | None) -> dict[str, str]:
    """``"A=Active, I=Inactive, T=Terminated"`` -> ``{"A": "Active", ...}``."""
    if not comment or "=" not in comment:
        return {}
    legend: dict[str, str] = {}
    for chunk in re.split(r"[,;/]", comment):
        if "=" not in chunk:
            continue
        code, _, label = chunk.partition("=")
        code, label = code.strip(), label.strip()
        if len(code) == 1 and label:
            legend[code] = label
    return legend


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(text: str, filename: str = "") -> str:
    lowered_name = filename.lower()
    if lowered_name.endswith(".sql"):
        return "mysql_ddl"
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        if "CREATE TABLE" in text.upper():
            return "mysql_ddl"
        raise SchemaParseError("Unrecognized schema format: expected JSON or MySQL DDL.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaParseError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if "tables" in payload:
        return "mysql_json"
    if "collections" in payload:
        values = [v for k, v in payload["collections"].items() if not k.startswith("_")]
        if values and isinstance(values[0], list):
            return "mongo_documents"
        return "mongo_json"
    raise SchemaParseError("JSON schema must contain a 'tables' or 'collections' key.")


class SchemaParseError(ValueError):
    """Raised with a human-actionable message; surfaced directly in the UI."""


# ---------------------------------------------------------------------------
# MySQL: structured JSON
# ---------------------------------------------------------------------------


def _column_from_mapping(table: str, spec: dict[str, Any]) -> SourceField:
    name = spec.get("name")
    if not name:
        raise SchemaParseError(f"Column in table '{table}' is missing 'name'.")
    sql_type = str(spec.get("type", "VARCHAR(255)"))
    base, _ = parse_sql_type(sql_type)
    key = str(spec.get("key", "")).upper()
    references = spec.get("references")
    is_pk = key == "PK" or bool(spec.get("primary_key"))
    is_fk = key == "FK" or bool(references)
    return SourceField(
        table=table,
        name=str(name),
        sql_type=sql_type,
        base_type=base,
        nullable=bool(spec.get("nullable", not is_pk)),
        is_primary_key=is_pk,
        is_foreign_key=is_fk and not is_pk,
        is_unique=bool(spec.get("unique", False)),
        references=str(references) if references else None,
        comment=(str(spec["comment"]) if spec.get("comment") else None),
    )


_INLINE_ATTRS = re.compile(
    r"""(?P<pk>\bPRIMARY\s+KEY\b)
      | (?P<unique>\bUNIQUE\b)
      | (?P<notnull>\bNOT\s+NULL\b)
      | (?P<ref>\bREFERENCES\s+`?(?P<reftable>\w+)`?\s*\(\s*`?(?P<refcol>\w+)`?\s*\))
      | (?P<fkarrow>\bFK\s*->\s*(?P<fktarget>[\w.]+))
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _column_from_type_string(table: str, name: str, spec: str) -> SourceField:
    """Parse the shorthand form ``"INT PRIMARY KEY  -- comment"``.

    This is what you get by quoting the assignment's pseudo-JSON, which makes
    pasting the assignment straight into the UI work.
    """
    body, _, comment = spec.partition("--")
    body = body.strip()
    comment = comment.strip() or None

    type_match = re.match(r"([A-Za-z_]+\s*(?:\([^)]*\))?)", body)
    sql_type = (type_match.group(1).strip() if type_match else "VARCHAR(255)")
    rest = body[len(type_match.group(1)) :] if type_match else body

    is_pk = is_unique = is_fk = False
    nullable = True
    references: str | None = None
    for m in _INLINE_ATTRS.finditer(rest):
        if m.group("pk"):
            is_pk, nullable = True, False
        elif m.group("unique"):
            is_unique = True
        elif m.group("notnull"):
            nullable = False
        elif m.group("ref"):
            is_fk = True
            references = f"{m.group('reftable')}.{m.group('refcol')}"
        elif m.group("fkarrow"):
            is_fk = True
            references = m.group("fktarget")

    base, _ = parse_sql_type(sql_type)
    return SourceField(
        table=table,
        name=name,
        sql_type=sql_type,
        base_type=base,
        nullable=nullable,
        is_primary_key=is_pk,
        is_foreign_key=is_fk and not is_pk,
        is_unique=is_unique,
        references=references,
        comment=comment,
    )


def parse_mysql_json(payload: dict[str, Any]) -> SourceSchema:
    schema = SourceSchema(
        database=str(payload.get("database", "source")),
        dialect=str(payload.get("type", "MySQL (Relational)")),
    )
    for table, spec in payload.get("tables", {}).items():
        if table.startswith("_"):
            continue
        columns: list[SourceField] = []
        if isinstance(spec, dict) and "columns" in spec:
            for col in spec["columns"]:
                columns.append(_column_from_mapping(table, col))
        elif isinstance(spec, dict):
            for name, col_spec in spec.items():
                if name.startswith("_"):
                    continue
                if isinstance(col_spec, dict):
                    columns.append(_column_from_mapping(table, {"name": name, **col_spec}))
                else:
                    columns.append(_column_from_type_string(table, name, str(col_spec)))
        elif isinstance(spec, list):
            for col in spec:
                columns.append(_column_from_mapping(table, col))
        else:
            raise SchemaParseError(f"Table '{table}' must be an object or list of columns.")
        if not columns:
            raise SchemaParseError(f"Table '{table}' has no columns.")
        schema.tables[table] = columns
    if not schema.tables:
        raise SchemaParseError("Source schema contains no tables.")
    return schema


# ---------------------------------------------------------------------------
# MySQL: DDL
# ---------------------------------------------------------------------------

_CONSTRAINT_PREFIXES = (
    "PRIMARY KEY",
    "FOREIGN KEY",
    "UNIQUE KEY",
    "UNIQUE INDEX",
    "UNIQUE",
    "KEY",
    "INDEX",
    "CONSTRAINT",
    "CHECK",
    "FULLTEXT",
    "SPATIAL",
)


def _strip_sql_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    out: list[str] = []
    for line in text.splitlines():
        cleaned: list[str] = []
        quote: str | None = None
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                cleaned.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "'\"`":
                quote = ch
                cleaned.append(ch)
            elif ch == "-" and line[i : i + 2] == "--":
                break
            elif ch == "#":
                break
            else:
                cleaned.append(ch)
            i += 1
        out.append("".join(cleaned))
    return "\n".join(out)


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def parse_mysql_ddl(text: str) -> SourceSchema:
    cleaned = _strip_sql_comments(text)
    schema = SourceSchema(database="source", dialect="MySQL (Relational)")

    db_match = re.search(r"(?:CREATE|USE)\s+(?:DATABASE|SCHEMA)?\s*`?(\w+)`?\s*;", cleaned, re.I)
    if db_match:
        schema.database = db_match.group(1)

    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(?:(\w+)`?\.`?)?(\w+)`?\s*\(",
        cleaned,
        re.I,
    ):
        db_prefix, table = match.group(1), match.group(2)
        if db_prefix and schema.database == "source":
            schema.database = db_prefix
        start = match.end()
        depth = 1
        i = start
        while i < len(cleaned) and depth:
            if cleaned[i] == "(":
                depth += 1
            elif cleaned[i] == ")":
                depth -= 1
            i += 1
        body = cleaned[start : i - 1]

        columns: dict[str, SourceField] = {}
        constraints: list[str] = []
        for part in _split_top_level(body):
            if part.upper().startswith(_CONSTRAINT_PREFIXES):
                constraints.append(part)
                continue
            col_match = re.match(r"`?(\w+)`?\s+(.*)", part, re.DOTALL)
            if not col_match:
                continue
            name, remainder = col_match.group(1), col_match.group(2)
            comment_match = re.search(r"COMMENT\s+'((?:[^']|'')*)'", remainder, re.I)
            comment = comment_match.group(1).replace("''", "'") if comment_match else None
            without_comment = remainder[: comment_match.start()] if comment_match else remainder
            fld = _column_from_type_string(table, name, without_comment)
            columns[name] = SourceField(**{**fld.__dict__, "comment": comment})

        for constraint in constraints:
            upper = constraint.upper()
            if upper.startswith("PRIMARY KEY"):
                for col in re.findall(r"`?(\w+)`?", constraint[len("PRIMARY KEY") :]):
                    if col in columns:
                        columns[col] = SourceField(
                            **{
                                **columns[col].__dict__,
                                "is_primary_key": True,
                                "is_foreign_key": False,
                                "nullable": False,
                            }
                        )
            elif "FOREIGN KEY" in upper:
                fk = re.search(
                    r"FOREIGN\s+KEY\s*\(\s*`?(\w+)`?\s*\)\s*REFERENCES\s+`?(\w+)`?\s*\(\s*`?(\w+)`?\s*\)",
                    constraint,
                    re.I,
                )
                if fk and fk.group(1) in columns:
                    col = fk.group(1)
                    columns[col] = SourceField(
                        **{
                            **columns[col].__dict__,
                            "is_foreign_key": not columns[col].is_primary_key,
                            "references": f"{fk.group(2)}.{fk.group(3)}",
                        }
                    )
            elif upper.startswith(("UNIQUE KEY", "UNIQUE INDEX", "UNIQUE")):
                tail = constraint[constraint.upper().index("(") :] if "(" in constraint else ""
                for col in re.findall(r"`?(\w+)`?", tail):
                    if col in columns:
                        columns[col] = SourceField(**{**columns[col].__dict__, "is_unique": True})

        if columns:
            schema.tables[table] = list(columns.values())

    if not schema.tables:
        raise SchemaParseError("No CREATE TABLE statements found in DDL input.")
    return schema


# ---------------------------------------------------------------------------
# MongoDB: structured JSON
# ---------------------------------------------------------------------------


# Telling a field spec from an inline sub-document, in the name-keyed form.
#
# "name" is deliberately not sufficient on its own: in the keyed form the outer
# key already supplies the field name, so an inner "name" is redundant there -
# while `name` is an extremely common leaf name (`author.name`). Reading it as a
# spec key silently collapsed `{"author": {"name": "String"}}` into one leaf
# called `author`.
#
# Requiring *every* key to be a spec key also keeps a sub-document that happens
# to contain a `type` or `comment` child intact, since its other children are
# not spec keys.
_SPEC_ONLY_KEYS = {"type", "bson_type", "fields", "comment", "references"}
_SPEC_KEYS = _SPEC_ONLY_KEYS | {"name"}


def _is_field_spec(spec: dict[str, Any]) -> bool:
    keys = set(spec)
    return bool(keys) and keys <= _SPEC_KEYS and bool(keys & _SPEC_ONLY_KEYS)

# "ObjectId  -- ref -> employees._id" in the terse form.
_DEST_REF = re.compile(r"\bref(?:erence[sd]?)?\s*(?:->|to)\s*([\w.]+)", re.IGNORECASE)


def _dest_spec_from_string(spec: str) -> dict[str, Any]:
    """Parse ``"ObjectId  -- ref -> employees._id"`` into a field spec.

    This is the destination-side counterpart of the source shorthand, and it is
    what you get by quoting the assignment's Dataset B pseudo-JSON. The trailing
    comment is worth keeping rather than discarding: comment text is the single
    strongest signal in candidate scoring.
    """
    body, _, comment = spec.partition("--")
    comment = comment.strip() or None
    out: dict[str, Any] = {"type": body.strip() or "String"}
    if comment:
        out["comment"] = comment
        ref = _DEST_REF.search(comment)
        if ref:
            out["references"] = ref.group(1)
    return out


def _iter_field_specs(container: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    """Accept either an ordered list of ``{"name": ...}`` or a name-keyed object."""
    if isinstance(container, list):
        for item in container:
            if not isinstance(item, dict) or "name" not in item:
                raise SchemaParseError("Each destination field needs a 'name'.")
            yield str(item["name"]), item
    elif isinstance(container, dict):
        for name, spec in container.items():
            if name.startswith("_comment"):
                continue
            if isinstance(spec, dict):
                # Otherwise it is a nested sub-document written inline - the
                # terse form of the assignment's own schema - and its children
                # are the fields. Without this, `"fullName": {"firstName": ...}`
                # parses as one leaf named fullName and both real paths vanish.
                if _is_field_spec(spec):
                    yield name, spec
                else:
                    yield name, {"type": "Object", "fields": spec}
            else:
                yield name, _dest_spec_from_string(str(spec))
    else:
        raise SchemaParseError("Destination fields must be a list or object.")


def _flatten_dest(
    collection: str, container: Any, prefix: str = ""
) -> list[DestField]:
    """Depth-first flatten to dot paths, keeping only leaves.

    Container objects (``fullName``, ``employment``) are intentionally dropped:
    they are not assignable destinations, and including them would inflate the
    destination field count and pollute ``unmapped_destination_fields``.
    """
    out: list[DestField] = []
    for name, spec in _iter_field_specs(container):
        path = f"{prefix}{name}"
        nested = spec.get("fields")
        declared = str(spec.get("type", "")).strip()
        if nested is not None:
            out.extend(_flatten_dest(collection, nested, prefix=f"{path}."))
            continue
        if declared.lower() in {"object", "document"} and not nested:
            continue
        out.append(
            DestField(
                collection=collection,
                path=path,
                bson_type=declared or "String",
                comment=(str(spec["comment"]) if spec.get("comment") else None),
                references=(str(spec["references"]) if spec.get("references") else None),
            )
        )
    return out


def parse_mongo_json(payload: dict[str, Any]) -> DestinationSchema:
    schema = DestinationSchema(
        database=str(payload.get("database", "destination")),
        dialect=str(payload.get("type", "MongoDB (Document)")),
    )
    for collection, spec in payload.get("collections", {}).items():
        if collection.startswith("_"):
            continue
        container = spec.get("fields", spec) if isinstance(spec, dict) else spec
        paths = _flatten_dest(collection, container)
        if not paths:
            raise SchemaParseError(f"Collection '{collection}' has no fields.")
        schema.collections[collection] = paths
    if not schema.collections:
        raise SchemaParseError("Destination schema contains no collections.")
    return schema


# ---------------------------------------------------------------------------
# MongoDB: inferred from sample documents
# ---------------------------------------------------------------------------


def _infer_bson_type(value: Any) -> str:
    if isinstance(value, dict):
        if "$oid" in value:
            return "ObjectId"
        if "$date" in value:
            return "ISODate"
        if "$numberDecimal" in value:
            return "Decimal128"
        if "$numberLong" in value or "$numberInt" in value:
            return "Number"
        return "Object"
    if isinstance(value, bool):  # must precede int: bool is a subclass of int
        return "Boolean"
    if isinstance(value, (int, float)):
        return "Number"
    if isinstance(value, str):
        return "String"
    if isinstance(value, list):
        return "Array"
    return "Null"


def _merge_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Union keys across documents, preferring the first non-null type seen.

    Needed because a nullable field (``employment.endDate``) is untyped in a
    document where it is null, but typed in the next one.
    """
    merged: dict[str, Any] = {}
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        for key, value in doc.items():
            if key.startswith("_comment"):
                continue
            existing = merged.get(key)
            if isinstance(value, dict) and not _is_extended(value):
                # A real sub-document: recurse, folding in whatever was already
                # merged for this key so sibling keys across documents survive.
                previous = [existing] if isinstance(existing, dict) and not _is_extended(existing) else []
                merged[key] = _merge_documents([*previous, value])
            elif key not in merged or merged[key] is None:
                merged[key] = value
    return merged


def _is_extended(value: dict[str, Any]) -> bool:
    return any(k.startswith("$") for k in value)


def _dest_from_merged(
    collection: str, merged: dict[str, Any], prefix: str = ""
) -> list[DestField]:
    out: list[DestField] = []
    for key, value in merged.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and not _is_extended(value):
            out.extend(_dest_from_merged(collection, value, prefix=f"{path}."))
            continue
        out.append(DestField(collection=collection, path=path, bson_type=_infer_bson_type(value)))
    return out


def parse_mongo_documents(payload: dict[str, Any]) -> DestinationSchema:
    schema = DestinationSchema(
        database=str(payload.get("database", "destination")),
        dialect="MongoDB (Document)",
    )
    for collection, docs in payload.get("collections", {}).items():
        if collection.startswith("_"):
            continue
        if not isinstance(docs, list) or not docs:
            raise SchemaParseError(f"Collection '{collection}' needs a non-empty document list.")
        merged = _merge_documents(docs)
        paths = _dest_from_merged(collection, merged)
        unknown = [p.path for p in paths if p.bson_type == "Null"]
        if unknown:
            raise SchemaParseError(
                f"Cannot infer types for {collection}: {', '.join(unknown)} are null in every "
                "sample document. Add a document where they carry a value."
            )
        schema.collections[collection] = paths
    if not schema.collections:
        raise SchemaParseError("Destination schema contains no collections.")
    return schema


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def load_source(text: str, filename: str = "") -> SourceSchema:
    fmt = detect_format(text, filename)
    if fmt == "mysql_ddl":
        return parse_mysql_ddl(text)
    if fmt == "mysql_json":
        return parse_mysql_json(json.loads(text))
    raise SchemaParseError(f"Input looks like '{fmt}', which is not a MySQL source schema.")


def load_destination(text: str, filename: str = "") -> DestinationSchema:
    fmt = detect_format(text, filename)
    if fmt == "mongo_json":
        return parse_mongo_json(json.loads(text))
    if fmt == "mongo_documents":
        return parse_mongo_documents(json.loads(text))
    raise SchemaParseError(f"Input looks like '{fmt}', which is not a MongoDB destination schema.")


def load_source_file(path: str | Path) -> SourceSchema:
    p = Path(path)
    return load_source(p.read_text(encoding="utf-8"), p.name)


def load_destination_file(path: str | Path) -> DestinationSchema:
    p = Path(path)
    return load_destination(p.read_text(encoding="utf-8"), p.name)
