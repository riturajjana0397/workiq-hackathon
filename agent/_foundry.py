r"""Shared Azure AI Foundry chat-client factory.

Every Work IQ sub-agent (and the orchestrator) uses this to construct the
OpenAI-compatible client backed by the Foundry deployment. Auth is Entra ID
via `AzureCliCredential` — no API keys.
"""
from __future__ import annotations

import os

from openai import AsyncOpenAI
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider

from agent_framework.openai import OpenAIChatClient


FOUNDRY_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get(
    "AZURE_AI_FOUNDRY_ENDPOINT"
)
DEFAULT_DEPLOYMENT = os.environ.get("AZURE_AI_FOUNDRY_DEPLOYMENT", "gpt-4o-mini")


def build_chat_client(deployment: str | None = None) -> OpenAIChatClient:
    """Return an `OpenAIChatClient` targeting the Foundry deployment.

    Uses AzureCliCredential -> bearer token against `https://ai.azure.com/.default`.
    Tokens auto-refresh because the openai SDK accepts a callable for `api_key`.
    """
    if not FOUNDRY_ENDPOINT:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT (or AZURE_AI_FOUNDRY_ENDPOINT) is not set. "
            "Point it at your Foundry /openai/v1 endpoint."
        )
    credential = AzureCliCredential()
    token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
    openai_client = AsyncOpenAI(
        base_url=FOUNDRY_ENDPOINT,
        api_key=token_provider,  # type: ignore[arg-type]
    )
    return OpenAIChatClient(model=deployment or DEFAULT_DEPLOYMENT, async_client=openai_client)
