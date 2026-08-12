"""Is this source schema even related to this destination schema?

Nothing stopped a run of `legacy_library` against `people_platform`, and the
pipeline dutifully paired `bk_master -> employees`. Every stage behaved as
designed: routing must choose *some* collection, and shortlisting must offer
*some* candidates, so a mismatched pair produces confident-looking nonsense at
the top and a pile of unmapped fields underneath.

The check belongs before the run, and it has to be free - it runs on every
keystroke in the input panel, so it cannot cost a model call.

Method: reuse the Stage 2 signals, but only the ones that say anything about
*meaning* - name overlap, fuzzy name similarity, and comment agreement. The other
two signals are deliberately excluded because they are domain-blind: a VARCHAR
scores full type compatibility against any String on earth, and a primary key
scores full key-role affinity against any `_id`. Including them puts a floor
under every pair and hides exactly the difference this needs to detect.
"""

from __future__ import annotations

from dataclasses import dataclass

from .candidates import comment_score, fuzzy_score, lexical_score
from .knowledge import KnowledgePack, load_knowledge
from .normalize import DestField, DestinationSchema, SourceField, SourceSchema

# Renormalized from the Stage 2 weights, dropping type_compat and key_role.
W_LEXICAL = 0.45
W_FUZZY = 0.25
W_COMMENT = 0.30

# A field whose best destination clears this has a plausible home. Calibrated in
# scripts/eval_pairing.py against every bundled pair, matched and mismatched.
FIELD_AFFINITY_FLOOR = 0.30

# Verdict boundaries on the fraction of source fields that clear the floor.
ALIGNED_AT = 0.55
WEAK_AT = 0.30


@dataclass(frozen=True)
class TablePairing:
    """The collection a table would most likely be routed to, and how well it fits."""

    table: str
    collection: str
    affinity: float
    fields: int
    placed_fields: int


@dataclass(frozen=True)
class PairAssessment:
    score: float
    verdict: str
    headline: str
    detail: str
    placed_fields: int
    total_fields: int
    pairings: list[TablePairing]

    @property
    def ok(self) -> bool:
        return self.verdict == "aligned"

    def as_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 3),
            "verdict": self.verdict,
            "headline": self.headline,
            "detail": self.detail,
            "placed_fields": self.placed_fields,
            "total_fields": self.total_fields,
            "pairings": [
                {
                    "table": p.table,
                    "collection": p.collection,
                    "affinity": round(p.affinity, 3),
                    "fields": p.fields,
                    "placed_fields": p.placed_fields,
                }
                for p in self.pairings
            ],
        }


def field_affinity(src: SourceField, dest: DestField, knowledge: KnowledgePack) -> float:
    """Vocabulary-only similarity between one column and one destination path."""
    lexical = lexical_score(knowledge.tokenize(src.name), knowledge.tokenize(dest.path))
    fuzzy = fuzzy_score(src, dest)
    comment = comment_score(src, dest, knowledge)
    return min(1.0, W_LEXICAL * lexical + W_FUZZY * fuzzy + W_COMMENT * comment)


def best_affinity(
    src: SourceField, dest_fields: list[DestField], knowledge: KnowledgePack
) -> float:
    return max((field_affinity(src, dest, knowledge) for dest in dest_fields), default=0.0)


def assess_pair(
    source: SourceSchema,
    destination: DestinationSchema,
    knowledge: KnowledgePack | None = None,
) -> PairAssessment:
    """Judge whether these two schemas describe the same domain.

    Deterministic and model-free. The headline counts fields rather than quoting
    the score, because "7 of 31 columns have a plausible destination" tells you
    what to do and "affinity 0.21" does not.
    """
    pack = knowledge or load_knowledge()
    pairings: list[TablePairing] = []
    placed_total = 0
    field_total = 0

    for table in source.table_names:
        src_fields = source.table(table)
        best: TablePairing | None = None
        for collection in destination.collection_names:
            dest_fields = destination.collection(collection)
            affinities = [best_affinity(src, dest_fields, pack) for src in src_fields]
            mean = sum(affinities) / len(affinities) if affinities else 0.0
            placed = sum(1 for a in affinities if a >= FIELD_AFFINITY_FLOOR)
            if best is None or (placed, mean) > (best.placed_fields, best.affinity):
                best = TablePairing(
                    table=table,
                    collection=collection,
                    affinity=mean,
                    fields=len(src_fields),
                    placed_fields=placed,
                )
        if best is None:
            continue
        pairings.append(best)
        placed_total += best.placed_fields
        field_total += best.fields

    score = placed_total / field_total if field_total else 0.0
    if score >= ALIGNED_AT:
        verdict = "aligned"
    elif score >= WEAK_AT:
        verdict = "weak"
    else:
        verdict = "unrelated"

    headline = {
        "aligned": "These schemas look like a matching pair.",
        "weak": "These schemas only partly overlap.",
        "unrelated": "These schemas look unrelated.",
    }[verdict]

    if verdict == "aligned":
        detail = (
            f"{placed_total} of {field_total} source columns have a plausible destination."
        )
    else:
        worst = sorted(pairings, key=lambda p: p.placed_fields / max(p.fields, 1))
        examples = ", ".join(f"{p.table} \u2192 {p.collection}" for p in worst[:2])
        detail = (
            f"Only {placed_total} of {field_total} source columns have a plausible "
            f"destination, and the best available pairings are forced ({examples}). "
            "A run will still finish, but expect low confidence and many unmapped fields. "
            "Check that both files come from the same pair."
        )

    return PairAssessment(
        score=score,
        verdict=verdict,
        headline=headline,
        detail=detail,
        placed_fields=placed_total,
        total_fields=field_total,
        pairings=pairings,
    )
