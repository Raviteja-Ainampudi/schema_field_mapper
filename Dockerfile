# Container image for AWS Lambda, and for running the same thing locally.
#
# Why a plain Python base rather than an AWS Lambda base image: the Lambda Web
# Adapter is an extension that speaks the Runtime API on the app's behalf, so the
# main process is just uvicorn. That means this exact image also runs on a laptop
# with `docker run` - the adapter does nothing outside Lambda - which makes the
# deployed artifact locally testable instead of only observable in production.
#
# Local:  docker build -t schema-field-mapper . && docker run --rm -p 8081:8080 schema-field-mapper
# Lambda: built and pushed by `sam deploy` (see template.yaml, docs/DEPLOY.md)

FROM python:3.12-slim

# The adapter binary. Pinned: it is the piece that decides whether responses
# stream, so it must not change under us on a rebuild.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /var/task

# Dependencies first, so editing application code does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code and the read-only assets the pipeline needs at runtime.
COPY src/ ./src/
COPY api/ ./api/
COPY data/ ./data/

# Cassettes make offline replay possible in the deployed app, which is how a
# visitor sees a full run for free. They live under tests/ because that is where
# they are recorded and asserted against; shipping them is deliberate, not an
# accident of copying the test tree.
COPY tests/fixtures/cassettes/ ./tests/fixtures/cassettes/

# The committed artifact, so the page opens on a real mapping. /tmp is empty on
# every cold start, so without this the first visitor sees an empty graph.
COPY outputs/mapping_legacy_hrm_to_people_platform.json outputs/run_report.json ./outputs/

ENV PYTHONPATH=/var/task/src:/var/task
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Port the adapter forwards to; AWS_LWA_PORT falls back to PORT.
ENV PORT=8080

# Must match InvokeMode on the function URL. The two modes use different payload
# formats, so a mismatch yields a response the browser cannot parse - which is
# why template.yaml sets RESPONSE_STREAM to match this.
ENV AWS_LWA_INVOKE_MODE=response_stream

# Readiness probe: the app's own health endpoint, not "/", which serves the SPA.
ENV AWS_LWA_READINESS_CHECK_PATH=/api/health
ENV AWS_LWA_READINESS_CHECK_HEALTHY_STATUS=200-399

EXPOSE 8080

# Single worker on purpose: one Lambda execution environment handles one request,
# and the in-memory run history would otherwise differ per worker.
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
