"""Shared fixtures. Every test here runs offline and costs nothing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from schema_mapper.config import (  # noqa: E402
    DEFAULT_DESTINATION_SCHEMA,
    DEFAULT_SOURCE_SCHEMA,
)
from schema_mapper.knowledge import load_knowledge  # noqa: E402
from schema_mapper.models import MappingDocument  # noqa: E402
from schema_mapper.normalize import load_destination_file, load_source_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SAMPLES = ROOT / "data" / "samples"
ARTIFACTS = ROOT / "outputs"
MAPPING_ARTIFACT = ARTIFACTS / "mapping_legacy_hrm_to_people_platform.json"

_REGENERATE = (
    "Run `bash scripts/dev.sh offline` to regenerate it from the committed "
    "cassettes (no AWS credentials required)."
)


def _load_artifact(path: Path) -> dict:
    if not path.is_file():
        pytest.fail(f"Missing committed artifact {path.name}. {_REGENERATE}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def source_schema():
    return load_source_file(DEFAULT_SOURCE_SCHEMA)


@pytest.fixture(scope="session")
def dest_schema():
    return load_destination_file(DEFAULT_DESTINATION_SCHEMA)


@pytest.fixture(scope="session")
def knowledge():
    return load_knowledge()


@pytest.fixture(scope="session")
def oracle() -> dict:
    return json.loads((FIXTURES / "expected_mapping.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def routing(oracle) -> dict[str, str]:
    return {t["source_table"]: t["destination_collection"] for t in oracle["tables"]}


@pytest.fixture(scope="session")
def mapping_json() -> dict:
    return _load_artifact(MAPPING_ARTIFACT)


@pytest.fixture(scope="session")
def mapping_document(mapping_json) -> MappingDocument:
    """The committed artifact, parsed through the strict contract models.

    Parsing is itself the first assertion: if the artifact violates the contract,
    every test that depends on it fails loudly here.
    """
    return MappingDocument.model_validate(mapping_json)


@pytest.fixture(scope="session")
def prompt_trace() -> list[dict]:
    path = ARTIFACTS / "prompt_trace.json"
    if not path.is_file():
        pytest.fail(f"Missing committed prompt trace. {_REGENERATE}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def run_report() -> dict:
    return _load_artifact(ARTIFACTS / "run_report.json")


@pytest.fixture(scope="session")
def tiny_source():
    return load_source_file(SAMPLES / "tiny_crm.mysql.json")


@pytest.fixture(scope="session")
def tiny_dest():
    return load_destination_file(SAMPLES / "tiny_crm.mongo.json")


@pytest.fixture
def field_of(source_schema):
    """Look up a source field by table and name."""

    def lookup(table: str, name: str):
        match = next((f for f in source_schema.table(table) if f.name == name), None)
        assert match is not None, f"no such source field {table}.{name}"
        return match

    return lookup


@pytest.fixture
def dest_of(dest_schema):
    """Look up a destination leaf by collection and dot path."""

    def lookup(collection: str, path: str):
        match = dest_schema.lookup(collection, path)
        assert match is not None, f"no such destination path {collection}.{path}"
        return match

    return lookup
