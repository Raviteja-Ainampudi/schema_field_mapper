"""FastAPI application: same pipeline as the CLI, streamed to a browser.

Design notes worth stating:

* **One code path.** The API calls :class:`schema_mapper.pipeline.Pipeline`
  exactly as the CLI does. There is no separate "web" mapping logic that could
  drift from the artifact the CLI produces.
* **Streaming over polling.** A run makes a dozen sequential model calls and
  takes tens of seconds, so progress is pushed as Server-Sent Events. The
  pipeline is synchronous, so it runs on a worker thread and publishes events
  through a queue; the request handler only forwards them.
* **POST for the stream.** ``EventSource`` is GET-only and would force run state
  onto the server. Streaming the response to a POST keeps each run
  self-contained and lets the browser send its full configuration.
* **Lambda-safe.** No module-level writes, artifacts go through
  ``config.output_dir()`` (``/tmp`` under Lambda), and the run history is a
  bounded in-memory map with optional S3 persistence.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from schema_mapper.bedrock import (
    BedrockClient,
    BedrockError,
    CassetteMissing,
    CassetteStore,
    OfflineClient,
)
from schema_mapper.candidates import shortlist_field
from schema_mapper.config import (
    ASSET_ROOT,
    CASSETTE_DIR,
    DATA_DIR,
    DEFAULT_DESTINATION_SCHEMA,
    DEFAULT_SOURCE_SCHEMA,
    MODEL_REGISTRY,
    THRESHOLDS,
    Settings,
    in_lambda,
    load_settings,
    output_dir,
    spec_for,
)
from schema_mapper.cost import BudgetExceeded, CostLedger
from schema_mapper.knowledge import load_knowledge
from schema_mapper.models import mapping_json_schema
from schema_mapper.normalize import (
    SchemaParseError,
    detect_format,
    load_destination,
    load_destination_file,
    load_source,
    load_source_file,
)
from schema_mapper.pairing import assess_pair
from schema_mapper.pipeline import Pipeline
from schema_mapper.transforms import apply_transform, build_document

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SAMPLES_DIR = DATA_DIR / "samples"
MAX_HISTORY = 25
MAX_UPLOAD_CHARS = 200_000

app = FastAPI(
    title="Schema Field Mapper",
    description="Maps every field of a legacy MySQL schema to a MongoDB destination schema.",
    version="1.0.0",
)

# Bounded history so a long-lived container cannot grow without limit.
RUNS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def require_token(supplied: str | None) -> None:
    """Shared-secret gate, active only when APP_ACCESS_TOKEN is configured.

    A public Function URL that runs paid model calls needs *some* barrier. This
    is deliberately minimal: it protects a demo link, and is not a user system.
    """
    expected = load_settings().access_token
    if not expected or expected == "change-me-for-shared-demo":
        return
    if supplied != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid access token.")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    source_text: str | None = Field(
        None, description="Raw source schema. Falls back to the bundled legacy_hrm schema."
    )
    destination_text: str | None = Field(
        None, description="Raw destination schema. Falls back to bundled people_platform."
    )
    router_model: str | None = None
    mapper_model: str | None = None
    cheap_mapper_model: str | None = None
    enable_cascade: bool = True
    enable_reflection: bool = True
    enable_cache: bool = True
    offline: bool = False
    batch_size: int = Field(THRESHOLDS.batch_size, ge=1, le=20)
    top_k: int = Field(THRESHOLDS.top_k, ge=2, le=15)


class ParseRequest(BaseModel):
    """Dry-run a pasted or uploaded schema without spending a model call."""

    source_text: str | None = None
    destination_text: str | None = None


class PreviewRequest(BaseModel):
    """Run the mapped transforms against one real source row."""

    table: str = "emp_master"
    collection: str = "employees"
    row: dict[str, Any]
    mappings: list[dict[str, str]]


# ---------------------------------------------------------------------------
# Schema loading helpers
# ---------------------------------------------------------------------------


def _load_schemas(request: RunRequest):
    try:
        if request.source_text and request.source_text.strip():
            if len(request.source_text) > MAX_UPLOAD_CHARS:
                raise HTTPException(status_code=413, detail="Source schema is too large.")
            source = load_source(request.source_text)
        else:
            source = load_source_file(DEFAULT_SOURCE_SCHEMA)

        if request.destination_text and request.destination_text.strip():
            if len(request.destination_text) > MAX_UPLOAD_CHARS:
                raise HTTPException(status_code=413, detail="Destination schema is too large.")
            destination = load_destination(request.destination_text)
        else:
            destination = load_destination_file(DEFAULT_DESTINATION_SCHEMA)
    except SchemaParseError as exc:
        # A parse failure is the user's most likely mistake, so say exactly what
        # was wrong and which formats are accepted.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return source, destination


def _build_settings(request: RunRequest) -> Settings:
    settings = load_settings()
    if request.router_model:
        settings.router_model = request.router_model
    if request.mapper_model:
        settings.mapper_model = request.mapper_model
    if request.cheap_mapper_model:
        settings.cheap_mapper_model = request.cheap_mapper_model
    settings.enable_cascade = request.enable_cascade
    settings.enable_reflection = request.enable_reflection
    settings.enable_cache = request.enable_cache

    unknown = [
        model
        for model in (settings.router_model, settings.mapper_model, settings.cheap_mapper_model)
        if model not in MODEL_REGISTRY
    ]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model id(s): {', '.join(unknown)}. See GET /api/models.",
        )
    return settings


# ---------------------------------------------------------------------------
# Static and metadata endpoints
# ---------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    target = STATIC_DIR / "index.html"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="UI assets are not bundled in this build.")
    return FileResponse(target)


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = load_settings()
    cassettes = list(CASSETTE_DIR.glob("*.json")) if CASSETTE_DIR.is_dir() else []
    return {
        "status": "ok",
        "lambda": in_lambda(),
        "region": settings.region,
        "offline_available": bool(cassettes),
        "cassette_count": len(cassettes),
        "auth_required": bool(
            settings.access_token and settings.access_token != "change-me-for-shared-demo"
        ),
        "defaults": {
            "router_model": settings.router_model,
            "mapper_model": settings.mapper_model,
            "cheap_mapper_model": settings.cheap_mapper_model,
            "cascade": settings.enable_cascade,
            "reflection": settings.enable_reflection,
        },
        "thresholds": {
            "high_confidence": THRESHOLDS.high_confidence,
            "medium_confidence": THRESHOLDS.medium_confidence,
            "escalate_below": THRESHOLDS.escalate_below,
            "reflect_below": THRESHOLDS.reflect_below,
            "top_k": THRESHOLDS.top_k,
            "batch_size": THRESHOLDS.batch_size,
            "model_weight": THRESHOLDS.model_weight,
            "retrieval_weight": THRESHOLDS.retrieval_weight,
            "manual_transform_cap": THRESHOLDS.manual_transform_cap,
        },
        "runs_in_memory": len(RUNS),
    }


@app.get("/api/models")
def models() -> dict[str, Any]:
    """The registry with prices, so the UI can show cost before a run starts."""
    return {
        "models": [
            {
                "id": spec.id,
                "label": spec.label,
                "family": spec.family,
                "tier": spec.tier,
                "input_per_mtok": spec.input_per_mtok,
                "output_per_mtok": spec.output_per_mtok,
            }
            for spec in MODEL_REGISTRY.values()
            if spec.tier != "embedding"
        ]
    }


@app.get("/api/schemas")
def schemas() -> dict[str, Any]:
    """Bundled schemas and sample inputs, so the UI needs nothing pasted to start."""
    samples = []
    if SAMPLES_DIR.is_dir():
        for path in sorted(SAMPLES_DIR.glob("*")):
            if path.suffix.lower() not in {".json", ".sql"}:
                continue
            if path.name.endswith("_rows.json"):
                continue  # row data for the transform preview, not a schema
            name = path.name.lower()
            kind = "source" if ("mysql" in name or "ddl" in name) else "destination"
            samples.append(
                {
                    "name": path.name,
                    "kind": kind,
                    "bytes": path.stat().st_size,
                }
            )
    return {
        "default_source": DEFAULT_SOURCE_SCHEMA.read_text(encoding="utf-8"),
        "default_destination": DEFAULT_DESTINATION_SCHEMA.read_text(encoding="utf-8"),
        "samples": samples,
        "accepted_formats": [
            "MySQL schema as JSON",
            "MySQL CREATE TABLE statements",
            "MongoDB schema as JSON",
            "MongoDB sample documents (Extended JSON)",
        ],
    }


@app.get("/api/samples/{name}")
def sample(name: str) -> dict[str, str]:
    target = (SAMPLES_DIR / name).resolve()
    # Contain the read to the samples directory regardless of the name given.
    if not str(target).startswith(str(SAMPLES_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail=f"No sample named {name}.")
    return {"name": name, "text": target.read_text(encoding="utf-8")}


@app.post("/api/parse")
def parse(request: ParseRequest) -> dict[str, Any]:
    """Validate schema text and report what was understood.

    Free and model-free: the UI calls this as the user pastes or uploads a file,
    so a format mistake surfaces before a paid run rather than as a failed one.
    """

    resolved: dict[str, Any] = {}

    def summarize(text: str | None, kind: str) -> dict[str, Any]:
        if not text or not text.strip():
            # The run will use the bundled schema, so assess that one for pairing.
            resolved[kind] = (
                load_source_file(DEFAULT_SOURCE_SCHEMA)
                if kind == "source"
                else load_destination_file(DEFAULT_DESTINATION_SCHEMA)
            )
            return {"ok": True, "used": "bundled default", "chars": 0}
        if len(text) > MAX_UPLOAD_CHARS:
            return {
                "ok": False,
                "error": f"Too large: {len(text)} characters, limit is {MAX_UPLOAD_CHARS}.",
                "chars": len(text),
            }
        try:
            detected = detect_format(text)
            if kind == "source":
                source_schema = load_source(text)
                resolved[kind] = source_schema
                containers = {t: len(source_schema.table(t)) for t in source_schema.table_names}
                database, dialect, count = (
                    source_schema.database,
                    source_schema.dialect,
                    source_schema.field_count,
                )
            else:
                dest_schema = load_destination(text)
                resolved[kind] = dest_schema
                containers = {
                    c: len(dest_schema.collection(c)) for c in dest_schema.collection_names
                }
                database, dialect, count = (
                    dest_schema.database,
                    dest_schema.dialect,
                    dest_schema.field_count,
                )
            return {
                "ok": True,
                "used": "pasted",
                "chars": len(text),
                "database": database,
                "dialect": dialect,
                "format": detected,
                "containers": containers,
                "fields": count,
            }
        except SchemaParseError as exc:
            return {"ok": False, "error": str(exc), "chars": len(text)}

    source = summarize(request.source_text, "source")
    destination = summarize(request.destination_text, "destination")

    # Whether the two halves belong together is a separate question from whether
    # each parses, and it is the one that silently wasted a run: pairing an HR
    # destination with a library source maps books onto departments rather than
    # failing. Deterministic, so it costs nothing to answer on every keystroke.
    pairing = None
    if "source" in resolved and "destination" in resolved:
        pairing = assess_pair(resolved["source"], resolved["destination"]).as_dict()

    return {
        "ok": bool(source["ok"] and destination["ok"]),
        "source": source,
        "destination": destination,
        "pairing": pairing,
    }


@app.get("/api/contract")
def contract() -> dict[str, Any]:
    return mapping_json_schema()


# ---------------------------------------------------------------------------
# Candidate inspection (free, no model calls)
# ---------------------------------------------------------------------------


@app.get("/api/candidates")
def candidates(
    table: str = Query(...),
    field: str = Query(...),
    collection: str = Query(...),
    top_k: int = Query(THRESHOLDS.top_k, ge=1, le=20),
) -> dict[str, Any]:
    """Stage 2 scoring for one field against the bundled schemas.

    Costs nothing and needs no credentials, so the UI can show why a candidate
    ranked where it did without spending anything.
    """
    source = load_source_file(DEFAULT_SOURCE_SCHEMA)
    destination = load_destination_file(DEFAULT_DESTINATION_SCHEMA)
    src = next((f for f in source.table(table) if f.name == field), None)
    if src is None:
        raise HTTPException(status_code=404, detail=f"No source field {table}.{field}.")
    if collection not in destination.collections:
        raise HTTPException(status_code=404, detail=f"No destination collection {collection}.")

    shortlist = shortlist_field(
        src,
        destination.collection(collection),
        load_knowledge(),
        top_k=top_k,
        ref_collections={table: collection},
    )
    return {
        "source_field": src.describe(),
        "candidates": [
            {"path": c.path, "bson_type": c.field.bson_type, **c.scores.as_dict()}
            for c in shortlist
        ],
    }


# ---------------------------------------------------------------------------
# Transform preview
# ---------------------------------------------------------------------------


@app.post("/api/preview")
def preview(request: PreviewRequest) -> dict[str, Any]:
    """Execute a mapping's transforms against one source row.

    Turns the mapping from a claim into something demonstrable: if a mapping says
    ``A -> active``, this shows it happening on real data.
    """
    source = load_source_file(DEFAULT_SOURCE_SCHEMA)
    destination = load_destination_file(DEFAULT_DESTINATION_SCHEMA)
    if request.table not in source.tables:
        raise HTTPException(status_code=404, detail=f"No source table {request.table}.")
    if request.collection not in destination.collections:
        raise HTTPException(status_code=404, detail=f"No collection {request.collection}.")

    src_fields = {f.name: f for f in source.table(request.table)}
    dest_lookup = {f.path: f for f in destination.collection(request.collection)}
    pairs = [
        (m["source_field"], m["destination_field"])
        for m in request.mappings
        if m.get("source_field") and m.get("destination_field")
    ]
    document, annotations = build_document(request.row, src_fields, pairs, dest_lookup)
    return {
        "document": document,
        "annotations": {
            path: {"rule": result.rule, "manual": result.manual, "detail": result.detail}
            for path, result in annotations.items()
        },
        "manual_count": sum(1 for r in annotations.values() if r.manual),
    }


@app.get("/api/sample_rows")
def sample_rows(table: str = Query("emp_master")) -> dict[str, Any]:
    path = SAMPLES_DIR / f"{table}_rows.json"
    if not path.is_file():
        return {"table": table, "rows": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {"table": table, "rows": payload.get(table, [])}


# ---------------------------------------------------------------------------
# The run stream
# ---------------------------------------------------------------------------


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _remember(run_id: str, record: dict[str, Any]) -> None:
    RUNS[run_id] = record
    while len(RUNS) > MAX_HISTORY:
        RUNS.popitem(last=False)


def _persist(run_id: str, record: dict[str, Any]) -> None:
    """Write artifacts locally, and to S3 when a bucket is configured.

    Best effort by design: losing a copy of an artifact must not fail a run that
    already succeeded, but it must be logged rather than hidden.
    """
    try:
        directory = output_dir() / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mapping.json").write_text(
            json.dumps(record["mapping"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (directory / "report.json").write_text(
            json.dumps(record["report"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("could not write run artifacts for %s: %s", run_id, exc)

    bucket = load_settings().artifact_bucket
    if not bucket:
        return
    try:
        import boto3

        s3 = boto3.client("s3")
        for name, payload in (("mapping.json", record["mapping"]), ("report.json", record["report"])):
            s3.put_object(
                Bucket=bucket,
                Key=f"runs/{run_id}/{name}",
                Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
    except Exception as exc:  # noqa: BLE001 - persistence must not break a good run
        logger.warning("S3 persistence failed for run %s: %s", run_id, exc)


def _stream_run(request: RunRequest) -> Iterator[str]:
    source, destination = _load_schemas(request)
    settings = _build_settings(request)

    ledger = CostLedger(max_tokens=settings.max_tokens_per_run)
    if request.offline:
        client = OfflineClient(cassettes=CassetteStore(CASSETTE_DIR, record=False), ledger=ledger)
    else:
        client = BedrockClient(
            region=settings.region,
            ledger=ledger,
            cassettes=CassetteStore(CASSETTE_DIR, record=False),
            use_cache=settings.enable_cache,
        )

    events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    outcome: dict[str, Any] = {}

    pipeline = Pipeline(
        client=client,
        source=source,
        destination=destination,
        settings=settings,
        knowledge=load_knowledge(),
        progress=events.put,
        raw_schema_chars=len(request.source_text or "") + len(request.destination_text or ""),
    )
    pipeline.tools.batch_size = request.batch_size
    pipeline.tools.top_k = request.top_k

    def worker() -> None:
        try:
            result = pipeline.run()
            outcome["result"] = result
        except (BedrockError, CassetteMissing, BudgetExceeded) as exc:
            outcome["error"] = str(exc)
            outcome["error_kind"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            logger.exception("pipeline failed")
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            outcome["error_kind"] = type(exc).__name__
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, name="pipeline", daemon=True)
    thread.start()

    yield _sse(
        "hello",
        {
            "source": {
                "database": source.database,
                "tables": {t: [f.name for f in source.table(t)] for t in source.table_names},
                "fields": source.field_count,
                "descriptors": {
                    f"{f.table}.{f.name}": f.describe() for f in source.fields()
                },
            },
            "destination": {
                "database": destination.database,
                "collections": {
                    c: [f.path for f in destination.collection(c)]
                    for c in destination.collection_names
                },
                "leaf_paths": destination.field_count,
                "types": {
                    f"{f.collection}.{f.path}": f.bson_type for f in destination.fields()
                },
            },
            "models": {
                "router": spec_for(settings.router_model).label,
                "mapper": spec_for(settings.mapper_model).label,
                "cheap_mapper": spec_for(settings.cheap_mapper_model).label,
            },
            "mode": "offline" if request.offline else "live",
        },
    )

    # Forward progress until the worker signals completion.
    while True:
        try:
            event = events.get(timeout=120)
        except queue.Empty:
            yield _sse("error", {"message": "The run stalled with no progress for 120 seconds."})
            return
        if event is None:
            break
        yield _sse(event.get("type", "progress"), event)

    thread.join(timeout=5)

    if "error" in outcome:
        yield _sse("error", {"message": outcome["error"], "kind": outcome.get("error_kind")})
        return

    result = outcome["result"]
    record = {
        "run_id": result.report["run_id"],
        "created_at": result.document.generated_at,
        "mapping": result.document.to_json_dict(),
        "report": result.report,
        "trace": result.trace,
        "decisions": [d.as_dict() for d in result.decisions],
    }
    _remember(record["run_id"], record)
    _persist(record["run_id"], record)

    yield _sse(
        "result",
        {
            "run_id": record["run_id"],
            "mapping": record["mapping"],
            "report": record["report"],
            "decisions": record["decisions"],
        },
    )


@app.post("/api/run")
def run(request: RunRequest, x_access_token: str | None = Header(None)) -> StreamingResponse:
    require_token(x_access_token)
    return StreamingResponse(
        _stream_run(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Without this an intermediate proxy may buffer the whole stream and
            # the UI would sit silent until the run finishes.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    return {
        "runs": [
            {
                "run_id": run_id,
                "created_at": record["created_at"],
                "mode": record["report"]["mode"],
                "mapped": record["report"]["coverage"]["source_fields_mapped"],
                "total": record["report"]["coverage"]["source_fields_total"],
                "mean_confidence": record["report"]["quality"]["mean_confidence"],
                "usd": record["report"]["cost"].get("total_usd", 0.0),
                "models": record["report"]["models"]["labels"],
                "ok": record["report"]["diagnostics"]["ok"],
            }
            for run_id, record in reversed(RUNS.items())
        ]
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    record = RUNS.get(run_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Run {run_id} is not in this instance's memory; it may have been evicted.",
        )
    return record


@app.get("/api/runs/{run_id}/mapping.json")
def download_mapping(run_id: str) -> JSONResponse:
    record = RUNS.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found.")
    return JSONResponse(
        record["mapping"],
        headers={
            "Content-Disposition": f'attachment; filename="mapping_{run_id}.json"'
        },
    )


@app.get("/api/latest_artifact")
def latest_artifact() -> dict[str, Any]:
    """The committed mapping, so the UI has something real to show before a run.

    Two directories, in this order, because they are the same path only when
    running from a checkout. A local run writes to ``output_dir()``, so that wins
    and the page reflects the newest artifact. Under Lambda ``output_dir()`` is
    ``/tmp`` and is empty on every cold start, so the read-only copy baked into
    the image is what a first visitor sees - without this fallback the deployed
    page opens on an empty graph.
    """
    searched = [output_dir()]
    bundled = ASSET_ROOT / "outputs"
    if bundled.resolve() != output_dir().resolve():
        searched.append(bundled)

    for directory in searched:
        for candidate in sorted(directory.glob("mapping_*_to_*.json")):
            report_path = candidate.parent / "run_report.json"
            return {
                "mapping": json.loads(candidate.read_text(encoding="utf-8")),
                "report": (
                    json.loads(report_path.read_text(encoding="utf-8"))
                    if report_path.is_file()
                    else None
                ),
                "source": candidate.name,
            }
    return {"mapping": None, "report": None, "source": None}


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------
# Under AWS Lambda Web Adapter the container runs uvicorn and the adapter
# translates the invoke, so `app` is the only handle needed. Kept explicit so a
# Mangum-style handler can be swapped in without touching the app.

handler = app


def main() -> None:  # pragma: no cover - local convenience
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":  # pragma: no cover
    main()
