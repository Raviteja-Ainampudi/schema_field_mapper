"""What the interface shows before anyone presses Run.

Deployment-shaped tests: under Lambda the writable directory is `/tmp` and is
empty on every cold start, so anything that only looks there works perfectly in a
checkout and shows an empty page in production. These call the endpoint function
directly - no HTTP client dependency needed for a question about file lookup.
"""

from __future__ import annotations

import json

from api.main import latest_artifact

COMMITTED = "mapping_legacy_hrm_to_people_platform.json"


class TestLatestArtifact:
    def test_falls_back_to_the_bundled_copy_when_the_writable_dir_is_empty(
        self, tmp_path, monkeypatch
    ):
        """The Lambda cold-start case: /tmp has nothing in it yet."""
        monkeypatch.setenv("SCHEMA_MAPPER_OUTPUT_DIR", str(tmp_path))
        served = latest_artifact()
        assert served["source"] == COMMITTED
        assert served["mapping"] is not None
        assert served["report"] is not None, "the run report ships next to the mapping"

    def test_a_fresh_run_output_wins_over_the_bundled_copy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCHEMA_MAPPER_OUTPUT_DIR", str(tmp_path))
        (tmp_path / "mapping_fresh_to_run.json").write_text(
            json.dumps({"mapping_version": "test"}), encoding="utf-8"
        )
        served = latest_artifact()
        assert served["source"] == "mapping_fresh_to_run.json"
        assert served["mapping"]["mapping_version"] == "test"

    def test_the_committed_artifact_is_valid_json_both_ways(self, tmp_path, monkeypatch):
        """Guards against serving a half-written file to the first visitor."""
        monkeypatch.setenv("SCHEMA_MAPPER_OUTPUT_DIR", str(tmp_path))
        mapping = latest_artifact()["mapping"]
        assert mapping["mapping_version"]
        assert mapping["tables"], "an artifact with no tables would render an empty graph"
