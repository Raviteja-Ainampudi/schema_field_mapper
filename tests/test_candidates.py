"""Stage 2: the accuracy ceiling.

Recall is the gate. A field whose true destination never enters its shortlist
cannot be mapped correctly by any model at any price, so this suite fails the
build rather than letting a silent quality regression through.
"""

from __future__ import annotations

import pytest

from schema_mapper.candidates import (
    key_role_score,
    lexical_score,
    retrieval_margin,
    score_pair,
    shortlist_field,
    shortlist_table,
    type_compatibility,
)
from schema_mapper.config import THRESHOLDS


@pytest.fixture(scope="module")
def shortlists(source_schema, dest_schema, knowledge, oracle, routing):
    """Every source field's shortlist, keyed table.field."""
    out = {}
    for table_spec in oracle["tables"]:
        table = table_spec["source_table"]
        dest_fields = dest_schema.collection(table_spec["destination_collection"])
        for src in source_schema.table(table):
            out[f"{table}.{src.name}"] = shortlist_field(
                src, dest_fields, knowledge, ref_collections=routing
            )
    return out


def _expected_pairs(oracle) -> list[tuple[str, str, str]]:
    return [
        (spec["source_table"], source_field, destination)
        for spec in oracle["tables"]
        for source_field, destination in spec["mappings"].items()
    ]


class TestRecall:
    def test_every_expected_destination_is_in_its_shortlist(self, shortlists, oracle):
        """100% recall@6. This is the gate; anything less is unwinnable downstream."""
        misses = []
        for table, source_field, expected in _expected_pairs(oracle):
            paths = [c.path for c in shortlists[f"{table}.{source_field}"]]
            if expected not in paths:
                misses.append(f"{table}.{source_field} -> {expected} (offered: {paths})")
        assert not misses, "shortlist recall below 100%:\n" + "\n".join(misses)

    def test_shortlists_are_small_enough_to_stay_cheap(self, shortlists):
        for key, candidates in shortlists.items():
            assert len(candidates) <= THRESHOLDS.top_k, key

    def test_rank_one_accuracy_stays_high(self, shortlists, oracle):
        """Ranking quality, held to a floor rather than perfection.

        Deciding between close candidates is Stage 3's job, so this is a
        regression guard on retrieval, not a correctness requirement.
        """
        pairs = _expected_pairs(oracle)
        hits = sum(
            1
            for table, source_field, expected in pairs
            if shortlists[f"{table}.{source_field}"]
            and shortlists[f"{table}.{source_field}"][0].path == expected
        )
        assert hits / len(pairs) >= 0.85, f"rank@1 dropped to {hits}/{len(pairs)}"

    def test_no_candidate_below_the_score_floor(self, shortlists):
        for key, candidates in shortlists.items():
            for candidate in candidates:
                assert candidate.score >= THRESHOLDS.min_candidate_score, key

    def test_candidates_are_ordered_best_first(self, shortlists):
        for key, candidates in shortlists.items():
            scores = [c.score for c in candidates]
            assert scores == sorted(scores, reverse=True), key


class TestUnmappableFieldsScoreLower:
    def test_dob_is_separable_from_every_real_mapping(self, shortlists, oracle):
        """The one genuinely unmappable column must not look like a match.

        Separation is what lets the adjudicating model answer null with support
        from retrieval instead of against it.
        """
        best_for_dob = shortlists["emp_master.dob"][0].score if shortlists["emp_master.dob"] else 0.0
        weakest_true = min(
            next(
                c.score
                for c in shortlists[f"{table}.{source_field}"]
                if c.path == expected
            )
            for table, source_field, expected in _expected_pairs(oracle)
        )
        assert best_for_dob < weakest_true, (
            f"dob's best candidate ({best_for_dob:.3f}) scores at or above the weakest true "
            f"mapping ({weakest_true:.3f}); the null decision is no longer supported by retrieval"
        )


class TestDeterminism:
    def test_same_inputs_give_identical_scores(
        self, source_schema, dest_schema, knowledge, routing
    ):
        """No randomness, so a rerun cannot silently change the shortlist."""
        src = source_schema.table("emp_master")[0]
        dest_fields = dest_schema.collection("employees")
        first = shortlist_field(src, dest_fields, knowledge, ref_collections=routing)
        second = shortlist_field(src, dest_fields, knowledge, ref_collections=routing)
        assert [(c.path, c.score) for c in first] == [(c.path, c.score) for c in second]

    def test_shortlist_table_covers_every_field(self, source_schema, dest_schema, knowledge):
        result = shortlist_table(
            source_schema.table("locations"), dest_schema.collection("locations"), knowledge
        )
        assert set(result) == {f.name for f in source_schema.table("locations")}


class TestScoringComponents:
    def test_type_compatibility_rewards_sensible_moves(self, field_of, dest_of):
        assert type_compatibility(
            field_of("emp_master", "hire_dt"), dest_of("employees", "employment.startDate")
        ) == 1.0
        assert type_compatibility(
            field_of("emp_master", "is_remote"), dest_of("employees", "employment.isRemote")
        ) == 1.0
        # A date column has no business in a boolean field.
        assert (
            type_compatibility(
                field_of("emp_master", "hire_dt"), dest_of("employees", "employment.isRemote")
            )
            < 0.3
        )

    def test_enum_code_can_target_string_or_boolean(self, field_of, dest_of):
        rec_stat = field_of("emp_master", "rec_stat")
        assert type_compatibility(rec_stat, dest_of("employees", "employment.status")) == 1.0
        # Lossy but legitimate, so plausible rather than perfect.
        boolean_score = type_compatibility(
            rec_stat, dest_of("employees", "employment.isRemote")
        )
        assert 0.5 < boolean_score < 1.0

    def test_plain_column_is_penalized_for_targeting_id(self, field_of, dest_of):
        """A non-key column claiming document identity is almost always wrong."""
        assert key_role_score(field_of("emp_master", "f_name"), dest_of("employees", "_id")) == 0.0
        assert key_role_score(field_of("emp_master", "emp_id"), dest_of("employees", "_id")) == 1.0

    def test_foreign_key_reference_match_is_the_strongest_signal(
        self, field_of, dest_of, routing
    ):
        """office_loc_id references locations, and so does location.locationId."""
        assert (
            key_role_score(
                field_of("emp_master", "office_loc_id"),
                dest_of("employees", "location.locationId"),
                routing,
            )
            == 1.0
        )

    def test_synonyms_rescue_pairs_with_no_shared_tokens(self, knowledge):
        """dept_stat and isActive share no identifier tokens at all.

        Without synonym expansion this pair scores zero lexically and the correct
        destination never reaches the shortlist.
        """
        plain = lexical_score(knowledge.tokenize("dept_stat"), knowledge.tokenize("isActive"))
        assert plain > 0.4

    def test_comment_agreement_contributes(self, field_of, dest_of, knowledge):
        """Both sides say ISO 4217, which is real evidence beyond the names."""
        breakdown = score_pair(
            field_of("emp_master", "sal_currency"),
            dest_of("employees", "compensation.currency"),
            knowledge,
        )
        assert breakdown.comment > 0.5


class TestRetrievalMargin:
    def test_decisive_win_scores_higher_than_a_narrow_one(
        self, source_schema, dest_schema, knowledge, routing
    ):
        src = next(f for f in source_schema.table("locations") if f.name == "city")
        candidates = shortlist_field(
            src, dest_schema.collection("locations"), knowledge, ref_collections=routing
        )
        top = candidates[0].path
        runner_up = candidates[1].path
        assert retrieval_margin(candidates, top) > retrieval_margin(candidates, runner_up)

    def test_unknown_or_missing_choice_scores_zero(self, source_schema, dest_schema, knowledge):
        src = source_schema.table("locations")[0]
        candidates = shortlist_field(src, dest_schema.collection("locations"), knowledge)
        assert retrieval_margin(candidates, None) == 0.0
        assert retrieval_margin(candidates, "not.a.path") == 0.0
        assert retrieval_margin([], "anything") == 0.0

    def test_margin_is_bounded(self, shortlists, oracle):
        for table, source_field, expected in _expected_pairs(oracle):
            margin = retrieval_margin(shortlists[f"{table}.{source_field}"], expected)
            assert 0.0 <= margin <= 1.0


class TestGenerality:
    def test_works_on_an_unrelated_schema(self, tiny_source, tiny_dest, knowledge):
        """Proves the scorer is not tuned to the HR schema specifically."""
        expected = {
            "cust_id": "_id",
            "cust_cd": "customerCode",
            "co_nm": "companyName",
            "signup_dt": "lifecycle.signedUpAt",
            "acct_stat": "lifecycle.status",
            "is_vip": "lifecycle.isVip",
            "mrr_amt": "revenue.monthlyRecurring",
            "amt_currency": "revenue.currency",
            "billing_email": "contact.billingEmail",
        }
        shortlists = shortlist_table(
            tiny_source.table("cust_master"),
            tiny_dest.collection("customers"),
            knowledge,
            ref_collections={"cust_master": "customers"},
        )
        misses = [
            f"{name} -> {want}"
            for name, want in expected.items()
            if want not in [c.path for c in shortlists[name]]
        ]
        assert not misses, f"unrelated-schema recall gaps: {misses}"
