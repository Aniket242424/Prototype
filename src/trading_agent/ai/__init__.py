"""
LLM client factory — backend-agnostic Claude access.

Selects between direct Anthropic API and AWS Bedrock based on
settings.advisor_backend. Returns clients with identical messages.create()
API surface, so call sites don't need to branch on backend.

Used by:
- Phase 7 pre-market agent (tool-use loop)
- Future Phase 4 advisor refactor (currently uses raw boto3 invoke_model)
"""
from trading_agent.ai.llm_client import (
    get_llm_client,
    get_model_id,
)

__all__ = ["get_llm_client", "get_model_id"]
