"""Runtime configuration, path resolution, model registry, and pinned thresholds.

Lambda compatibility is decided here rather than sprinkled through the code:

* The task filesystem is read-only except ``/tmp``, so every writable path is
  resolved through :func:`writable_dir` and lands under ``/tmp`` when running
  in Lambda. No module writes next to its source.
* Read-only assets (schemas, knowledge pack, cassettes) are located relative to
  this file, never relative to the working directory, because Lambda starts in
  ``/var/task`` and the CLI can be invoked from anywhere.
* Linux is case-sensitive while Windows is not, so asset filenames are lowercase
  and referenced with exactly the casing on disk. A mismatch that works locally
  would otherwise fail only after deploy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

load_dotenv()

PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent


def _find_asset_root() -> Path:
    """Locate the directory containing ``data/``.

    Two layouts must both work: the repo checkout (``<repo>/data``) and the
    container image, where ``data/`` is copied next to the package under
    ``/var/task``. An explicit override wins so tests can point elsewhere.
    """
    override = os.getenv("SCHEMA_MAPPER_ASSET_ROOT")
    if override:
        return Path(override).resolve()

    candidates = [
        PACKAGE_DIR.parent.parent,  # repo root: <repo>/src/schema_mapper -> <repo>
        PACKAGE_DIR.parent,  # src/ layout collapsed
        PACKAGE_DIR,  # data copied inside the package
        Path("/var/task"),  # Lambda task root
    ]
    for candidate in candidates:
        if (candidate / "data" / "schemas").is_dir():
            return candidate
    return PACKAGE_DIR.parent.parent


ASSET_ROOT: Final[Path] = _find_asset_root()
DATA_DIR: Final[Path] = ASSET_ROOT / "data"
SCHEMA_DIR: Final[Path] = DATA_DIR / "schemas"
SAMPLE_DIR: Final[Path] = DATA_DIR / "samples"
KNOWLEDGE_DIR: Final[Path] = DATA_DIR / "knowledge"

DEFAULT_SOURCE_SCHEMA: Final[Path] = SCHEMA_DIR / "legacy_hrm.mysql.json"
DEFAULT_DESTINATION_SCHEMA: Final[Path] = SCHEMA_DIR / "people_platform.mongo.json"

CASSETTE_DIR: Final[Path] = ASSET_ROOT / "tests" / "fixtures" / "cassettes"


def in_lambda() -> bool:
    return bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def writable_dir() -> Path:
    """Root for anything the process creates.

    ``/tmp`` in Lambda (the only writable location, and ephemeral per execution
    environment), the repo root locally so artifacts are easy to find.
    """
    override = os.getenv("SCHEMA_MAPPER_WORK_DIR")
    if override:
        root = Path(override)
    elif in_lambda():
        root = Path("/tmp/schema_mapper")  # noqa: S108 - the only writable path in Lambda
    else:
        root = ASSET_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def output_dir() -> Path:
    path = Path(os.getenv("SCHEMA_MAPPER_OUTPUT_DIR") or writable_dir() / "outputs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = Path(os.getenv("SCHEMA_MAPPER_CACHE_DIR") or writable_dir() / ".cache")
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# Model registry
# --------------------------------------------------------------------------
# Prices are us-east-1 on-demand USD per 1M tokens. They live in code so the
# cost ledger can price any run without a network call, and so a reviewer can
# see exactly what the reported dollar figures are derived from.


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    family: str
    tier: str
    input_per_mtok: float
    output_per_mtok: float
    supports_tool_use: bool = True


MODEL_REGISTRY: Final[dict[str, ModelSpec]] = {
    spec.id: spec
    for spec in [
        ModelSpec("us.amazon.nova-micro-v1:0", "Nova Micro", "nova", "cheap", 0.035, 0.14),
        ModelSpec("us.amazon.nova-lite-v1:0", "Nova Lite", "nova", "cheap", 0.06, 0.24),
        ModelSpec("us.amazon.nova-pro-v1:0", "Nova Pro", "nova", "mid", 0.80, 3.20),
        ModelSpec(
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "Claude Haiku 4.5",
            "claude",
            "cheap",
            1.00,
            5.00,
        ),
        ModelSpec(
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "Claude Sonnet 4.5",
            "claude",
            "strong",
            3.00,
            15.00,
        ),
        ModelSpec(
            "amazon.titan-embed-text-v2:0",
            "Titan Embeddings v2",
            "titan",
            "embedding",
            0.02,
            0.0,
            supports_tool_use=False,
        ),
    ]
}

UNKNOWN_MODEL = ModelSpec("unknown", "Unknown model", "unknown", "unknown", 0.0, 0.0)


def spec_for(model_id: str) -> ModelSpec:
    """Never raise on an unregistered model; report zero price instead.

    A model the registry does not know about must still be runnable - it just
    cannot be priced, and the run report says so rather than inventing a cost.
    """
    return MODEL_REGISTRY.get(model_id, UNKNOWN_MODEL)


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------
# One source of truth for every number the pipeline, the UI, and the write-up
# quote. Changing a band here changes it everywhere.


@dataclass(frozen=True)
class Thresholds:
    high_confidence: float = 0.90
    medium_confidence: float = 0.80
    escalate_below: float = 0.80
    reflect_below: float = 0.75
    collision_delta: float = 0.05
    top_k: int = 6
    min_candidate_score: float = 0.15
    batch_size: int = 8
    model_weight: float = 0.60
    retrieval_weight: float = 0.40
    type_mismatch_penalty: float = 0.05
    manual_transform_cap: float = 0.85

    def band(self, confidence: float) -> str:
        if confidence >= self.high_confidence:
            return "high"
        if confidence >= self.medium_confidence:
            return "medium"
        return "review"


THRESHOLDS: Final[Thresholds] = Thresholds()


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    router_model: str = field(
        default_factory=lambda: os.getenv("BEDROCK_ROUTER_MODEL") or "us.amazon.nova-lite-v1:0"
    )
    mapper_model: str = field(
        default_factory=lambda: os.getenv("BEDROCK_MAPPER_MODEL")
        or "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    cheap_mapper_model: str = field(
        default_factory=lambda: os.getenv("BEDROCK_CHEAP_MAPPER_MODEL")
        or "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("BEDROCK_EMBEDDING_MODEL") or "amazon.titan-embed-text-v2:0"
    )
    enable_embeddings: bool = field(default_factory=lambda: _flag("ENABLE_EMBEDDINGS", False))
    enable_cache: bool = field(default_factory=lambda: _flag("ENABLE_RESPONSE_CACHE", True))
    enable_cascade: bool = field(default_factory=lambda: _flag("ENABLE_CASCADE", True))
    enable_reflection: bool = field(default_factory=lambda: _flag("ENABLE_REFLECTION", True))
    max_tokens_per_run: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOKENS_PER_RUN", "120000"))
    )
    access_token: str = field(default_factory=lambda: os.getenv("APP_ACCESS_TOKEN", ""))
    artifact_bucket: str = field(default_factory=lambda: os.getenv("ARTIFACT_BUCKET", ""))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def mapper_chain(self) -> list[str]:
        """Models tried in order for field adjudication.

        With the cascade on, the cheap model answers first and only
        low-confidence fields reach the strong model.
        """
        if self.enable_cascade and self.cheap_mapper_model != self.mapper_model:
            return [self.cheap_mapper_model, self.mapper_model]
        return [self.mapper_model]


def load_settings() -> Settings:
    return Settings()


MAPPING_VERSION: Final[str] = "1.0"
SOURCE_LABEL: Final[str] = "legacy_hrm (MySQL)"
DESTINATION_LABEL: Final[str] = "people_platform (MongoDB)"
