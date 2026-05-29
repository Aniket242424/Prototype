"""
Backend-agnostic LLM client factory.

Both AsyncAnthropic and AsyncAnthropicBedrock expose the identical
messages.create() API surface (including tool-use), so the only real
differences are construction args and model IDs.

Auth:
- direct Anthropic: ANTHROPIC_API_KEY env var
- Bedrock: standard boto3 credential chain (env vars on laptop, IAM role
  on EC2). Region from settings.aws_region.

Model IDs:
- direct Anthropic: settings.anthropic_model (e.g. claude-sonnet-4-6)
- Bedrock: settings.bedrock_model_id (e.g. global.anthropic.claude-haiku-4-5-20251001-v1:0)
"""
from __future__ import annotations

import os
from typing import Any

from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger

log = get_logger(__name__)


def get_llm_client(settings: AppSettings | None = None) -> Any:
    """
    Return an async Claude client matching the configured backend.

    Returns either AsyncAnthropic or AsyncAnthropicBedrock — both expose
    messages.create() with the same signature (model, max_tokens, system,
    messages, tools, ...).
    """
    settings = settings or get_settings()

    if settings.advisor_backend == "bedrock":
        from anthropic import AsyncAnthropicBedrock

        # Bedrock client picks up creds from boto3 default chain:
        #   1. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars
        #   2. ~/.aws/credentials profile
        #   3. EC2 instance IAM role (on AWS-hosted deployments)
        # We pass aws_region explicitly so it's clear in logs.
        log.info(
            "llm_client.bedrock",
            region=settings.aws_region,
            model_id=settings.bedrock_model_id,
            creds_source=_detect_aws_creds_source(),
        )
        return AsyncAnthropicBedrock(aws_region=settings.aws_region)

    # Default: direct Anthropic API
    from anthropic import AsyncAnthropic

    log.info(
        "llm_client.anthropic",
        model=settings.anthropic_model,
    )
    return AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


def get_model_id(settings: AppSettings | None = None) -> str:
    """Return the right model identifier for the active backend."""
    settings = settings or get_settings()
    if settings.advisor_backend == "bedrock":
        return settings.bedrock_model_id
    return settings.anthropic_model


def _detect_aws_creds_source() -> str:
    """For logs only — best-effort guess at where Bedrock creds will come from."""
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return "env_vars"
    if os.path.exists(os.path.expanduser("~/.aws/credentials")):
        return "shared_credentials_file"
    # If neither, boto3 will try EC2 metadata service / IAM role
    return "instance_role_or_unknown"
