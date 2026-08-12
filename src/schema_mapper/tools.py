"""The scoped tool surface: the only schema access LLM-facing code is given.

This is where the assignment's constraint is enforced as code rather than as
prompt wording. No method here can return a whole schema:

* :meth:`SchemaTools.column_names` returns names with no types, for routing.
* :meth:`SchemaTools.get_source_fields` returns a bounded batch and refuses to
  return more than ``batch_size`` fields in one call.
* :meth:`SchemaTools.lookup_candidates` returns at most ``top_k`` destination
  paths for one named field.

Deterministic bookkeeping (coverage accounting, collision detection) reads the
schemas directly, because it is code with no context window and cannot leak
anything into a prompt. The distinction that matters is not "who may read the
schema" but "what can reach a prompt", and everything that reaches a prompt is
assembled from the methods below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .candidates import Candidate, shortlist_field
from .config import THRESHOLDS
from .knowledge import KnowledgePack, load_knowledge
from .normalize import DestinationSchema, DestField, SourceField, SourceSchema


class ToolScopeError(RuntimeError):
    """A caller asked for more schema than a single prompt is allowed to carry."""


@dataclass
class SchemaTools:
    source: SourceSchema
    destination: DestinationSchema
    knowledge: KnowledgePack = field(default_factory=load_knowledge)
    routing: dict[str, str] = field(default_factory=dict)
    top_k: int = THRESHOLDS.top_k
    batch_size: int = THRESHOLDS.batch_size
    min_score: float = THRESHOLDS.min_candidate_score
    calls: list[dict[str, object]] = field(default_factory=list)

    # -- naming only -------------------------------------------------------

    def list_tables(self) -> list[str]:
        self.calls.append({"tool": "list_tables"})
        return self.source.table_names

    def list_collections(self) -> list[str]:
        self.calls.append({"tool": "list_collections"})
        return self.destination.collection_names

    def column_names(self, table: str) -> list[str]:
        """Column names for one table. No types, comments, or keys."""
        self.calls.append({"tool": "column_names", "table": table})
        return [f.name for f in self.source.table(table)]

    # -- bounded field access ---------------------------------------------

    def get_source_fields(self, table: str, offset: int = 0, limit: int | None = None) -> list[SourceField]:
        size = self.batch_size if limit is None else limit
        if size > self.batch_size:
            raise ToolScopeError(
                f"Requested {size} fields but a single prompt may carry at most "
                f"{self.batch_size}; split the request into batches."
            )
        self.calls.append(
            {"tool": "get_source_fields", "table": table, "offset": offset, "limit": size}
        )
        return self.source.table(table)[offset : offset + size]

    def batches(self, table: str) -> list[list[SourceField]]:
        fields = self.source.table(table)
        return [
            fields[i : i + self.batch_size] for i in range(0, len(fields), self.batch_size)
        ]

    # -- candidate retrieval ----------------------------------------------

    def lookup_candidates(self, table: str, field_name: str) -> list[Candidate]:
        """Top-K destination paths for one field, inside its routed collection."""
        collection = self.routing.get(table)
        if collection is None:
            raise ToolScopeError(
                f"Table '{table}' has not been routed yet; candidates are scoped to the "
                "matched collection."
            )
        src = next((f for f in self.source.table(table) if f.name == field_name), None)
        if src is None:
            raise ToolScopeError(f"Unknown field '{table}.{field_name}'.")

        candidates = shortlist_field(
            src,
            self.destination.collection(collection),
            self.knowledge,
            top_k=self.top_k,
            min_score=self.min_score,
            ref_collections=self.routing,
        )
        self.calls.append(
            {
                "tool": "lookup_candidates",
                "table": table,
                "field": field_name,
                "returned": len(candidates),
            }
        )
        return candidates

    def destination_field(self, table: str, path: str) -> DestField | None:
        collection = self.routing.get(table)
        if collection is None:
            return None
        return self.destination.lookup(collection, path)

    def legal_paths(self, table: str) -> set[str]:
        """Allowlist for the hallucinated-path guard."""
        collection = self.routing.get(table)
        return self.destination.path_set(collection) if collection else set()

    def as_dict(self) -> dict[str, object]:
        return {
            "tool_calls": len(self.calls),
            "by_tool": {
                name: sum(1 for c in self.calls if c.get("tool") == name)
                for name in sorted({str(c.get("tool")) for c in self.calls})
            },
        }
