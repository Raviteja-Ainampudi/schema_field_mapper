"""Stage 2: deterministic candidate shortlisting. No LLM, no network, free.

For each source field this ranks the destination paths inside its matched
collection and keeps the best few. Two things follow from that:

* **Cost.** Stage 3 prompts carry six candidate paths per field instead of the
  whole destination schema, which is what keeps prompts in the hundreds of
  tokens rather than thousands.
* **The accuracy ceiling.** A field whose true destination is not in its
  shortlist cannot be mapped correctly by any model at any price. Shortlist
  recall is therefore the metric to watch, and ``tests/test_candidates.py``
  gates it at 100% against the expected-mapping oracle.

Every score is a weighted sum of five explainable components rather than one
opaque similarity number, so the UI can show *why* a candidate ranked where it
did and a reviewer can audit the ranking without running anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import THRESHOLDS
from .knowledge import KnowledgePack, TokenSet, load_knowledge, split_identifier
from .normalize import DestField, SourceField, is_enum_code

# Component weights. They sum to 1.0 so a total is readable as a 0-1 score.
W_LEXICAL = 0.38
W_FUZZY = 0.12
W_TYPE = 0.20
W_KEY = 0.14
W_COMMENT = 0.16

# Applied only when embeddings are enabled; the deterministic components are
# rescaled so the total stays in 0-1.
W_EMBEDDING = 0.15


@dataclass(frozen=True)
class ScoreBreakdown:
    lexical: float
    fuzzy: float
    type_compat: float
    key_role: float
    comment: float
    embedding: float | None
    total: float

    def as_dict(self) -> dict[str, float | None]:
        return {
            "lexical": round(self.lexical, 4),
            "fuzzy": round(self.fuzzy, 4),
            "type_compat": round(self.type_compat, 4),
            "key_role": round(self.key_role, 4),
            "comment": round(self.comment, 4),
            "embedding": None if self.embedding is None else round(self.embedding, 4),
            "total": round(self.total, 4),
        }


@dataclass(frozen=True)
class Candidate:
    field: DestField
    scores: ScoreBreakdown

    @property
    def path(self) -> str:
        return self.field.path

    @property
    def score(self) -> float:
        return self.scores.total


# ---------------------------------------------------------------------------
# Type compatibility
# ---------------------------------------------------------------------------

_TYPE_MATRIX: dict[tuple[str, str], float] = {
    ("integer", "number"): 1.0,
    ("integer", "objectid"): 0.75,
    ("integer", "string"): 0.30,
    ("integer", "boolean"): 0.25,
    ("decimal", "number"): 1.0,
    ("decimal", "string"): 0.25,
    ("string", "string"): 1.0,
    ("string", "objectid"): 0.30,
    ("string", "number"): 0.20,
    ("string", "boolean"): 0.25,
    ("string", "datetime"): 0.20,
    ("date", "datetime"): 1.0,
    ("date", "string"): 0.40,
    ("datetime", "datetime"): 1.0,
    ("datetime", "string"): 0.40,
    ("datetime", "number"): 0.20,
    ("boolean", "boolean"): 1.0,
    ("boolean", "number"): 0.40,
    ("boolean", "string"): 0.30,
    ("object", "object"): 1.0,
    ("binary", "string"): 0.30,
}


def type_compatibility(src: SourceField, dest: DestField) -> float:
    """How plausible is this type move, ignoring names entirely."""
    src_cat, dest_cat = src.category, dest.category

    # A CHAR(1) code column with a legend in its comment is an enum in disguise.
    # It legitimately targets either a readable String enum or, when the legend
    # has exactly two states, a Boolean.
    if is_enum_code(src):
        if dest_cat == "string":
            return 1.0
        if dest_cat == "boolean":
            return 0.65

    return _TYPE_MATRIX.get((src_cat, dest_cat), 0.15)


# ---------------------------------------------------------------------------
# Key-role compatibility
# ---------------------------------------------------------------------------


def key_role_score(
    src: SourceField, dest: DestField, ref_collections: dict[str, str] | None = None
) -> float:
    """Reward structurally sensible key moves, penalize nonsensical ones.

    The strongest signal available for foreign keys: when the source column
    references ``locations.loc_id`` and the destination path is an ObjectId
    referencing the collection that ``locations`` was routed to, that is almost
    certainly the same relationship.
    """
    is_id_path = dest.path == "_id"

    if src.is_primary_key:
        if is_id_path:
            return 1.0
        return 0.35 if dest.is_ref else 0.15

    if src.is_foreign_key:
        if dest.is_ref:
            if ref_collections and src.references and dest.references:
                src_table = src.references.split(".", 1)[0]
                target_collection = ref_collections.get(src_table)
                dest_collection = dest.references.split(".", 1)[0]
                if target_collection and target_collection == dest_collection:
                    return 1.0
            return 0.70
        return 0.20 if is_id_path else 0.10

    if is_id_path:
        # A plain, non-key column mapping to the document identity is almost
        # always wrong; make it expensive rather than impossible.
        return 0.0

    if src.is_unique and dest.leaf.lower().endswith("code"):
        return 0.85

    return 0.40 if not dest.is_ref else 0.10


# ---------------------------------------------------------------------------
# Lexical scoring
# ---------------------------------------------------------------------------


def _overlap(a: frozenset[str], b: frozenset[str]) -> tuple[float, float]:
    if not a or not b:
        return 0.0, 0.0
    inter = len(a & b)
    containment = inter / min(len(a), len(b))
    jaccard = inter / len(a | b)
    return containment, jaccard


def lexical_score(src_tokens: TokenSet, dest_tokens: TokenSet) -> float:
    """Core-token agreement, with synonyms allowed to fill an empty gap.

    Concept (synonym) overlap only ever adds to a weak core score and cannot
    exceed it, so ``dept_stat`` can reach ``isActive`` without every date-ish
    field looking identical to every other one.
    """
    containment, jaccard = _overlap(src_tokens.core, dest_tokens.core)
    core = 0.7 * containment + 0.3 * jaccard

    concept_hits = (
        len(src_tokens.concepts & dest_tokens.core)
        + len(src_tokens.core & dest_tokens.concepts)
        + 0.5 * len(src_tokens.concepts & dest_tokens.concepts)
    )
    denom = max(1, min(len(src_tokens.core), len(dest_tokens.core)))
    concept = min(1.0, concept_hits / denom)

    return min(1.0, core + 0.35 * concept * (1.0 - core))


def fuzzy_score(src: SourceField, dest: DestField) -> float:
    """Character-level similarity, to catch what tokenization misses."""
    src_flat = "".join(split_identifier(src.name))
    leaf_flat = "".join(split_identifier(dest.leaf))
    path_flat = "".join(split_identifier(dest.path))
    return max(
        SequenceMatcher(None, src_flat, leaf_flat).ratio(),
        SequenceMatcher(None, src_flat, path_flat).ratio(),
    )


def comment_score(
    src: SourceField, dest: DestField, knowledge: KnowledgePack
) -> float:
    """Agreement between column comments, and between a comment and a path name.

    This is the signal that decides several of the hardest fields. Both
    ``sal_currency`` and ``compensation.currency`` say "ISO 4217"; both
    ``rec_stat`` and ``employment.status`` enumerate active/inactive/terminated.
    The comment-to-name fallback is what connects ``dept_stat`` ("A=Active,
    I=Inactive") to ``isActive``, which share no identifier tokens at all.
    """
    if not src.comment:
        return 0.0

    src_tokens = knowledge.tokenize(src.comment).core
    best = 0.0

    if dest.comment:
        containment, jaccard = _overlap(src_tokens, knowledge.tokenize(dest.comment).core)
        best = max(best, 0.7 * containment + 0.3 * jaccard)

    containment, _ = _overlap(src_tokens, knowledge.tokenize(dest.path).core)
    best = max(best, 0.6 * containment)

    return min(1.0, best)


# ---------------------------------------------------------------------------
# Shortlisting
# ---------------------------------------------------------------------------


def score_pair(
    src: SourceField,
    dest: DestField,
    knowledge: KnowledgePack,
    ref_collections: dict[str, str] | None = None,
    embedding: float | None = None,
) -> ScoreBreakdown:
    lexical = lexical_score(knowledge.tokenize(src.name), knowledge.tokenize(dest.path))
    fuzzy = fuzzy_score(src, dest)
    type_compat = type_compatibility(src, dest)
    key_role = key_role_score(src, dest, ref_collections)
    comment = comment_score(src, dest, knowledge)

    total = (
        W_LEXICAL * lexical
        + W_FUZZY * fuzzy
        + W_TYPE * type_compat
        + W_KEY * key_role
        + W_COMMENT * comment
    )

    if embedding is not None:
        total = total * (1.0 - W_EMBEDDING) + W_EMBEDDING * embedding

    return ScoreBreakdown(
        lexical=lexical,
        fuzzy=fuzzy,
        type_compat=type_compat,
        key_role=key_role,
        comment=comment,
        embedding=embedding,
        total=min(1.0, max(0.0, total)),
    )


def shortlist_field(
    src: SourceField,
    dest_fields: list[DestField],
    knowledge: KnowledgePack | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
    ref_collections: dict[str, str] | None = None,
    embeddings: dict[str, float] | None = None,
) -> list[Candidate]:
    """Top-K destination paths for one source field, best first.

    Returns an empty list when nothing clears the floor, which is how a field
    with no sensible destination is reported unmapped instead of being forced
    onto the least-bad option.
    """
    pack = knowledge or load_knowledge()
    k = THRESHOLDS.top_k if top_k is None else top_k
    floor = THRESHOLDS.min_candidate_score if min_score is None else min_score

    scored = [
        Candidate(
            field=dest,
            scores=score_pair(
                src,
                dest,
                pack,
                ref_collections,
                (embeddings or {}).get(dest.path),
            ),
        )
        for dest in dest_fields
    ]
    scored = [c for c in scored if c.score >= floor]
    # Sort by score, then by path for a stable order when scores tie exactly.
    scored.sort(key=lambda c: (-c.score, c.path))
    return scored[:k]


def shortlist_table(
    src_fields: list[SourceField],
    dest_fields: list[DestField],
    knowledge: KnowledgePack | None = None,
    ref_collections: dict[str, str] | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
) -> dict[str, list[Candidate]]:
    pack = knowledge or load_knowledge()
    return {
        src.name: shortlist_field(
            src,
            dest_fields,
            pack,
            top_k=top_k,
            min_score=min_score,
            ref_collections=ref_collections,
        )
        for src in src_fields
    }


def retrieval_margin(candidates: list[Candidate], chosen_path: str | None) -> float:
    """How decisively the chosen candidate won its shortlist, in 0-1.

    Feeds the confidence blend: a field the model likes but that barely beat its
    runner-up should not report the same confidence as an uncontested match.
    """
    if not candidates or chosen_path is None:
        return 0.0
    chosen = next((c for c in candidates if c.path == chosen_path), None)
    if chosen is None:
        return 0.0
    others = [c.score for c in candidates if c.path != chosen_path]
    if not others:
        return min(1.0, chosen.score + 0.25)
    lead = chosen.score - max(others)
    # A 0.20 lead is treated as fully decisive; scale linearly below that.
    return max(0.0, min(1.0, chosen.score * 0.5 + min(lead, 0.20) / 0.20 * 0.5))
