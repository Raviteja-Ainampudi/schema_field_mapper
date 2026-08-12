# Quickstart

Two paths: **offline** needs no AWS account and spends nothing, **live** calls Amazon
Bedrock. Start with offline — it reproduces the committed artifact exactly.

## 1. Create the environment

Python 3.12+ is required. A `.venv` built on Windows cannot be used from WSL or Linux
and vice versa; each script replaces a foreign venv rather than half-installing into it.

**Windows (PowerShell)**

```powershell
.\scripts\setup_venv.ps1
.\.venv\Scripts\Activate.ps1
```

**WSL / Linux / macOS**

```bash
bash scripts/setup_venv.sh
source .venv/bin/activate
```

Sanity check, which verifies dependencies and that every module imports:

```bash
bash scripts/dev.sh check
```

## 2. Run the pipeline offline

Replays recorded Bedrock exchanges from `tests/fixtures/cassettes/`. No credentials, no
network, no spend, and byte-identical output apart from the timestamp.

```bash
bash scripts/dev.sh offline          # or: python -m schema_mapper.cli --offline
```

You get a summary with coverage, confidence bands, the constraint proof, and cost, plus
three files in `outputs/`:

| File | Contents |
| --- | --- |
| `mapping_legacy_hrm_to_people_platform.json` | the deliverable |
| `run_report.json` | per-stage timings, tokens, USD, coverage, diagnostics |
| `prompt_trace.json` | every prompt sent, with its manifest |

Expect 33 of 34 source fields mapped, `dob` declared unmapped, and validation passing.

## 3. Open the interface

```bash
bash scripts/dev.sh api              # http://localhost:8000
```

The bundled schemas are already loaded, so press **Run pipeline** with **offline** ticked
and watch the wires resolve. `PORT=8010 bash scripts/dev.sh serve` runs it without
file-watching, which is what the container does.

Interactive API reference: <http://localhost:8000/docs>.

## 4. Optional: a live Bedrock run

Copy `.env.sample` to `.env` and fill in the AWS values. Never commit `.env`.

```bash
bash scripts/dev.sh bedrock          # verify credentials and per-model access first
bash scripts/dev.sh run              # live run, ~$0.04 with the default cascade
```

A live run needs `bedrock:InvokeModel` on the configured model IDs in `us-east-1`. Model
access is per-model in Bedrock, so `scripts/check_bedrock.py` probes each one with a
single-token call — a model can be listed and still return `AccessDeniedException`.

## 5. Run the tests

```bash
bash scripts/dev.sh test             # 245 tests, fully offline
bash scripts/dev.sh eval             # Stage 2 shortlist recall against the oracle
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `No venv found` | Run the setup script for the OS you are actually on. |
| `Found a Windows venv but running under Linux/WSL` | Run `bash scripts/setup_venv.sh`; venvs are not portable. |
| `Address already in use` | Another server holds the port. Use `PORT=8010 bash scripts/dev.sh serve`. |
| `CassetteMissing` on an offline run | You changed a prompt or the schemas, so the request hash no longer matches a recording. Re-record with `--record` against live Bedrock. |
| `AccessDeniedException` | Model access is not enabled for that model ID in this account and region. Run `bash scripts/dev.sh bedrock`. |
| Interface loads but stays empty | Check the browser console; the frontend loads Preact from a CDN and needs network access for that one file. |
