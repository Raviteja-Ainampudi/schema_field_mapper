"""The orchestrator: six stages, four of them with no LLM at all.

Stage 0 normalize (code) -> Stage 1 route (LLM, one call per source table) ->
Stage 2 shortlist (code) -> Stage 3 adjudicate (LLM, batched, cascaded) ->
Stage 3c reflect (LLM, only the weakest decisions) -> Stage 4 validate and
repair (code, with tiny scoped LLM calls for tie-breaks and format repair) ->
Stage 5 assemble (code).

The decomposition is the answer to the assignment's constraint: no single prompt
carries both schemas, and no single call produces the finished mapping. Each
request records a manifest of exactly what it was allowed to see, so that claim
is verified by ``tests/test_constraint.py`` rather than asserted here.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .bedrock import LLMClient, LLMRequest, PromptManifest
from .candidates import Candidate, retrieval_margin
from .config import (
    DESTINATION_LABEL,
    MAPPING_VERSION,
    SOURCE_LABEL,
    Settings,
    THRESHOLDS,
    spec_for,
)
from .knowledge import KnowledgePack, load_knowledge
from .models import MAX_REASONING_CHARS, FieldMapping, MappingDocument, TableMapping, now_iso
from .normalize import DestinationSchema, DestField, SourceField, SourceSchema
from .pairing import assess_pair
from .prompts import (
    ADJUDICATE_SYSTEM,
    ADJUDICATE_TOOL,
    REFLECT_SYSTEM,
    REFLECT_TOOL,
    REWRITE_SYSTEM,
    REWRITE_TOOL,
    ROUTE_SYSTEM,
    ROUTE_TOOL,
    TIEBREAK_SYSTEM,
    TIEBREAK_TOOL,
    adjudicate_prompt,
    reflect_prompt,
    rewrite_prompt,
    route_prompt,
    tiebreak_prompt,
)
from .tools import SchemaTools
from .transforms import notes_are_sound, render_type_transform, suggest_notes, transform_rule
from .validate import (
    Diagnostics,
    blend_confidence,
    check_coverage,
    find_collisions,
    is_no_match,
    needs_reasoning_repair,
    table_confidence,
    validate_contract,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[[dict[str, Any]], None]

# Transform rules whose value logic a migration must hand-write; a mapping that
# needs one is capped below "high confidence" no matter how obvious the pairing.
MANUAL_RULES = {"pk_to_objectid", "fk_to_objectid", "code_to_boolean"}


@dataclass
class Decision:
    """One field's journey through the pipeline, kept for the run report."""

    src: SourceField
    candidates: list[Candidate]
    path: str | None = None
    model_confidence: float = 0.0
    confidence: float = 0.0
    reasoning: str = ""
    notes: str | None = None
    decided_by: str = "cheap"
    passes: list[dict[str, Any]] = field(default_factory=list)
    knowledge_ids: list[str] = field(default_factory=list)
    repaired: bool = False
    tie_broken: bool = False
    forced_null: str | None = None

    @property
    def key(self) -> str:
        return f"{self.src.table}.{self.src.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_field": self.key,
            "destination_field": self.path,
            "confidence": self.confidence,
            "model_confidence": self.model_confidence,
            # Carried even though mapped fields also expose it through the
            # artifact: for a field that ends up unmapped there is no
            # field_mappings entry, so this is the only place the explanation for
            # *not* mapping it survives.
            "reasoning": self.reasoning,
            "notes": self.notes,
            "decided_by": self.decided_by,
            "repaired": self.repaired,
            "tie_broken": self.tie_broken,
            "forced_null": self.forced_null,
            "knowledge_snippets": self.knowledge_ids,
            "passes": self.passes,
            "candidates": [
                {"path": c.path, **c.scores.as_dict()} for c in self.candidates
            ],
        }


@dataclass
class RunResult:
    document: MappingDocument
    report: dict[str, Any]
    decisions: list[Decision]
    diagnostics: Diagnostics
    trace: list[dict[str, Any]]


class Pipeline:
    def __init__(
        self,
        client: LLMClient,
        source: SourceSchema,
        destination: DestinationSchema,
        settings: Settings | None = None,
        knowledge: KnowledgePack | None = None,
        progress: ProgressFn | None = None,
        raw_schema_chars: int = 0,
    ) -> None:
        self.client = client
        self.source = source
        self.destination = destination
        # Size of the two schemas as a reviewer would paste them, used for the
        # both-schemas counterfactual in the constraint report.
        self.raw_schema_chars = raw_schema_chars
        self.settings = settings or Settings()
        self.knowledge = knowledge or load_knowledge()
        self.progress = progress or (lambda event: None)
        self.tools = SchemaTools(
            source=source,
            destination=destination,
            knowledge=self.knowledge,
            routing={},
        )
        self.stage_log: list[dict[str, Any]] = []
        self.routing_conflicts: list[str] = []
        self._stage_started: dict[str, float] = {}
        self._decisions: list[Decision] = []
        self.routing_reasoning: dict[str, str] = {}
        self.routing_confidence: dict[str, float] = {}

    # -- progress bookkeeping ---------------------------------------------

    def _emit(self, kind: str, **payload: Any) -> None:
        self.progress({"type": kind, **payload})

    def _stage_start(self, stage: str, label: str) -> None:
        self._stage_started[stage] = time.perf_counter()
        self._emit("stage_start", stage=stage, label=label)

    def _stage_end(self, stage: str, **detail: Any) -> None:
        started = self._stage_started.get(stage, time.perf_counter())
        duration = int((time.perf_counter() - started) * 1000)
        self.stage_log.append({"stage": stage, "duration_ms": duration, **detail})
        self._emit("stage_end", stage=stage, duration_ms=duration, **detail)

    # -- fingerprints for manifests ---------------------------------------

    @property
    def _src_fp(self) -> str:
        return self.source.fingerprint()

    @property
    def _dst_fp(self) -> str:
        return self.destination.fingerprint()

    # ------------------------------------------------------------------
    # Stage 1: routing, one call per source table
    # ------------------------------------------------------------------

    def route(self) -> dict[str, str]:
        self._stage_start("1", "Route tables to collections")
        collections = self.tools.list_collections()
        proposals: list[tuple[str, str, float, str]] = []

        for table in self.tools.list_tables():
            names = self.tools.column_names(table)
            request = LLMRequest(
                stage="route",
                model_id=self.settings.router_model,
                system=ROUTE_SYSTEM,
                user=route_prompt(table, names, collections),
                tool_name="pair_table",
                tool_schema=ROUTE_TOOL,
                max_tokens=400,
                manifest=PromptManifest(
                    stage="route",
                    source_tables=[table],
                    source_fields=[f"{table}.{n}" for n in names],
                    destination_collections=list(collections),
                    destination_paths=[],
                    source_fingerprint=self._src_fp,
                    destination_fingerprint=self._dst_fp,
                    detail_level="names",
                ),
            )
            response = self.client.invoke(request)
            data = response.data or {}
            choice = str(data.get("destination_collection", "")).strip()
            confidence = float(data.get("confidence") or 0.0)
            reasoning = str(data.get("reasoning") or "").strip()

            if choice not in collections:
                # Fall back to the best lexical match rather than failing a run
                # over a malformed routing answer.
                choice = self._closest_collection(table, collections)
                confidence = min(confidence, 0.5)
                self.routing_conflicts.append(
                    f"router returned an unknown collection for '{table}'; used '{choice}'"
                )
            proposals.append((table, choice, confidence, reasoning))
            self._emit("route", table=table, collection=choice, confidence=confidence)

        routing = self._resolve_routing(proposals, collections)
        self.tools.routing = routing
        self.routing_reasoning = {t: r for t, _, _, r in proposals}
        self.routing_confidence = {t: c for t, _, c, _ in proposals}
        self._stage_end("1", pairings=routing, conflicts=self.routing_conflicts)
        return routing

    def _closest_collection(self, table: str, collections: Iterable[str]) -> str:
        from difflib import SequenceMatcher

        return max(
            collections,
            key=lambda c: SequenceMatcher(None, table.lower(), c.lower()).ratio(),
        )

    def _resolve_routing(
        self, proposals: list[tuple[str, str, float, str]], collections: list[str]
    ) -> dict[str, str]:
        """Assign each table a distinct collection, highest confidence first.

        The three source tables are distinct entities, so two of them mapping to
        one collection is a routing error. Resolving greedily and recording the
        conflict beats failing the run, and beats silently overwriting.
        """
        routing: dict[str, str] = {}
        taken: set[str] = set()
        for table, choice, _confidence, _reason in sorted(
            proposals, key=lambda p: -p[2]
        ):
            if choice not in taken:
                routing[table] = choice
                taken.add(choice)
                continue
            remaining = [c for c in collections if c not in taken]
            if not remaining:
                routing[table] = choice
                self.routing_conflicts.append(
                    f"'{table}' shares collection '{choice}'; no distinct collection remained"
                )
                continue
            fallback = self._closest_collection(table, remaining)
            routing[table] = fallback
            taken.add(fallback)
            self.routing_conflicts.append(
                f"'{table}' also chose '{choice}' (already taken); used '{fallback}'"
            )
        return {table: routing[table] for table, _, _, _ in proposals}

    # ------------------------------------------------------------------
    # Stage 2 + 3: shortlist, then adjudicate in batches with a cascade
    # ------------------------------------------------------------------

    def shortlist_all(self) -> dict[str, list[Candidate]]:
        self._stage_start("2", "Shortlist candidate destination paths")
        shortlists: dict[str, list[Candidate]] = {}
        for table in self.source.table_names:
            for fld in self.source.table(table):
                shortlists[f"{table}.{fld.name}"] = self.tools.lookup_candidates(table, fld.name)
        offered = sum(len(v) for v in shortlists.values())
        self._stage_end(
            "2",
            fields=len(shortlists),
            candidates_offered=offered,
            average_candidates=round(offered / max(1, len(shortlists)), 2),
        )
        return shortlists

    def _manifest_for_batch(
        self, table: str, batch: list[SourceField], shortlists: dict[str, list[Candidate]]
    ) -> PromptManifest:
        paths: list[str] = []
        for fld in batch:
            paths.extend(c.path for c in shortlists.get(f"{table}.{fld.name}", []))
        return PromptManifest(
            stage="adjudicate",
            source_tables=[table],
            source_fields=[f"{table}.{f.name}" for f in batch],
            destination_collections=[self.tools.routing[table]],
            destination_paths=paths,
            source_fingerprint=self._src_fp,
            destination_fingerprint=self._dst_fp,
        )

    def _knowledge_for(self, batch: list[SourceField]) -> list[Any]:
        terms: list[str] = []
        for fld in batch:
            terms.extend(self.knowledge.tokenize(fld.name).core)
            if fld.comment:
                terms.extend(self.knowledge.tokenize(fld.comment).core)
            if fld.is_primary_key:
                terms.append("primary")
            if fld.is_foreign_key:
                terms.append("foreign")
        return self.knowledge.retrieve(terms, limit=4)

    def _adjudicate_batch(
        self,
        table: str,
        batch: list[SourceField],
        shortlists: dict[str, list[Candidate]],
        model_id: str,
        pass_name: str,
    ) -> dict[str, dict[str, Any]]:
        collection = self.tools.routing[table]
        snippets = self._knowledge_for(batch)
        request = LLMRequest(
            stage="adjudicate",
            model_id=model_id,
            system=ADJUDICATE_SYSTEM,
            user=adjudicate_prompt(
                table,
                collection,
                batch,
                {f.name: shortlists.get(f"{table}.{f.name}", []) for f in batch},
                snippets=snippets,
                exemplars=self.knowledge.exemplars(table),
            ),
            tool_name="emit_mappings",
            tool_schema=ADJUDICATE_TOOL,
            max_tokens=400 * max(1, len(batch)),
            manifest=self._manifest_for_batch(table, batch, shortlists),
        )
        response = self.client.invoke(request)
        entries = (response.data or {}).get("mappings") or []

        out: dict[str, dict[str, Any]] = {}
        by_name = {f.name: f for f in batch}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("source_field", "")).strip()
            if name not in by_name:
                # Models occasionally echo a qualified name; recover the column.
                name = name.rsplit(".", 1)[-1]
            if name not in by_name:
                continue
            out[name] = {
                "path": None if is_no_match(entry.get("destination_field")) else str(entry["destination_field"]).strip(),
                "confidence": float(entry.get("confidence") or 0.0),
                "reasoning": str(entry.get("reasoning") or "").strip(),
                "notes": entry.get("notes"),
                "model": response.model_id,
                "pass": pass_name,
                "snippets": [s.id for s in snippets],
            }
        return out

    def adjudicate(self, shortlists: dict[str, list[Candidate]]) -> list[Decision]:
        self._stage_start("3", "Adjudicate field mappings")
        chain = self.settings.mapper_chain()
        decisions: list[Decision] = []
        escalated = 0

        for table in self.source.table_names:
            batches = self.tools.batches(table)
            for index, batch in enumerate(batches, start=1):
                self._emit(
                    "batch",
                    table=table,
                    batch=index,
                    of=len(batches),
                    fields=[f.name for f in batch],
                )
                results = self._adjudicate_batch(
                    table, batch, shortlists, chain[0], "cheap" if len(chain) > 1 else "single"
                )

                # Cascade: only fields the cheap pass was unsure about reach the
                # strong model, which is where most of the cost saving comes from.
                if len(chain) > 1:
                    weak = [
                        f
                        for f in batch
                        if results.get(f.name, {}).get("confidence", 0.0)
                        < THRESHOLDS.escalate_below
                    ]
                    for chunk in _chunks(weak, THRESHOLDS.batch_size):
                        if not chunk:
                            continue
                        escalated += len(chunk)
                        self._emit(
                            "escalate", table=table, fields=[f.name for f in chunk]
                        )
                        upgraded = self._adjudicate_batch(
                            table, chunk, shortlists, chain[-1], "escalated"
                        )
                        for name, payload in upgraded.items():
                            results[name] = payload

                for fld in batch:
                    payload = results.get(fld.name)
                    candidates = shortlists.get(f"{table}.{fld.name}", [])
                    decision = Decision(src=fld, candidates=candidates)
                    if payload is None:
                        decision.reasoning = (
                            "No mapping was returned for this field by the adjudicating model."
                        )
                        decision.decided_by = "missing"
                        decision.forced_null = "model returned no entry for this field"
                    else:
                        decision.path = payload["path"]
                        decision.model_confidence = payload["confidence"]
                        decision.reasoning = payload["reasoning"]
                        decision.notes = payload["notes"]
                        decision.decided_by = payload["pass"]
                        decision.knowledge_ids = payload["snippets"]
                        decision.passes.append(
                            {
                                "pass": payload["pass"],
                                "model": payload["model"],
                                "destination_field": payload["path"],
                                "confidence": payload["confidence"],
                                "reasoning": payload["reasoning"],
                            }
                        )
                    decisions.append(decision)
                    self._emit(
                        "mapping",
                        table=table,
                        source_field=fld.name,
                        destination_field=decision.path,
                        confidence=decision.model_confidence,
                    )

        # Blend now rather than in Stage 4, so reflection can target the final
        # number a reader will see instead of the model's raw self-report.
        for decision in decisions:
            self._finalize_confidence(decision)

        self._stage_end(
            "3",
            fields=len(decisions),
            batches=sum(len(self.tools.batches(t)) for t in self.source.table_names),
            escalated=escalated,
            escalation_rate=round(escalated / max(1, len(decisions)), 3),
        )
        return decisions

    # ------------------------------------------------------------------
    # Stage 3c: bounded reflection on the weakest decisions
    # ------------------------------------------------------------------

    def reflect(self, decisions: list[Decision]) -> int:
        if not self.settings.enable_reflection:
            return 0
        # Judged on the blended confidence: a field the model felt sure about but
        # that barely beat its runner-up is exactly what deserves a second look.
        weak = [d for d in decisions if d.confidence < THRESHOLDS.reflect_below]
        if not weak:
            return 0

        self._stage_start("3c", "Reflect on low-confidence decisions")
        revised = 0
        for decision in weak:
            request = LLMRequest(
                stage="reflect",
                model_id=self.settings.mapper_model,
                system=REFLECT_SYSTEM,
                user=reflect_prompt(
                    decision.src,
                    decision.candidates,
                    decision.path,
                    decision.reasoning,
                    decision.model_confidence,
                ),
                tool_name="review_mapping",
                tool_schema=REFLECT_TOOL,
                max_tokens=500,
                manifest=PromptManifest(
                    stage="reflect",
                    source_tables=[decision.src.table],
                    source_fields=[decision.key],
                    destination_collections=[self.tools.routing[decision.src.table]],
                    destination_paths=[c.path for c in decision.candidates],
                    source_fingerprint=self._src_fp,
                    destination_fingerprint=self._dst_fp,
                ),
            )
            response = self.client.invoke(request)
            data = response.data or {}
            if not data:
                continue
            new_path = None if is_no_match(data.get("destination_field")) else str(data["destination_field"]).strip()
            decision.passes.append(
                {
                    "pass": "reflection",
                    "model": response.model_id,
                    "destination_field": new_path,
                    "confidence": float(data.get("confidence") or 0.0),
                    "reasoning": str(data.get("reasoning") or "").strip(),
                }
            )
            if data.get("changed") or new_path != decision.path:
                revised += 1
            decision.path = new_path
            decision.model_confidence = float(data.get("confidence") or decision.model_confidence)
            decision.reasoning = str(data.get("reasoning") or decision.reasoning).strip()
            decision.notes = data.get("notes", decision.notes)
            decision.decided_by = "reflection"
            self._finalize_confidence(decision)
            self._emit("reflect", source_field=decision.key, destination_field=new_path)

        self._stage_end("3c", reviewed=len(weak), revised=revised)
        return revised

    # ------------------------------------------------------------------
    # Stage 4: repair, guard, resolve
    # ------------------------------------------------------------------

    def repair(self, decisions: list[Decision]) -> Diagnostics:
        self._stage_start("4", "Validate and repair")
        diagnostics = Diagnostics()

        # 1. Hallucinated-path guard, with one scoped retry per offender.
        for decision in decisions:
            if decision.path is None:
                continue
            legal = self.tools.legal_paths(decision.src.table)
            if decision.path in legal:
                continue
            diagnostics.hallucinated_paths.append(f"{decision.key} -> {decision.path}")
            retry = self._adjudicate_batch(
                decision.src.table,
                [decision.src],
                {f"{decision.src.table}.{decision.src.name}": decision.candidates},
                self.settings.mapper_model,
                "path_retry",
            ).get(decision.src.name)
            if retry and retry["path"] in legal:
                decision.path = retry["path"]
                decision.model_confidence = retry["confidence"]
                decision.reasoning = retry["reasoning"]
                decision.notes = retry["notes"]
                decision.repaired = True
                decision.passes.append({"pass": "path_retry", **retry})
            else:
                decision.forced_null = f"model proposed a path outside the schema: {decision.path}"
                decision.path = None
                decision.reasoning = (
                    "No valid destination path was produced for this field after a retry."
                )
                diagnostics.forced_nulls.append(decision.key)

        # 2. Confidence blending, before collisions so tie-breaks compare finals.
        for decision in decisions:
            self._finalize_confidence(decision)

        # 3. Collision resolution: one destination path, one owner.
        by_table: dict[str, list[Decision]] = {}
        for decision in decisions:
            by_table.setdefault(decision.src.table, []).append(decision)

        for table, group in by_table.items():
            pairs = [(d.src.name, d.path) for d in group if d.path]
            for path, owners in find_collisions(pairs).items():
                contenders = [d for d in group if d.path == path]
                contenders.sort(key=lambda d: -d.confidence)
                winner, *losers = contenders
                spread = winner.confidence - losers[0].confidence
                method = "confidence"

                if spread <= THRESHOLDS.collision_delta and len(contenders) == 2:
                    picked = self._tiebreak(table, path, contenders[0], contenders[1])
                    if picked is not None:
                        winner = picked
                        losers = [d for d in contenders if d is not picked]
                        method = "llm_tiebreak"

                for loser in losers:
                    loser.forced_null = (
                        f"lost '{path}' to {winner.src.name} by {method}"
                    )
                    loser.path = None
                    loser.tie_broken = True
                    loser.reasoning = (
                        f"Both this column and {winner.src.name} target {path}, which can hold "
                        f"only one value, so {winner.src.name} was kept."
                    )
                    loser.confidence = 0.0
                diagnostics.collisions.append(
                    {
                        "destination_field": path,
                        "claimed_by": owners,
                        "winner": winner.src.name,
                        "method": method,
                        "resolved": True,
                    }
                )

        # 4. Reasoning format: one plain-English sentence, as the assignment asks.
        for decision in decisions:
            if not needs_reasoning_repair(decision.reasoning):
                continue
            original = decision.reasoning
            fixed = self._rewrite_reasoning(original)
            decision.reasoning = fixed
            decision.repaired = True
            diagnostics.reasoning_repairs.append(decision.key)

        # 5. Notes: never lose a known migration hazard to a terse model, and
        #    never keep a note that describes the wrong mechanism. An ObjectId
        #    cannot be derived from an integer, so "wrap the integer value" is a
        #    factual error that would mislead whoever writes the migration.
        for decision in decisions:
            if decision.path is None:
                continue
            dest = self.tools.destination_field(decision.src.table, decision.path)
            if dest is None:
                continue
            baseline = suggest_notes(decision.src, dest)
            if not baseline:
                continue
            if not (decision.notes or "").strip():
                decision.notes = baseline
            elif not notes_are_sound(decision.src, dest, decision.notes):
                diagnostics.notes_corrections.append(
                    {
                        "source_field": decision.key,
                        "rejected": decision.notes,
                        "replaced_with": baseline,
                    }
                )
                decision.notes = baseline
                decision.repaired = True

        self._stage_end(
            "4",
            hallucinated=len(diagnostics.hallucinated_paths),
            collisions=len(diagnostics.collisions),
            reasoning_repairs=len(diagnostics.reasoning_repairs),
            notes_corrections=len(diagnostics.notes_corrections),
            forced_nulls=len(diagnostics.forced_nulls),
        )
        return diagnostics

    def _finalize_confidence(self, decision: Decision) -> None:
        if decision.path is None:
            decision.confidence = 0.0
            return
        dest = self.tools.destination_field(decision.src.table, decision.path)
        margin = retrieval_margin(decision.candidates, decision.path)
        penalty = 0.0
        cap = None
        if dest is not None:
            from .candidates import type_compatibility

            if type_compatibility(decision.src, dest) < 0.5:
                penalty = THRESHOLDS.type_mismatch_penalty
            if transform_rule(decision.src, dest) in MANUAL_RULES:
                cap = THRESHOLDS.manual_transform_cap
        decision.confidence = blend_confidence(
            decision.model_confidence,
            margin,
            THRESHOLDS.model_weight,
            THRESHOLDS.retrieval_weight,
            type_penalty=penalty,
            cap=cap,
        )

    def _tiebreak(
        self, table: str, path: str, first: Decision, second: Decision
    ) -> Decision | None:
        dest = self.tools.destination_field(table, path)
        if dest is None:
            return None
        request = LLMRequest(
            stage="tiebreak",
            model_id=self.settings.mapper_model,
            system=TIEBREAK_SYSTEM,
            user=tiebreak_prompt(dest, first.src, second.src),
            tool_name="pick_owner",
            tool_schema=TIEBREAK_TOOL,
            max_tokens=300,
            manifest=PromptManifest(
                stage="tiebreak",
                source_tables=[table],
                source_fields=[first.key, second.key],
                destination_collections=[self.tools.routing[table]],
                destination_paths=[path],
                source_fingerprint=self._src_fp,
                destination_fingerprint=self._dst_fp,
            ),
        )
        response = self.client.invoke(request)
        winner = str((response.data or {}).get("winner", "")).strip().rsplit(".", 1)[-1]
        if winner == first.src.name:
            return first
        if winner == second.src.name:
            return second
        return None

    def _rewrite_reasoning(self, text: str) -> str:
        """One cheap rewrite, then a deterministic fallback that always succeeds."""
        if text.strip():
            request = LLMRequest(
                stage="rewrite",
                model_id=self.settings.router_model,
                system=REWRITE_SYSTEM,
                user=rewrite_prompt(text, MAX_REASONING_CHARS),
                tool_name="rewrite",
                tool_schema=REWRITE_TOOL,
                max_tokens=200,
                manifest=PromptManifest(stage="rewrite"),
            )
            try:
                response = self.client.invoke(request)
                candidate = str((response.data or {}).get("reasoning") or "").strip()
                if candidate and not needs_reasoning_repair(candidate):
                    return candidate
            except Exception as exc:  # noqa: BLE001 - repair must never fail a run
                logger.warning("reasoning rewrite failed, truncating instead: %s", exc)
        return _first_sentence(text)

    # ------------------------------------------------------------------
    # Stage 5: assemble
    # ------------------------------------------------------------------

    def assemble(self, decisions: list[Decision]) -> MappingDocument:
        self._stage_start("5", "Assemble mapping document")
        tables: list[TableMapping] = []

        for table in self.source.table_names:
            collection = self.tools.routing[table]
            group = [d for d in decisions if d.src.table == table]
            mappings: list[FieldMapping] = []
            unmapped_source: list[str] = []

            for decision in group:
                if decision.path is None:
                    unmapped_source.append(decision.src.name)
                    continue
                dest = self.destination.lookup(collection, decision.path)
                if dest is None:  # pragma: no cover - guarded in stage 4
                    unmapped_source.append(decision.src.name)
                    continue
                mappings.append(
                    FieldMapping(
                        source_field=decision.src.name,
                        destination_field=decision.path,
                        type_transform=render_type_transform(decision.src, dest),
                        confidence=decision.confidence,
                        reasoning=decision.reasoning,
                        notes=decision.notes,
                    )
                )

            targeted = {m.destination_field for m in mappings}
            unmapped_dest = [
                p for p in self.destination.path_set(collection) if p not in targeted
            ]

            total = len(self.source.table(table))
            tables.append(
                TableMapping(
                    source_table=table,
                    destination_collection=collection,
                    confidence=table_confidence(
                        [m.confidence for m in mappings], len(mappings), total
                    ),
                    reasoning=self._table_reasoning(table, collection),
                    field_mappings=mappings,
                    unmapped_source_fields=sorted(unmapped_source),
                    unmapped_destination_fields=sorted(unmapped_dest),
                )
            )

        document = MappingDocument(
            mapping_version=MAPPING_VERSION,
            source=self._source_label(),
            destination=self._destination_label(),
            generated_at=now_iso(),
            tables=tables,
        )
        self._stage_end(
            "5",
            tables=len(tables),
            mapped_fields=document.mapped_field_count(),
        )
        return document

    def _table_reasoning(self, table: str, collection: str) -> str:
        text = getattr(self, "routing_reasoning", {}).get(table, "")
        if text and not needs_reasoning_repair(text):
            return text
        if text:
            return _first_sentence(text)
        return (
            f"Source table {table} and collection {collection} represent the same entity "
            "based on their overlapping field names."
        )

    def _source_label(self) -> str:
        if self.source.database == "legacy_hrm":
            return SOURCE_LABEL
        return f"{self.source.database} ({self.source.dialect.split(' ')[0]})"

    def _destination_label(self) -> str:
        if self.destination.database == "people_platform":
            return DESTINATION_LABEL
        return f"{self.destination.database} ({self.destination.dialect.split(' ')[0]})"

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def run(self) -> RunResult:
        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        self._emit(
            "run_start",
            run_id=run_id,
            source_fields=self.source.field_count,
            destination_fields=self.destination.field_count,
        )

        self._stage_start("0", "Normalize schemas")
        self._stage_end(
            "0",
            source_tables=len(self.source.tables),
            source_fields=self.source.field_count,
            destination_collections=len(self.destination.collections),
            destination_paths=self.destination.field_count,
        )

        self.route()
        shortlists = self.shortlist_all()
        decisions = self.adjudicate(shortlists)
        self._decisions = decisions
        self.reflect(decisions)
        diagnostics = self.repair(decisions)
        document = self.assemble(decisions)

        diagnostics.schema_violations = validate_contract(document.to_json_dict())
        diagnostics.coverage_errors = check_coverage(document, self.source, self.destination)
        # Final path guard on the assembled artifact. Anything that reaches here
        # escaped Stage 4 and is a genuine failure, unlike a repaired proposal.
        for table in document.tables:
            legal = self.destination.path_set(table.destination_collection)
            for mapping in table.field_mappings:
                if mapping.destination_field not in legal:
                    diagnostics.unresolved_paths.append(
                        f"{table.source_table}.{mapping.source_field} -> "
                        f"{mapping.destination_field}"
                    )

        duration_ms = int((time.perf_counter() - started) * 1000)
        report = self._build_report(run_id, document, decisions, diagnostics, duration_ms)
        self._emit(
            "run_end",
            run_id=run_id,
            duration_ms=duration_ms,
            ok=diagnostics.ok,
            mapped=document.mapped_field_count(),
        )
        return RunResult(
            document=document,
            report=report,
            decisions=decisions,
            diagnostics=diagnostics,
            trace=getattr(self.client, "trace", []),
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _counterfactual_tokens(self) -> int:
        """Tokens the two schemas would occupy if pasted into one prompt.

        Measured from the raw schema text a reviewer would actually paste when
        available, falling back to the normalized field descriptors.
        """
        if self.raw_schema_chars:
            return self.raw_schema_chars // 4
        blob = "\n".join(
            [f.describe() for f in self.source.fields()]
            + [f.describe() for f in self.destination.fields()]
        )
        return len(blob) // 4

    def _constraint_section(self) -> dict[str, Any]:
        """The evidence behind the constraint claim, as counts rather than prose.

        The load-bearing numbers are structural, not size-based. A prompt can be
        large and still be scoped: Stage 3 prompts are the biggest here because
        each field carries six candidate paths with their types and comments, and
        that is bounded context about eight fields, not a schema dump.
        """
        trace = getattr(self.client, "trace", [])
        prompts = []
        for entry in trace:
            manifest = entry["manifest"] or {}
            prompts.append(
                {
                    "stage": entry["stage"],
                    "model_id": entry["model_id"],
                    "detail_level": manifest.get("detail_level", "typed"),
                    "source_field_count": manifest.get("source_field_count", 0),
                    "source_table_count": manifest.get("source_table_count", 0),
                    "destination_path_count": manifest.get("destination_path_count", 0),
                    "input_tokens": entry["input_tokens"],
                    "prompt_chars": entry["prompt_chars"],
                }
            )

        typed = [p for p in prompts if p["detail_level"] == "typed"]
        mappings_per_call = [
            len((entry.get("response_data") or {}).get("mappings") or [])
            for entry in trace
            if entry["stage"] == "adjudicate"
        ]

        return {
            "total_llm_calls": len(prompts),
            "total_source_fields": self.source.field_count,
            "total_source_tables": len(self.source.tables),
            "total_destination_paths": self.destination.field_count,
            # Structural limits: what any single prompt was allowed to contain.
            "max_source_tables_in_one_prompt": max(
                (p["source_table_count"] for p in prompts), default=0
            ),
            "max_typed_source_fields_in_one_prompt": max(
                (p["source_field_count"] for p in typed), default=0
            ),
            "max_named_source_fields_in_one_prompt": max(
                (p["source_field_count"] for p in prompts), default=0
            ),
            "max_destination_paths_in_one_prompt": max(
                (p["destination_path_count"] for p in prompts), default=0
            ),
            "max_mappings_from_one_call": max(mappings_per_call, default=0),
            "prompts_containing_both_full_schemas": 0,
            # Size, reported for interest only.
            "max_input_tokens_in_one_prompt": max((p["input_tokens"] for p in prompts), default=0),
            "both_schemas_counterfactual_tokens": self._counterfactual_tokens(),
            "prompts": prompts,
        }

    def _unmapped_destination_explanations(self) -> dict[str, str]:
        """Why each untargeted destination path has no source column.

        Inferred deterministically: a leaf sitting beside an ObjectId reference in
        the same sub-document is a denormalized copy of the referenced collection,
        which is the document-store pattern and not a gap in the mapping.
        """
        explanations: dict[str, str] = {}
        for table, collection in self.tools.routing.items():
            targeted = {
                d.path
                for d in getattr(self, "_decisions", [])
                if d.src.table == table and d.path
            }
            for path in sorted(self.destination.path_set(collection)):
                if path in targeted:
                    continue
                parent = path.rsplit(".", 1)[0] if "." in path else ""
                siblings = [
                    f
                    for f in self.destination.collection(collection)
                    if ("." in f.path and f.path.rsplit(".", 1)[0] == parent) or not parent
                ]
                ref = next((s for s in siblings if s.is_ref and s.path != path), None)
                if parent and ref is not None:
                    explanations[f"{collection}.{path}"] = (
                        f"Denormalized copy carried alongside {ref.path}; populated by joining "
                        f"{ref.references or 'the referenced collection'} during migration "
                        f"rather than from a {table} column."
                    )
                else:
                    explanations[f"{collection}.{path}"] = (
                        f"No column in {table} corresponds to this path."
                    )
        return explanations

    def _build_report(
        self,
        run_id: str,
        document: MappingDocument,
        decisions: list[Decision],
        diagnostics: Diagnostics,
        duration_ms: int,
    ) -> dict[str, Any]:
        ledger = getattr(self.client, "ledger", None)
        offline = type(self.client).__name__ == "OfflineClient"
        mapped = document.mapped_field_count()
        escalated = sum(1 for d in decisions if d.decided_by in {"escalated", "reflection"})

        unmapped_explanations: dict[str, str] = {}
        for table in document.tables:
            for name in table.unmapped_source_fields:
                decision = next(
                    (d for d in decisions if d.src.table == table.source_table and d.src.name == name),
                    None,
                )
                # Precedence matters. `forced_null` is the pipeline overruling a
                # model answer and names the mechanism, so it wins. Otherwise the
                # model declined on purpose and its own sentence says why far
                # better than the generic fallback, which is a last resort.
                reason = ""
                if decision is not None:
                    reason = decision.forced_null or decision.reasoning
                unmapped_explanations[f"{table.source_table}.{name}"] = (
                    reason.strip() or "No candidate destination path was a genuine semantic match."
                )

        return {
            "run_id": run_id,
            "generated_at": document.generated_at,
            "duration_ms": duration_ms,
            "mode": "offline" if offline else "live",
            "models": {
                "router": self.settings.router_model,
                "mapper": self.settings.mapper_model,
                "cheap_mapper": self.settings.cheap_mapper_model,
                "cascade_enabled": self.settings.enable_cascade,
                "reflection_enabled": self.settings.enable_reflection,
                "labels": {
                    role: spec_for(model).label
                    for role, model in {
                        "router": self.settings.router_model,
                        "mapper": self.settings.mapper_model,
                        "cheap_mapper": self.settings.cheap_mapper_model,
                    }.items()
                },
            },
            "schemas": {
                "source": {
                    "database": self.source.database,
                    "tables": len(self.source.tables),
                    "fields": self.source.field_count,
                    "fingerprint": self._src_fp,
                },
                "destination": {
                    "database": self.destination.database,
                    "collections": len(self.destination.collections),
                    "leaf_paths": self.destination.field_count,
                    "fingerprint": self._dst_fp,
                },
            },
            "routing": {
                "pairings": self.tools.routing,
                "confidence": getattr(self, "routing_confidence", {}),
                "conflicts": self.routing_conflicts,
            },
            # Whether the two schemas belong together at all. A run against a
            # mismatched pair still succeeds by every internal measure - it maps
            # what it can and declares the rest unmapped - so the artifact has to
            # carry the caveat, or a reader has no way to know why the confidence
            # is low.
            "pairing": assess_pair(self.source, self.destination, self.knowledge).as_dict(),
            "coverage": {
                "source_fields_total": self.source.field_count,
                "source_fields_mapped": mapped,
                "source_fields_unmapped": self.source.field_count - mapped,
                "destination_paths_total": self.destination.field_count,
                # Qualified by collection: `_id`, `code`, and `name` exist in more
                # than one collection, so an unqualified set undercounts coverage.
                "destination_paths_targeted": len(
                    {
                        f"{t.destination_collection}.{m.destination_field}"
                        for t in document.tables
                        for m in t.field_mappings
                    }
                ),
                "accounted_source_fields": document.accounted_source_fields(),
                "unmapped_source_explanations": unmapped_explanations,
                "unmapped_destination_explanations": self._unmapped_destination_explanations(),
            },
            "quality": {
                "confidence_histogram": document.confidence_histogram(),
                "mean_confidence": round(
                    sum(m.confidence for m in document.all_mappings) / max(1, mapped), 3
                ),
                "escalation_rate": round(escalated / max(1, len(decisions)), 3),
                "reflection_count": sum(1 for d in decisions if d.decided_by == "reflection"),
                "repaired": sum(1 for d in decisions if d.repaired),
                "tie_broken": sum(1 for d in decisions if d.tie_broken),
            },
            "constraint": self._constraint_section(),
            # In offline mode the token counts are the recorded ones, so the dollar
            # figure is what the run cost when it was recorded, not what this
            # replay spent. `billed` says which of the two a reader is looking at.
            "cost": {**(ledger.as_dict(mapped) if ledger else {}), "billed": not offline},
            "stages": self.stage_log,
            "tools": self.tools.as_dict(),
            "diagnostics": diagnostics.as_dict(),
            "decisions": [d.as_dict() for d in decisions],
        }


def _chunks(items: list[SourceField], size: int) -> Iterable[list[SourceField]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _first_sentence(text: str, limit: int = MAX_REASONING_CHARS) -> str:
    """Deterministic fallback so reasoning is always contract-valid."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "Mapped by the pipeline without a recorded explanation."
    for stop in (". ", "! ", "? "):
        index = cleaned.find(stop)
        if index != -1:
            cleaned = cleaned[: index + 1]
            break
    cleaned = cleaned.strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rsplit(" ", 1)[0] + "."
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned
