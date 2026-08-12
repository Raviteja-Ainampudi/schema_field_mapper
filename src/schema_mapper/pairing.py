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

# A field whose best destination clears this has a plausible home. Used only for
# the interpretable "N of M columns" counts, not for the verdict.
FIELD_AFFINITY_FLOOR = 0.30

# Verdict boundaries on the field-weighted mean affinity, calibrated by
# scripts/eval_pairing.py over all 16 source x destination combinations of the
# four bundled pairs:
#
#   true pairs    0.477 .. 0.611
#   crossed pairs 0.278 .. 0.429
#
# The gap is real but narrow, which is why this warns and never blocks: the
# counting metric tried first (fraction of columns with any plausible
# destination) could not separate them at all, because a crossed pair genuinely
# does place generic columns - `locations -> branches` scores 0.528 on its own
# merits, since both really are addresses.
ALIGNED_AT = 0.45
UNRELATED_BELOW = 0.38

# A single table whose best collection scores below this is being forced, even if
# the schema as a whole looks fine. Same calibration: real table pairings run
# 0.456 and up, forced ones 0.436 and down, with two honest exceptions noted above.
TABLE_ALIGNED_AT = 0.45


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

    score = (
        sum(p.affinity * p.fields for p in pairings) / field_total if field_total else 0.0
    )
    if score >= ALIGNED_AT:
        verdict = "aligned"
    elif score >= UNRELATED_BELOW:
        verdict = "weak"
    else:
        verdict = "unrelated"

    forced = [p for p in pairings if p.affinity < TABLE_ALIGNED_AT]
    # A single homeless table in an otherwise plausible schema is the common real
    # case and the one worth naming, so it downgrades the verdict rather than
    # being averaged away.
    if verdict == "aligned" and forced:
        verdict = "weak"

    headline = {
        "aligned": "These schemas look like a matching pair.",
        "weak": "These schemas only partly overlap.",
        "unrelated": "These schemas look unrelated.",
    }[verdict]

    if verdict == "aligned":
        detail = f"{placed_total} of {field_total} source columns have a plausible destination."
    else:
        if forced:
            named = ", ".join(f"{p.table} \u2192 {p.collection}" for p in forced[:3])
            lead = (
                f"{len(forced)} of {len(pairings)} source tables "
                f"{'has' if len(forced) == 1 else 'have'} no clearly matching collection, "
                f"so the closest available is forced ({named})."
            )
        else:
            lead = "No source table has a clearly matching collection."
        detail = (
            f"{lead} A run will finish, but expect low confidence and many unmapped fields. "
            "Check that both files are two halves of the same pair."
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
