#!/usr/bin/env bash
# Build the container image, push it, and deploy the stack.
#
# Preflight first: every check here corresponds to a failure that otherwise shows
# up minutes into a build, or worse, as a deployed app that cannot call Bedrock.
#
# Usage:
#   bash scripts/deploy.sh                       # deploy with template defaults
#   BUDGET_EMAIL=me@example.com bash scripts/deploy.sh
#   SAM=/tmp/samvenv/bin/sam bash scripts/deploy.sh   # sam not on PATH
#
# Env:
#   SAM               path to the sam binary (default: sam)
#   STACK             stack name (default: from samconfig.toml)
#   BUDGET_EMAIL      enables the monthly budget alert
#   ACCESS_TOKEN      require X-Access-Token on /api/run (makes the UI read-only)
#   ARTIFACT_BUCKET   existing bucket to mirror run artifacts into
#   CONCURRENCY       reserved concurrency (default: template default of 5)
set -euo pipefail

cd "$(dirname "$0")/.."
SAM="${SAM:-sam}"

fail() { echo "error: $*" >&2; exit 1; }

echo "=== preflight"
command -v "$SAM" >/dev/null 2>&1 || fail "sam not found. Install the AWS SAM CLI, or set SAM=/path/to/sam"
command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
docker info >/dev/null 2>&1 || fail "the Docker daemon is not reachable; sam build needs it to build the image"

IDENTITY="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" ||
  fail "no usable AWS credentials; run 'aws configure' first"
REGION="$(aws configure get region || true)"
REGION="${REGION:-us-east-1}"
echo "  identity : $IDENTITY"
echo "  region   : $REGION"

# The models are cross-region inference profiles; being listed is not the same as
# being invocable, so this checks invocation the same way check_bedrock.py does.
echo "  bedrock  : verifying model access"
if ! aws bedrock list-inference-profiles --region "$REGION" >/dev/null 2>&1; then
  echo "  WARNING: cannot list Bedrock inference profiles in $REGION." >&2
  echo "           The app will deploy but every live run will fail. Run scripts/check_bedrock.py." >&2
fi

"$SAM" validate --lint >/dev/null || fail "template.yaml is not valid"
echo "  template : valid"

OVERRIDES=()
[ -n "${BUDGET_EMAIL:-}" ] && OVERRIDES+=("BudgetEmail=$BUDGET_EMAIL")
[ -n "${ACCESS_TOKEN:-}" ] && OVERRIDES+=("AccessToken=$ACCESS_TOKEN")
[ -n "${ARTIFACT_BUCKET:-}" ] && OVERRIDES+=("ArtifactBucket=$ARTIFACT_BUCKET")
[ -n "${CONCURRENCY:-}" ] && OVERRIDES+=("ReservedConcurrency=$CONCURRENCY")

echo
echo "=== build image"
"$SAM" build

echo
echo "=== deploy"
DEPLOY_ARGS=()
[ -n "${STACK:-}" ] && DEPLOY_ARGS+=(--stack-name "$STACK")
[ ${#OVERRIDES[@]} -gt 0 ] && DEPLOY_ARGS+=(--parameter-overrides "${OVERRIDES[@]}")
"$SAM" deploy "${DEPLOY_ARGS[@]}"

STACK_NAME="${STACK:-schema-field-mapper}"
URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" \
  --output text)"

echo
echo "=== deployed"
echo "  $URL"
echo
echo "Verify it, including that responses really stream:"
echo "  bash scripts/smoke_deployed.sh $URL"
echo
echo "Tear down when finished (this is what stops any further cost):"
echo "  $SAM delete --stack-name $STACK_NAME"
