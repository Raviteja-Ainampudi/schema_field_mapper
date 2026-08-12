"""Diagnose Bedrock access before spending time on a full run.

Answers three questions a failing run cannot distinguish between:
  1. Are credentials present and valid at all?
  2. Which of the configured model IDs exist in this region?
  3. Which of them is this account actually allowed to invoke?

Model access is per-account and per-region, so a model can exist and still
return AccessDeniedException. The only reliable check is a one-token call.

Usage:  python scripts/check_bedrock.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

ROLES = {
    "router": os.getenv("BEDROCK_ROUTER_MODEL", "us.amazon.nova-lite-v1:0"),
    "mapper": os.getenv("BEDROCK_MAPPER_MODEL", ""),
    "cheap_mapper": os.getenv("BEDROCK_CHEAP_MAPPER_MODEL", ""),
}

# Fallbacks worth probing when a configured ID turns out to be unavailable.
ALTERNATES = [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.amazon.nova-pro-v1:0",
    "amazon.nova-lite-v1:0",
    "amazon.nova-micro-v1:0",
]


def identity() -> bool:
    try:
        who = boto3.client("sts", region_name=REGION).get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        print(f"[FAIL] credentials: {type(exc).__name__}: {exc}")
        return False
    print(f"[ok]   credentials valid, account {who['Account']}, region {REGION}")
    return True


def catalogue() -> tuple[set[str], set[str]]:
    """Return (foundation model ids, inference profile ids) visible in region."""
    models: set[str] = set()
    profiles: set[str] = set()
    br = boto3.client("bedrock", region_name=REGION)
    try:
        for page in br.get_paginator("list_foundation_models").paginate():
            for m in page["modelSummaries"]:
                models.add(m["modelId"])
    except (ClientError, BotoCoreError) as exc:
        print(f"[warn] list_foundation_models: {type(exc).__name__}: {exc}")
    try:
        for page in br.get_paginator("list_inference_profiles").paginate():
            for p in page["inferenceProfileSummaries"]:
                profiles.add(p["inferenceProfileId"])
    except (ClientError, BotoCoreError) as exc:
        print(f"[warn] list_inference_profiles: {type(exc).__name__}: {exc}")
    print(f"[ok]   region catalogue: {len(models)} models, {len(profiles)} inference profiles")
    return models, profiles


def probe(model_id: str) -> tuple[bool, str]:
    """One minimal Converse call. Cheapest possible proof of invoke permission."""
    rt = boto3.client("bedrock-runtime", region_name=REGION)
    try:
        resp = rt.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with the single word: ok"}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0.0},
        )
        usage = resp.get("usage", {})
        return True, f"in={usage.get('inputTokens')} out={usage.get('outputTokens')}"
    except ClientError as exc:
        return False, exc.response.get("Error", {}).get("Code", "ClientError")
    except BotoCoreError as exc:
        return False, type(exc).__name__


def main() -> int:
    if not identity():
        print("\nFix credentials in .env, or run the pipeline with --offline.")
        return 1

    models, profiles = catalogue()
    known = models | profiles

    to_probe: list[str] = []
    for role, model_id in ROLES.items():
        if not model_id:
            print(f"[warn] {role}: not configured in .env")
            continue
        listed = "listed" if model_id in known else "NOT LISTED in region"
        print(f"[..]   {role}: {model_id} ({listed})")
        to_probe.append(model_id)

    for alt in ALTERNATES:
        if alt not in to_probe:
            to_probe.append(alt)

    print("\nInvoke probe (a model can be listed and still be denied):")
    usable: list[str] = []
    for model_id in to_probe:
        ok, detail = probe(model_id)
        print(f"  {'PASS' if ok else 'fail'}  {model_id:<55} {detail}")
        if ok:
            usable.append(model_id)

    print("\nUsable models:")
    if not usable:
        print("  none. Enable model access in the Bedrock console:")
        print(f"  https://{REGION}.console.aws.amazon.com/bedrock/home?region={REGION}#/modelaccess")
        return 2
    for model_id in usable:
        print(f"  {model_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
