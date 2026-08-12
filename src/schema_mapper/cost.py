"""Token accounting and USD pricing.

Every Bedrock call is recorded with its real reported token usage, never an
estimate, so the dollar figure in the run report and the UI is derived from
`usage` fields rather than a guess. Prices come from the registry in
:mod:`schema_mapper.config`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import MODEL_REGISTRY, spec_for


class BudgetExceeded(RuntimeError):
    """Raised before a call that would breach the run's token ceiling.

    Deliberately raised *before* spending rather than after, so a runaway loop
    cannot bill first and report second.
    """


@dataclass
class CallCost:
    stage: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cache_hit: bool = False

    @property
    def usd(self) -> float:
        if self.cache_hit:
            return 0.0
        spec = spec_for(self.model_id)
        return (
            self.input_tokens / 1_000_000 * spec.input_per_mtok
            + self.output_tokens / 1_000_000 * spec.output_per_mtok
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "model_id": self.model_id,
            "model": spec_for(self.model_id).label,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "cache_hit": self.cache_hit,
            "usd": round(self.usd, 6),
        }


@dataclass
class CostLedger:
    max_tokens: int = 120_000
    calls: list[CallCost] = field(default_factory=list)

    def record(
        self,
        stage: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        cache_hit: bool = False,
    ) -> CallCost:
        entry = CallCost(stage, model_id, input_tokens, output_tokens, latency_ms, cache_hit)
        self.calls.append(entry)
        return entry

    def check_budget(self, projected: int = 0) -> None:
        if self.total_tokens + projected > self.max_tokens:
            raise BudgetExceeded(
                f"Run token ceiling reached: {self.total_tokens} used of {self.max_tokens} "
                f"(next call needs about {projected}). Raise MAX_TOKENS_PER_RUN to continue."
            )

    @property
    def billable_calls(self) -> int:
        return sum(1 for c in self.calls if not c.cache_hit)

    @property
    def cache_hits(self) -> int:
        return sum(1 for c in self.calls if c.cache_hit)

    @property
    def total_input(self) -> int:
        return sum(c.input_tokens for c in self.calls if not c.cache_hit)

    @property
    def total_output(self) -> int:
        return sum(c.output_tokens for c in self.calls if not c.cache_hit)

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output

    @property
    def total_usd(self) -> float:
        return sum(c.usd for c in self.calls)

    def by_stage(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for call in self.calls:
            bucket = out.setdefault(
                call.stage,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "models": []},
            )
            bucket["calls"] = int(bucket["calls"]) + 1
            bucket["input_tokens"] = int(bucket["input_tokens"]) + call.input_tokens
            bucket["output_tokens"] = int(bucket["output_tokens"]) + call.output_tokens
            bucket["usd"] = round(float(bucket["usd"]) + call.usd, 6)
            models = bucket["models"]
            assert isinstance(models, list)
            if call.model_id not in models:
                models.append(call.model_id)
        return out

    def by_model(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for call in self.calls:
            bucket = out.setdefault(
                call.model_id,
                {
                    "label": spec_for(call.model_id).label,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "usd": 0.0,
                },
            )
            bucket["calls"] = int(bucket["calls"]) + 1
            bucket["input_tokens"] = int(bucket["input_tokens"]) + call.input_tokens
            bucket["output_tokens"] = int(bucket["output_tokens"]) + call.output_tokens
            bucket["usd"] = round(float(bucket["usd"]) + call.usd, 6)
        return out

    def what_if(self) -> dict[str, float]:
        """Reprice this run's actual token counts against every registered model.

        Answers "what would this have cost on a cheaper model" without rerunning
        anything, using measured tokens rather than projections.
        """
        projections: dict[str, float] = {}
        for model_id, spec in MODEL_REGISTRY.items():
            if spec.tier == "embedding":
                continue
            projections[model_id] = round(
                self.total_input / 1_000_000 * spec.input_per_mtok
                + self.total_output / 1_000_000 * spec.output_per_mtok,
                6,
            )
        return projections

    def cost_per_mapped_field(self, mapped: int) -> float:
        return round(self.total_usd / mapped, 6) if mapped else 0.0

    def as_dict(self, mapped_fields: int = 0) -> dict[str, object]:
        return {
            "total_usd": round(self.total_usd, 6),
            "total_input_tokens": self.total_input,
            "total_output_tokens": self.total_output,
            "total_tokens": self.total_tokens,
            "billable_calls": self.billable_calls,
            "cache_hits": self.cache_hits,
            "token_ceiling": self.max_tokens,
            "cost_per_mapped_field": self.cost_per_mapped_field(mapped_fields),
            "by_stage": self.by_stage(),
            "by_model": self.by_model(),
            "what_if_single_model": self.what_if(),
            "calls": [c.as_dict() for c in self.calls],
        }
