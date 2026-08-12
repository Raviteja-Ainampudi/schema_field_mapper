"""Orchestrator behaviour, driven by scripted model responses.

Every guardrail is exercised by making the model misbehave on purpose:
inventing a path, claiming a path another column already owns, rambling for
three sentences, describing a transform that is not possible. The point is that
none of those reach the artifact, and that each one is reported rather than
quietly absorbed.
"""

from __future__ import annotations

import pytest

from schema_mapper.bedrock import CassetteMissing, CassetteStore, OfflineClient, ScriptedClient
from schema_mapper.config import Settings
from schema_mapper.pipeline import Pipeline
from schema_mapper.validate import check_coverage

CUSTOMER_FIELDS = [
    "cust_id",
    "cust_cd",
    "co_nm",
    "signup_dt",
    "acct_stat",
    "is_vip",
    "mrr_amt",
    "amt_currency",
    "billing_email",
]

GOOD_PAIRS = {
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


def entry(source_field: str, destination_field: str | None, confidence: float = 0.92, **over):
    payload = {
        "source_field": source_field,
        "destination_field": destination_field,
        "confidence": confidence,
        "reasoning": f"The {source_field} column corresponds to {destination_field}.",
        "notes": None,
    }
    payload.update(over)
    return payload


def route(collection: str = "customers"):
    return {
        "destination_collection": collection,
        "confidence": 0.95,
        "reasoning": "Both describe customers.",
    }


def batches(overrides: dict[str, dict] | None = None) -> list[dict]:
    """Two adjudication responses covering the 9 columns in batches of 8 and 1."""
    overrides = overrides or {}
    entries = []
    for name in CUSTOMER_FIELDS:
        if name in overrides:
            entries.append(overrides[name])
        else:
            entries.append(entry(name, GOOD_PAIRS[name]))
    return [{"mappings": entries[:8]}, {"mappings": entries[8:]}]


@pytest.fixture
def settings():
    """Cascade and reflection off, so scripted call order is exactly predictable."""
    return Settings(
        enable_cascade=False,
        enable_reflection=False,
        enable_cache=False,
        router_model="us.amazon.nova-lite-v1:0",
        mapper_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        cheap_mapper_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )


def run_pipeline(responses, tiny_source, tiny_dest, settings):
    client = ScriptedClient(responses)
    pipeline = Pipeline(
        client=client,
        source=tiny_source,
        destination=tiny_dest,
        settings=settings,
    )
    return pipeline, client, pipeline.run()


class TestHappyPath:
    @pytest.fixture
    def result(self, tiny_source, tiny_dest, settings):
        _, _, result = run_pipeline([route(), *batches()], tiny_source, tiny_dest, settings)
        return result

    def test_produces_a_valid_document(self, result):
        assert result.diagnostics.ok, result.diagnostics.as_dict()
        assert result.document.mapped_field_count() == 9

    def test_covers_every_source_field(self, result, tiny_source, tiny_dest):
        assert check_coverage(result.document, tiny_source, tiny_dest) == []
        assert result.document.accounted_source_fields() == 9

    def test_declares_the_destination_field_with_no_counterpart(self, result):
        """accountManagerId exists only in the destination and must be declared."""
        table = result.document.tables[0]
        assert table.unmapped_destination_fields == ["contact.accountManagerId"]

    def test_renders_type_transforms_deterministically(self, result):
        transforms = {m.source_field: m.type_transform for m in result.document.all_mappings}
        assert transforms["cust_id"] == "INT -> ObjectId"
        assert transforms["is_vip"] == "TINYINT(1) -> Boolean (nested path)"
        assert transforms["acct_stat"] == "CHAR(1) code -> String enum (nested path)"
        assert transforms["mrr_amt"] == "DECIMAL(10,2) -> Number (nested path)"

    def test_fills_notes_the_model_left_empty(self, result):
        """A silent cast would hide the code lookup a migration has to write."""
        notes = {m.source_field: m.notes for m in result.document.all_mappings}
        assert notes["acct_stat"] and "A -> active" in notes["acct_stat"]
        assert notes["is_vip"] == "Transform: 0 -> false, 1 -> true."
        assert notes["cust_cd"] is None

    def test_reports_cost_and_token_usage(self, result):
        cost = result.report["cost"]
        assert cost["billable_calls"] == 3
        assert cost["total_input_tokens"] > 0
        assert cost["total_usd"] > 0


class TestHallucinatedPaths:
    def test_retry_recovers_a_valid_path(self, tiny_source, tiny_dest, settings):
        bad = entry("co_nm", "lifecycle.companyNameThatDoesNotExist")
        responses = [
            route(),
            *batches({"co_nm": bad}),
            {"mappings": [entry("co_nm", "companyName")]},  # the scoped retry
        ]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        assert result.diagnostics.hallucinated_paths, "the invented path should be recorded"
        mapped = {m.source_field: m.destination_field for m in result.document.all_mappings}
        assert mapped["co_nm"] == "companyName"
        # A caught and repaired proposal is the guard working, not a failed run.
        assert result.diagnostics.unresolved_paths == []
        assert result.diagnostics.ok

    def test_persistent_invention_becomes_a_declared_gap(self, tiny_source, tiny_dest, settings):
        """Better an acknowledged gap than a fabricated destination."""
        bad = entry("co_nm", "not.a.real.path")
        responses = [
            route(),
            *batches({"co_nm": bad}),
            {"mappings": [entry("co_nm", "still.not.real")]},
        ]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        table = result.document.tables[0]
        assert "co_nm" in table.unmapped_source_fields
        assert "cust_master.co_nm" in result.diagnostics.forced_nulls
        assert all(m.destination_field != "not.a.real.path" for m in result.document.all_mappings)
        # Still a valid document: the field is accounted for, just not mapped.
        assert result.diagnostics.ok


class TestCollisions:
    def test_one_path_keeps_one_owner(self, tiny_source, tiny_dest, settings):
        """Two columns claim companyName; a document field can hold one value."""
        responses = [
            route(),
            *batches(
                {
                    "co_nm": entry("co_nm", "companyName", 0.95),
                    "cust_cd": entry("cust_cd", "companyName", 0.40),
                }
            ),
        ]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        owners = [
            m.source_field
            for m in result.document.all_mappings
            if m.destination_field == "companyName"
        ]
        assert owners == ["co_nm"], "the more confident column should keep the path"
        table = result.document.tables[0]
        assert "cust_cd" in table.unmapped_source_fields
        assert result.diagnostics.collisions[0]["destination_field"] == "companyName"
        assert result.diagnostics.collisions[0]["resolved"] is True

    def test_loser_reasoning_explains_the_contest(self, tiny_source, tiny_dest, settings):
        responses = [
            route(),
            *batches(
                {
                    "co_nm": entry("co_nm", "companyName", 0.95),
                    "cust_cd": entry("cust_cd", "companyName", 0.40),
                }
            ),
        ]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)
        decision = next(d for d in result.decisions if d.src.name == "cust_cd")
        assert decision.tie_broken
        assert "co_nm" in decision.reasoning


class TestReasoningRepair:
    def test_multi_sentence_reasoning_is_rewritten(self, tiny_source, tiny_dest, settings):
        rambling = entry(
            "cust_cd",
            "customerCode",
            reasoning="This is one sentence. This is a second. And a third.",
        )
        responses = [
            route(),
            *batches({"cust_cd": rambling}),
            {"reasoning": "The customer code maps to customerCode."},  # the rewrite call
        ]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        mapping = next(m for m in result.document.all_mappings if m.source_field == "cust_cd")
        assert mapping.reasoning == "The customer code maps to customerCode."
        assert "cust_master.cust_cd" in result.diagnostics.reasoning_repairs

    def test_falls_back_to_truncation_when_the_rewrite_also_fails(
        self, tiny_source, tiny_dest, settings
    ):
        """Repair must never fail a run; the deterministic path always succeeds."""
        rambling = entry(
            "cust_cd",
            "customerCode",
            reasoning="First sentence here. Second sentence here.",
        )
        responses = [
            route(),
            *batches({"cust_cd": rambling}),
            {"reasoning": "Still two sentences. Definitely two."},
        ]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        mapping = next(m for m in result.document.all_mappings if m.source_field == "cust_cd")
        assert mapping.reasoning == "First sentence here."
        assert result.diagnostics.ok


class TestNotesCorrection:
    def test_an_impossible_mechanism_is_replaced(self, tiny_source, tiny_dest, settings):
        """"Wrap the integer" is not how an ObjectId is produced."""
        wrong = entry(
            "cust_id",
            "_id",
            notes="Convert INT to ObjectId by wrapping the integer value.",
        )
        responses = [route(), *batches({"cust_id": wrong})]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        mapping = next(m for m in result.document.all_mappings if m.source_field == "cust_id")
        assert "wrapping" not in (mapping.notes or "")
        assert "legacy" in (mapping.notes or "").lower()
        assert len(result.diagnostics.notes_corrections) == 1
        assert result.diagnostics.notes_corrections[0]["source_field"] == "cust_master.cust_id"

    def test_a_sound_note_is_left_alone(self, tiny_source, tiny_dest, settings):
        good = entry(
            "cust_id",
            "_id",
            notes="MongoDB generates _id; retain the legacy cust_id for remapping.",
        )
        responses = [route(), *batches({"cust_id": good})]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        mapping = next(m for m in result.document.all_mappings if m.source_field == "cust_id")
        assert mapping.notes == "MongoDB generates _id; retain the legacy cust_id for remapping."
        assert result.diagnostics.notes_corrections == []


class TestNullDecisions:
    def test_a_declined_field_is_declared_unmapped(self, tiny_source, tiny_dest, settings):
        responses = [route(), *batches({"co_nm": entry("co_nm", None, 0.2)})]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        table = result.document.tables[0]
        assert "co_nm" in table.unmapped_source_fields
        assert result.document.mapped_field_count() == 8
        assert result.diagnostics.ok

    @pytest.mark.parametrize("sentinel", ["null", "none", "N/A", ""])
    def test_string_sentinels_are_treated_as_null(
        self, tiny_source, tiny_dest, settings, sentinel
    ):
        """Models say "no match" in many ways; all of them mean unmapped."""
        responses = [route(), *batches({"co_nm": entry("co_nm", sentinel, 0.2)})]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)
        assert "co_nm" in result.document.tables[0].unmapped_source_fields

    def test_a_missing_entry_does_not_lose_the_field(self, tiny_source, tiny_dest, settings):
        """If the model omits a field entirely, coverage must still account for it."""
        responses = [route(), {"mappings": []}, {"mappings": []}]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        assert result.document.accounted_source_fields() == 9
        assert set(result.document.tables[0].unmapped_source_fields) == set(CUSTOMER_FIELDS)
        assert result.diagnostics.ok


class TestRouting:
    def test_an_unknown_collection_falls_back_to_the_closest(
        self, tiny_source, tiny_dest, settings
    ):
        responses = [route("not_a_collection"), *batches()]
        _, _, result = run_pipeline(responses, tiny_source, tiny_dest, settings)

        assert result.document.tables[0].destination_collection == "customers"
        assert result.report["routing"]["conflicts"]


class TestConstraintBookkeeping:
    @pytest.fixture
    def scripted(self, tiny_source, tiny_dest, settings):
        _, client, result = run_pipeline([route(), *batches()], tiny_source, tiny_dest, settings)
        return client, result

    def test_every_request_carries_a_manifest(self, scripted):
        client, _ = scripted
        for request in client.requests:
            assert request.manifest is not None, request.stage
            assert request.manifest.stage

    def test_routing_sees_names_only(self, scripted):
        client, _ = scripted
        route_requests = [r for r in client.requests if r.stage == "route"]
        assert len(route_requests) == 1
        assert route_requests[0].manifest.detail_level == "names"
        assert "VARCHAR" not in route_requests[0].user

    def test_no_request_carries_more_than_one_table(self, scripted):
        client, _ = scripted
        for request in client.requests:
            assert len(request.manifest.source_tables) <= 1

    def test_adjudication_is_batched(self, scripted):
        client, _ = scripted
        adjudications = [r for r in client.requests if r.stage == "adjudicate"]
        assert len(adjudications) == 2
        for request in adjudications:
            assert request.manifest.source_field_count <= 8

    def test_report_constraint_section_is_derived_from_calls(self, scripted):
        _, result = scripted
        constraint = result.report["constraint"]
        assert constraint["total_llm_calls"] == 3
        assert constraint["max_source_tables_in_one_prompt"] == 1
        assert constraint["max_mappings_from_one_call"] == 8
        assert constraint["prompts_containing_both_full_schemas"] == 0


class TestOfflineReplay:
    def test_missing_cassette_fails_loudly(self, tiny_source, tiny_dest, settings, tmp_path):
        """Silent fallback to a live call would be a surprise AWS bill."""
        client = OfflineClient(cassettes=CassetteStore(tmp_path, record=False))
        pipeline = Pipeline(
            client=client, source=tiny_source, destination=tiny_dest, settings=settings
        )
        with pytest.raises(CassetteMissing, match="No cassette"):
            pipeline.run()

    def test_committed_cassettes_reproduce_the_artifact(
        self, source_schema, dest_schema, mapping_document
    ):
        """A reviewer with no AWS account gets the same mapping we committed."""
        pipeline = Pipeline(
            client=OfflineClient(),
            source=source_schema,
            destination=dest_schema,
            settings=Settings(enable_cache=False),
        )
        result = pipeline.run()

        replayed = {
            (t.source_table, m.source_field): m.destination_field
            for t in result.document.tables
            for m in t.field_mappings
        }
        committed = {
            (t.source_table, m.source_field): m.destination_field
            for t in mapping_document.tables
            for m in t.field_mappings
        }
        assert replayed == committed
        assert result.diagnostics.ok
        # Replay reports the recorded run's cost, flagged as not billed, rather
        # than a misleading $0 that would make the cost panel useless offline.
        assert result.report["cost"]["billed"] is False
        assert result.report["cost"]["total_usd"] > 0
