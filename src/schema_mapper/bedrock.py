"""Bedrock Converse client: constrained output, retries, cassettes, manifests.

Three responsibilities beyond "call the model":

* **Constrained decoding.** Requests carry a tool schema and ask the model to
  answer through it, so a structurally invalid response is largely impossible by
  construction rather than repaired after the fact. Models that reject a forced
  tool choice fall back to schema-in-prompt with tolerant JSON extraction.
* **Cassettes.** Every exchange is recorded keyed by a content hash of the
  request. Replaying them gives a reviewer with no AWS account a byte-identical
  run, and gives the test suite a fast offline fixture. The same hash is the
  response cache key, so caching and recording are one mechanism.
* **Constraint manifests.** Each request carries an accounting of what it was
  allowed to see (source table count, field count, destination path count).
  Those manifests are what `tests/test_constraint.py` asserts against, which is
  how "no prompt saw both schemas" becomes a test instead of a claim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import CASSETTE_DIR, cache_dir
from .cost import CostLedger

logger = logging.getLogger(__name__)

RETRYABLE = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelNotReadyException",
}


class BedrockError(RuntimeError):
    """Surfaced verbatim to the caller; never swallowed or retried blindly."""


class ModelAccessError(BedrockError):
    """The model exists but this account cannot invoke it."""


class CassetteMissing(BedrockError):
    """Offline replay was asked for an exchange that was never recorded."""


@dataclass
class PromptManifest:
    """What a single request was permitted to see.

    Recorded for every call so the constraint can be verified mechanically.
    """

    stage: str
    source_tables: list[str] = field(default_factory=list)
    source_fields: list[str] = field(default_factory=list)
    destination_collections: list[str] = field(default_factory=list)
    destination_paths: list[str] = field(default_factory=list)
    source_fingerprint: str = ""
    destination_fingerprint: str = ""
    # "names" means bare identifiers with no types, comments, keys, or structure;
    # "typed" means full field descriptors. The distinction matters because a list
    # of column names is not the schema - it cannot be mapped from on its own.
    detail_level: str = "typed"

    @property
    def source_field_count(self) -> int:
        return len(self.source_fields)

    @property
    def destination_path_count(self) -> int:
        return len(set(self.destination_paths))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_field_count"] = self.source_field_count
        data["destination_path_count"] = self.destination_path_count
        data["source_table_count"] = len(self.source_tables)
        return data


@dataclass
class LLMRequest:
    stage: str
    model_id: str
    user: str
    system: str = ""
    tool_name: str = "emit"
    tool_schema: dict[str, Any] | None = None
    max_tokens: int = 2000
    temperature: float = 0.0
    manifest: PromptManifest | None = None

    def cache_key(self) -> str:
        payload = json.dumps(
            {
                "model": self.model_id,
                "system": self.system,
                "user": self.user,
                "tool": self.tool_schema,
                "tool_name": self.tool_name,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def prompt_text(self) -> str:
        return f"{self.system}\n\n{self.user}".strip()

    def approx_tokens(self) -> int:
        # ~4 characters per token is close enough for a pre-call budget check.
        return len(self.prompt_text) // 4 + self.max_tokens


@dataclass
class LLMResponse:
    text: str
    data: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    model_id: str
    latency_ms: int
    stop_reason: str = ""
    cache_hit: bool = False
    source: str = "bedrock"

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "data": self.data,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_id": self.model_id,
            "latency_ms": self.latency_ms,
            "stop_reason": self.stop_reason,
        }


class LLMClient(Protocol):
    def invoke(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Recover a JSON object from free text.

    Only used when a model declines the tool path. Tries the whole string, then a
    fenced block, then the first balanced brace span.
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# ---------------------------------------------------------------------------
# Cassettes
# ---------------------------------------------------------------------------


class CassetteStore:
    """Content-addressed store of recorded exchanges.

    Doubles as the response cache: identical request, identical key, no spend.
    """

    def __init__(self, directory: Path | None = None, record: bool = False) -> None:
        self.directory = Path(directory) if directory else CASSETTE_DIR
        self.record = record
        if record:
            self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, request: LLMRequest) -> Path:
        return self.directory / f"{request.stage}-{request.cache_key()[:16]}.json"

    def load(self, request: LLMRequest) -> LLMResponse | None:
        path = self.path_for(request)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        response = payload["response"]
        return LLMResponse(
            text=response.get("text", ""),
            data=response.get("data"),
            input_tokens=int(response.get("input_tokens", 0)),
            output_tokens=int(response.get("output_tokens", 0)),
            model_id=response.get("model_id", request.model_id),
            latency_ms=int(response.get("latency_ms", 0)),
            stop_reason=response.get("stop_reason", ""),
            source="cassette",
        )

    def save(self, request: LLMRequest, response: LLMResponse) -> None:
        if not self.record:
            return
        payload = {
            "request": {
                "stage": request.stage,
                "model_id": request.model_id,
                "system": request.system,
                "user": request.user,
                "tool_name": request.tool_name,
                "tool_schema": request.tool_schema,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "manifest": request.manifest.as_dict() if request.manifest else None,
            },
            "response": response.as_dict(),
        }
        self.path_for(request).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class BaseClient:
    """Shared bookkeeping: ledger, cassettes, cache, and the prompt trace."""

    def __init__(
        self,
        ledger: CostLedger | None = None,
        cassettes: CassetteStore | None = None,
        use_cache: bool = True,
    ) -> None:
        self.ledger = ledger or CostLedger()
        self.cassettes = cassettes
        self.use_cache = use_cache
        self.trace: list[dict[str, Any]] = []
        self._memory_cache: dict[str, LLMResponse] = {}
        self._disk_cache = cache_dir() / "responses"

    # -- trace ------------------------------------------------------------

    def _log(self, request: LLMRequest, response: LLMResponse) -> None:
        self.trace.append(
            {
                "stage": request.stage,
                "model_id": response.model_id,
                "manifest": request.manifest.as_dict() if request.manifest else None,
                "system": request.system,
                "user": request.user,
                "response_text": response.text,
                "response_data": response.data,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
                "cache_hit": response.cache_hit,
                "source": response.source,
                "prompt_chars": len(request.prompt_text),
            }
        )
        self.ledger.record(
            stage=request.stage,
            model_id=response.model_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            cache_hit=response.cache_hit,
        )

    # -- cache ------------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        self._disk_cache.mkdir(parents=True, exist_ok=True)
        return self._disk_cache / f"{key[:24]}.json"

    def _cache_get(self, request: LLMRequest) -> LLMResponse | None:
        if not self.use_cache:
            return None
        key = request.cache_key()
        hit = self._memory_cache.get(key)
        if hit is None:
            path = self._cache_path(key)
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                hit = LLMResponse(**{**payload, "source": "cache"})
        if hit is None:
            return None
        return LLMResponse(**{**hit.__dict__, "cache_hit": True, "latency_ms": 0})

    def _cache_put(self, request: LLMRequest, response: LLMResponse) -> None:
        if not self.use_cache:
            return
        key = request.cache_key()
        self._memory_cache[key] = response
        try:
            self._cache_path(key).write_text(
                json.dumps(
                    {k: v for k, v in response.__dict__.items() if k not in {"cache_hit", "source"}},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - read-only filesystem
            logger.warning("response cache write failed: %s", exc)


class BedrockClient(BaseClient):
    """Live Amazon Bedrock via the Converse API."""

    def __init__(
        self,
        region: str = "us-east-1",
        ledger: CostLedger | None = None,
        cassettes: CassetteStore | None = None,
        use_cache: bool = True,
        max_attempts: int = 5,
        client: Any = None,
    ) -> None:
        super().__init__(ledger=ledger, cassettes=cassettes, use_cache=use_cache)
        self.region = region
        self.max_attempts = max_attempts
        self._client = client
        self._no_force_tool: set[str] = set()

    @property
    def runtime(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily so offline runs need no AWS SDK config

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def invoke(self, request: LLMRequest) -> LLMResponse:
        cached = self._cache_get(request)
        if cached is not None:
            self._log(request, cached)
            return cached

        self.ledger.check_budget(request.approx_tokens())
        response = self._invoke_with_retries(request)
        self._cache_put(request, response)
        if self.cassettes:
            self.cassettes.save(request, response)
        self._log(request, response)
        return response

    def _build_kwargs(self, request: LLMRequest, force_tool: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "modelId": request.model_id,
            "messages": [{"role": "user", "content": [{"text": request.user}]}],
            "inferenceConfig": {
                "maxTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        if request.system:
            kwargs["system"] = [{"text": request.system}]
        if request.tool_schema:
            kwargs["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": request.tool_name,
                            "description": "Return the result using this schema.",
                            "inputSchema": {"json": request.tool_schema},
                        }
                    }
                ]
            }
            if force_tool:
                kwargs["toolConfig"]["toolChoice"] = {"tool": {"name": request.tool_name}}
        return kwargs

    def _invoke_with_retries(self, request: LLMRequest) -> LLMResponse:
        from botocore.exceptions import ClientError

        force_tool = bool(request.tool_schema) and request.model_id not in self._no_force_tool
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            started = time.perf_counter()
            try:
                raw = self.runtime.converse(**self._build_kwargs(request, force_tool))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "ClientError")
                message = exc.response.get("Error", {}).get("Message", str(exc))

                if code == "AccessDeniedException":
                    raise ModelAccessError(
                        f"Model '{request.model_id}' is not enabled for this account in "
                        f"{self.region}. Enable it in the Bedrock console under Model access, "
                        "or choose a different model."
                    ) from exc

                if code == "ValidationException" and force_tool and "toolChoice" in message:
                    # Some model families reject a forced tool choice. Fall back
                    # to offering the tool without forcing it, for this model only.
                    logger.info("model %s rejects forced toolChoice; retrying", request.model_id)
                    self._no_force_tool.add(request.model_id)
                    force_tool = False
                    continue

                if code in RETRYABLE and attempt < self.max_attempts:
                    delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.4)
                    logger.warning(
                        "%s on %s (attempt %d/%d), retrying in %.1fs",
                        code,
                        request.stage,
                        attempt,
                        self.max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                    last_error = exc
                    continue

                raise BedrockError(f"{code} calling {request.model_id}: {message}") from exc

            latency_ms = int((time.perf_counter() - started) * 1000)
            return self._parse(request, raw, latency_ms)

        raise BedrockError(
            f"Exhausted {self.max_attempts} attempts on {request.stage}: {last_error}"
        )

    def _parse(self, request: LLMRequest, raw: dict[str, Any], latency_ms: int) -> LLMResponse:
        blocks = raw.get("output", {}).get("message", {}).get("content", [])
        text_parts: list[str] = []
        data: dict[str, Any] | None = None
        for block in blocks:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                data = block["toolUse"].get("input")

        text = "\n".join(text_parts).strip()
        if data is None and request.tool_schema and text:
            # The model answered in prose despite being offered a tool; recover
            # rather than fail, and let validation decide if the content is good.
            data = extract_json(text)

        usage = raw.get("usage", {})
        return LLMResponse(
            text=text,
            data=data,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            model_id=request.model_id,
            latency_ms=latency_ms,
            stop_reason=raw.get("stopReason", ""),
        )


class OfflineClient(BaseClient):
    """Replays recorded cassettes. No credentials, no network, no spend."""

    def __init__(
        self,
        cassettes: CassetteStore | None = None,
        ledger: CostLedger | None = None,
    ) -> None:
        super().__init__(ledger=ledger, cassettes=cassettes or CassetteStore(), use_cache=False)

    def invoke(self, request: LLMRequest) -> LLMResponse:
        assert self.cassettes is not None
        recorded = self.cassettes.load(request)
        if recorded is None:
            raise CassetteMissing(
                f"No cassette for stage '{request.stage}' (key {request.cache_key()[:16]}).\n"
                "Offline replay only reproduces recorded runs. Either run once with "
                "credentials and --record, or check that the schemas, models, and prompts "
                "match the recorded run exactly - any change to a prompt changes its key."
            )
        self._log(request, recorded)
        return recorded


class ScriptedClient(BaseClient):
    """Returns queued responses in order. For unit tests of pipeline logic."""

    def __init__(self, responses: list[dict[str, Any]], ledger: CostLedger | None = None) -> None:
        super().__init__(ledger=ledger, use_cache=False)
        self.queue = list(responses)
        self.requests: list[LLMRequest] = []

    def invoke(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"ScriptedClient exhausted at stage '{request.stage}'")
        payload = dict(self.queue.pop(0))
        input_tokens = int(payload.pop("_input_tokens", 100))
        output_tokens = int(payload.pop("_output_tokens", 50))
        response = LLMResponse(
            text=json.dumps(payload),
            data=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_id=request.model_id,
            latency_ms=1,
            source="scripted",
        )
        self._log(request, response)
        return response
