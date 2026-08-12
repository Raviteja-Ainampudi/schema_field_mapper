"""The mismatched-pair guard.

A run of `legacy_library` against `people_platform` finished happily and paired
`bk_master -> employees`, because every stage did its job: routing must pick some
collection and shortlisting must offer some candidates. Nothing in the pipeline
was positioned to say "these two files are not a pair".

Thresholds here are calibrated by `scripts/eval_pairing.py` over all combinations
of the four bundled pairs; these tests pin the behaviour that calibration bought,
including that the margin is narrow enough to warn rather than block.
"""

from __future__ import annotations

import pytest

from schema_mapper.config import DATA_DIR
from schema_mapper.normalize import load_destination, load_source
from schema_mapper.pairing import (
    TABLE_ALIGNED_AT,
    assess_pair,
    field_affinity,
)

SAMPLES = DATA_DIR / "samples"


def load_pair(source_name: str, dest_name: str):
    source = load_source((SAMPLES / source_name).read_text(encoding="utf-8"))
    destination = load_destination((SAMPLES / dest_name).read_text(encoding="utf-8"))
    return source, destination


@pytest.fixture(scope="module")
def library():
    return load_pair("library_legacy.ddl.sql", "library_platform.mongo.json")


@pytest.fixture(scope="module")
def school():
    return load_pair("school_sis.mysql.json", "school_platform.sample_docs.json")


class TestMatchingPairs:
    def test_bundled_assignment_pair_is_aligned(self, source_schema, dest_schema):
        assessment = assess_pair(source_schema, dest_schema)
        assert assessment.verdict == "aligned"
        assert assessment.ok

    def test_library_pair_is_aligned(self, library):
        assert assess_pair(*library).verdict == "aligned"

    def test_school_pair_is_aligned(self, school):
        assert assess_pair(*school).verdict == "aligned"

    def test_every_table_is_paired(self, library):
        assessment = assess_pair(*library)
        assert {p.table for p in assessment.pairings} == {"brnch", "bk_master", "mbr_info"}

    def test_matching_pair_routes_tables_correctly(self, library):
        routed = {p.table: p.collection for p in assess_pair(*library).pairings}
        assert routed == {"brnch": "branches", "bk_master": "books", "mbr_info": "members"}

    def test_aligned_detail_counts_placeable_columns(self, library):
        assessment = assess_pair(*library)
        assert f"of {assessment.total_fields} source columns" in assessment.detail


class TestCrossedPairs:
    """The exact mistake that prompted this: a library source, an HR destination."""

    @staticmethod
    @pytest.fixture(scope="class")
    def crossed(dest_schema):
        source = load_source(
            (SAMPLES / "library_legacy.ddl.sql").read_text(encoding="utf-8")
        )
        return assess_pair(source, dest_schema)

    def test_crossed_pair_is_not_aligned(self, crossed):
        assert crossed.verdict != "aligned"
        assert not crossed.ok

    def test_crossed_pair_scores_below_a_true_pair(self, crossed, library):
        assert crossed.score < assess_pair(*library).score

    def test_the_homeless_table_is_named(self, crossed):
        # bk_master is the table with genuinely nowhere to go in an HR schema.
        assert "bk_master" in crossed.detail

    def test_detail_says_a_run_will_still_finish(self, crossed):
        # The guard must not imply the run is impossible - it is not.
        assert "will finish" in crossed.detail

    def test_forced_tables_are_flagged_individually(self, crossed):
        forced = [p for p in crossed.pairings if p.affinity < TABLE_ALIGNED_AT]
        assert any(p.table == "bk_master" for p in forced)

    def test_genuinely_similar_table_is_not_flagged(self, crossed):
        # brnch -> locations is a fair match on its own merits: both are addresses.
        # Flagging it would train the reader to ignore the warning.
        branches = next(p for p in crossed.pairings if p.table == "brnch")
        assert branches.collection == "locations"
        assert branches.affinity >= TABLE_ALIGNED_AT

    def test_school_against_hr_is_not_aligned(self, school, dest_schema):
        assert assess_pair(school[0], dest_schema).verdict != "aligned"

    def test_hr_against_library_is_not_aligned(self, source_schema, library):
        assert assess_pair(source_schema, library[1]).verdict != "aligned"


class TestSeparation:
    def test_true_pairs_outscore_every_crossed_pair(self, source_schema, dest_schema, library):
        """The property the thresholds rest on, asserted rather than assumed."""
        sources = {
            "hrm": source_schema,
            "library": library[0],
        }
        destinations = {
            "hrm": dest_schema,
            "library": library[1],
        }
        true_scores, crossed_scores = [], []
        for src_key, source in sources.items():
            for dst_key, destination in destinations.items():
                score = assess_pair(source, destination).score
                (true_scores if src_key == dst_key else crossed_scores).append(score)
        assert min(true_scores) > max(crossed_scores)


class TestFieldAffinity:
    def test_identical_vocabulary_scores_higher_than_unrelated(self, knowledge, field_of, dest_of):
        email = field_of("emp_master", "work_email")
        good = dest_of("employees", "contact.email")
        bad = dest_of("employees", "compensation.baseSalary")
        assert field_affinity(email, good, knowledge) > field_affinity(email, bad, knowledge)

    def test_score_stays_in_range(self, knowledge, source_schema, dest_schema):
        for src in source_schema.table("emp_master"):
            for dest in dest_schema.collection("employees"):
                assert 0.0 <= field_affinity(src, dest, knowledge) <= 1.0
