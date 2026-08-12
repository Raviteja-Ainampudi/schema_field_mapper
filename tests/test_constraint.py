"""The assignment's hard constraint, machine-checked.

    "You cannot pass both entire schemas to an LLM in a single prompt and expect
     a complete mapping."

These assertions run against `outputs/prompt_trace.json`, the recorded text of
every request the committed artifact was produced by. They check the prompts that
were actually sent, not the code that intended to send them, and they check the
text as well as the manifests so a bookkeeping bug cannot make the proof pass.
"""

from __future__ import annotations

import re

from schema_mapper.config import THRESHOLDS

# Stages that see typed field descriptors. Routing is excluded because it sees
# bare column names, which is a strictly weaker exposure.
TYPED_STAGES = {"adjudicate", "reflect", "tiebreak"}


def _manifest(entry: dict) -> dict:
    return entry.get("manifest") or {}


class TestNoPromptSeesBothSchemas:
    def test_trace_is_present_and_non_trivial(self, prompt_trace):
        assert len(prompt_trace) >= 4, "a decomposed pipeline should make several calls"
        assert any(e["stage"] == "route" for e in prompt_trace)
        assert any(e["stage"] == "adjudicate" for e in prompt_trace)

    def test_no_prompt_contains_every_source_field_name(self, prompt_trace, source_schema):
        """Textual check: the full source schema never appears in one prompt."""
        all_names = [f.name for f in source_schema.fields()]
        for entry in prompt_trace:
            text = f"{entry['system']}\n{entry['user']}"
            present = [n for n in all_names if re.search(rf"\b{re.escape(n)}\b", text)]
            assert len(present) < len(all_names), (
                f"stage '{entry['stage']}' mentioned all {len(all_names)} source fields"
            )

    def test_no_prompt_contains_every_destination_path(self, prompt_trace, dest_schema):
        all_paths = [f.path for f in dest_schema.fields()]
        for entry in prompt_trace:
            text = f"{entry['system']}\n{entry['user']}"
            present = [p for p in all_paths if p in text]
            assert len(present) < len(all_paths), (
                f"stage '{entry['stage']}' mentioned all {len(all_paths)} destination paths"
            )

    def test_no_prompt_carries_more_than_one_source_table(self, prompt_trace):
        """Each call reasons about one table, so no prompt spans the source schema."""
        for entry in prompt_trace:
            count = _manifest(entry).get("source_table_count", 0)
            assert count <= 1, f"stage '{entry['stage']}' carried {count} source tables"

    def test_typed_field_exposure_is_bounded_by_batch_size(self, prompt_trace):
        for entry in prompt_trace:
            if entry["stage"] not in TYPED_STAGES:
                continue
            count = _manifest(entry).get("source_field_count", 0)
            assert count <= THRESHOLDS.batch_size, (
                f"stage '{entry['stage']}' carried {count} typed fields, "
                f"above the {THRESHOLDS.batch_size} cap"
            )

    def test_routing_prompts_carry_names_without_types(self, prompt_trace):
        """Routing sees column names only; a name list is not a mappable schema."""
        route_entries = [e for e in prompt_trace if e["stage"] == "route"]
        assert route_entries
        for entry in route_entries:
            assert _manifest(entry).get("detail_level") == "names"
            for type_token in ("VARCHAR", "TINYINT", "DECIMAL", "ISODate", "ObjectId"):
                assert type_token not in entry["user"], (
                    f"routing prompt leaked type information: {type_token}"
                )

    def test_destination_exposure_is_a_shortlist_not_a_schema(self, prompt_trace, dest_schema):
        total = dest_schema.field_count
        for entry in prompt_trace:
            count = _manifest(entry).get("destination_path_count", 0)
            assert count < total, (
                f"stage '{entry['stage']}' saw all {total} destination paths"
            )


class TestNoSingleCallProducesTheMapping:
    def test_no_response_contains_the_whole_mapping(self, prompt_trace, mapping_document):
        """The finished document is assembled by code from many partial answers."""
        total = mapping_document.mapped_field_count()
        for entry in prompt_trace:
            data = entry.get("response_data") or {}
            produced = len(data.get("mappings") or [])
            assert produced < total, (
                f"stage '{entry['stage']}' returned {produced} of {total} mappings in one call"
            )

    def test_batches_are_bounded(self, prompt_trace):
        for entry in prompt_trace:
            if entry["stage"] != "adjudicate":
                continue
            produced = len((entry.get("response_data") or {}).get("mappings") or [])
            assert produced <= THRESHOLDS.batch_size

    def test_mapping_is_the_product_of_several_calls(self, prompt_trace, mapping_document):
        adjudications = [e for e in prompt_trace if e["stage"] == "adjudicate"]
        assert len(adjudications) >= 3, (
            "a 34-field schema batched at 8 fields cannot be adjudicated in fewer than 3 calls"
        )
        assert mapping_document.mapped_field_count() > THRESHOLDS.batch_size

    def test_no_response_contains_type_transforms(self, prompt_trace):
        """Types are rendered deterministically, so the model never supplies them.

        Keeping type_transform out of the model's output is what makes an
        impossible pairing like "VARCHAR -> Number" unrepresentable.
        """
        for entry in prompt_trace:
            for mapping in (entry.get("response_data") or {}).get("mappings") or []:
                assert "type_transform" not in mapping


class TestReportedProofMatchesTheTrace:
    """The run report's constraint numbers must be derived, not asserted."""

    def test_counts_agree_with_the_trace(self, run_report, prompt_trace):
        constraint = run_report["constraint"]
        assert constraint["total_llm_calls"] == len(prompt_trace)
        assert constraint["prompts_containing_both_full_schemas"] == 0
        assert constraint["max_source_tables_in_one_prompt"] <= 1
        assert (
            constraint["max_typed_source_fields_in_one_prompt"] <= THRESHOLDS.batch_size
        )

    def test_totals_are_the_real_schema_sizes(self, run_report, source_schema, dest_schema):
        constraint = run_report["constraint"]
        assert constraint["total_source_fields"] == source_schema.field_count
        assert constraint["total_destination_paths"] == dest_schema.field_count

    def test_partial_exposure_is_strictly_partial(self, run_report):
        constraint = run_report["constraint"]
        assert (
            constraint["max_typed_source_fields_in_one_prompt"]
            < constraint["total_source_fields"]
        )
        assert (
            constraint["max_destination_paths_in_one_prompt"]
            < constraint["total_destination_paths"]
        )
        assert (
            constraint["max_mappings_from_one_call"]
            < run_report["coverage"]["source_fields_mapped"]
        )
