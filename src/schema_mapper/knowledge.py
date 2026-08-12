"""Conventions knowledge pack: token expansion plus retrievable snippets.

Two responsibilities that both count as retrieval, at very different cost:

* **Token expansion** feeds Stage 2's deterministic scorer. This is not a nicety.
  ``dept_stat`` and ``isActive`` share no characters, let alone tokens, so
  without a synonym layer the correct destination never enters the shortlist and
  no amount of model quality can recover it.
* **Snippet retrieval** feeds Stage 3 prompts with three to five relevant facts
  (ISO standards, naming rules, denormalization guidance). It improves
  consistency and gives the model a real authority to cite in ``reasoning``.

Deliberately in-memory with lexical scoring. The corpus is a dozen snippets and
the schema is 74 fields; provisioning a vector store for this would cost more
per month than every run of this pipeline combined.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .config import KNOWLEDGE_DIR

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT_CHARS = re.compile(r"[^A-Za-z0-9]+")


def split_identifier(name: str) -> list[str]:
    """``fullName.firstName`` -> ``['full', 'name', 'first', 'name']``.

    Handles snake_case, camelCase, dots, and the leading underscore of ``_id``.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    parts = _SPLIT_CHARS.split(spaced)
    return [p.lower() for p in parts if p]


@dataclass(frozen=True)
class Snippet:
    id: str
    tags: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class TypeRule:
    source: str
    target: str
    render: str
    note: str | None = None


@dataclass
class TokenSet:
    """Tokens at two strengths, kept separate on purpose.

    ``core`` is what the identifier literally says once abbreviations are
    expanded. ``concepts`` adds synonyms. Mixing them would flatten the ranking:
    the synonym group for dates contains ``timestamp`` and ``at``, so
    ``created_ts`` and ``startDate`` would look equally similar to everything.
    Scoring weights core overlap heavily and concept overlap lightly.
    """

    core: frozenset[str] = frozenset()
    concepts: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.core or self.concepts)


@dataclass
class KnowledgePack:
    abbreviations: dict[str, list[str]] = field(default_factory=dict)
    field_aliases: dict[str, list[str]] = field(default_factory=dict)
    synonym_index: dict[str, frozenset[str]] = field(default_factory=dict)
    stopwords: frozenset[str] = frozenset()
    type_rules: list[TypeRule] = field(default_factory=list)
    snippets: list[Snippet] = field(default_factory=list)
    verified_overrides: list[dict[str, Any]] = field(default_factory=list)
    version: str = "1.0"

    # -- token expansion ---------------------------------------------------

    def tokenize(self, name: str) -> TokenSet:
        raw = split_identifier(name)

        core: set[str] = set()
        alias = self.field_aliases.get(name.lower())
        if alias:
            core.update(alias)

        for token in raw:
            if token in self.stopwords:
                continue
            expansion = self.abbreviations.get(token)
            if expansion:
                core.update(expansion)
            else:
                core.add(token)

        core = {t for t in core if t and t not in self.stopwords}

        concepts = set(core)
        for token in core:
            concepts |= self.synonym_index.get(token, frozenset())

        return TokenSet(core=frozenset(core), concepts=frozenset(concepts - core))

    # -- snippet retrieval -------------------------------------------------

    def retrieve(self, terms: Iterable[str], limit: int = 4) -> list[Snippet]:
        """Lexically score snippets against query terms; return the best few."""
        wanted = {t.lower() for t in terms if t}
        if not wanted:
            return []

        scored: list[tuple[float, Snippet]] = []
        for snippet in self.snippets:
            score = 0.0
            for tag in snippet.tags:
                tag_tokens = set(split_identifier(tag))
                if tag_tokens & wanted:
                    score += 2.0
            body = set(split_identifier(snippet.text))
            score += 0.35 * len(body & wanted)
            if score > 0:
                scored.append((score, snippet))

        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [snippet for _, snippet in scored[:limit]]

    def exemplars(self, table: str | None = None) -> list[dict[str, Any]]:
        """Human-verified mappings, retrieved as few-shot examples on later runs."""
        if table is None:
            return list(self.verified_overrides)
        return [o for o in self.verified_overrides if o.get("source_table") == table]

    def type_rule(self, predicate: str) -> TypeRule | None:
        for rule in self.type_rules:
            if rule.source == predicate:
                return rule
        return None


def _build_synonym_index(groups: list[list[str]]) -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for group in groups:
        members = {g.lower() for g in group}
        for member in members:
            index.setdefault(member, set()).update(members - {member})
    return {k: frozenset(v) for k, v in index.items()}


def load_knowledge_file(path: Path) -> KnowledgePack:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return KnowledgePack(
        abbreviations={
            k.lower(): split_identifier(v) for k, v in payload.get("abbreviations", {}).items()
        },
        field_aliases={
            k.lower(): split_identifier(v) for k, v in payload.get("field_aliases", {}).items()
        },
        synonym_index=_build_synonym_index(payload.get("synonyms", [])),
        stopwords=frozenset(w.lower() for w in payload.get("stopwords", [])),
        type_rules=[
            TypeRule(
                source=rule["from"],
                target=rule["to"],
                render=rule["render"],
                note=rule.get("note"),
            )
            for rule in payload.get("type_map", [])
        ],
        snippets=[
            Snippet(id=s["id"], tags=tuple(s.get("tags", [])), text=s["text"])
            for s in payload.get("snippets", [])
        ],
        verified_overrides=list(payload.get("verified_overrides", [])),
        version=str(payload.get("version", "1.0")),
    )


@lru_cache(maxsize=4)
def load_knowledge(path: str | None = None) -> KnowledgePack:
    target = Path(path) if path else KNOWLEDGE_DIR / "conventions.json"
    if not target.is_file():
        # A missing pack degrades scoring but must not break the pipeline.
        return KnowledgePack()
    return load_knowledge_file(target)
