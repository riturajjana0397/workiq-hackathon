r"""
Work IQ orchestrator agent — Microsoft Agent Framework + Azure AI Foundry.

What this does
--------------
1. Authenticates to your Azure AI Foundry project with DefaultAzureCredential
   (no API keys) and wires an OpenAI-compatible async client at the Foundry
   `/openai/v1` endpoint.
2. Builds a `ChatAgent` powered by that Foundry deployment.
3. Wires the local Work IQ simulator in as a tool surface over BOTH transports:
     * MCP (stdio) -> spawns `simulator/server.py` as a child process and
                      exposes its tools (ask_work_iq, fetch, create_entity,
                      update_entity) to the model.
     * A2A (HTTP)  -> wraps the running `simulator/a2a_server.py` as a remote
                      sub-agent the orchestrator can delegate chat-style
                      questions to.
4. The model decides which transport to use per turn; the same wiring will
   work against real Work IQ later — only the endpoint changes.

Prereqs
-------
  pip install agent-framework agent-framework-foundry agent-framework-a2a \
              azure-identity openai
  az login                                    # for DefaultAzureCredential

  # Start the A2A side of the simulator in a separate terminal:
  .\.venv\Scripts\python.exe simulator\a2a_server.py
  # (The MCP side is launched automatically by this script.)

Environment
-----------
  AZURE_AI_FOUNDRY_ENDPOINT     e.g. https://<resource>.services.ai.azure.com/openai/v1
  AZURE_AI_FOUNDRY_DEPLOYMENT   your model deployment name (default: gpt-4o-mini)
  WORKIQ_SIM_PERSONA            ops_director | quality_pm | credentialing_lead | vendor_liaison
  WORKIQ_A2A_CARD               default http://127.0.0.1:8920/.well-known/agent-card.json

Run
---
  # one-shot
  .\.venv\Scripts\python.exe agent\workiq_agent.py --ask "what is blocking PPAP qualification?"

  # interactive REPL
  .\.venv\Scripts\python.exe agent\workiq_agent.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider

# Microsoft Agent Framework imports — verified against the installed version.
from agent_framework import Agent, MCPStdioTool
from agent_framework.openai import OpenAIChatClient
from agent_framework_a2a import A2AAgent


# ---------------------------------------------------------------------------- #
# Configuration                                                                #
# ---------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
MCP_SCRIPT = REPO_ROOT / "simulator" / "server.py"

FOUNDRY_ENDPOINT = "https://aparnaram-foundry-subdomain.services.ai.azure.com/openai/v1"
DEPLOYMENT = os.environ.get("AZURE_AI_FOUNDRY_DEPLOYMENT", "gpt-4o-mini")
PERSONA = os.environ.get("WORKIQ_SIM_PERSONA", "quality_pm")
SCENARIO = "scenarios/c1-northbridge"
api_version = ""
A2A_CARD_URL = os.environ.get(
    "WORKIQ_A2A_CARD",
    "http://127.0.0.1:8920/.well-known/agent-card.json",
)

INSTRUCTIONS = """\
You are a Work IQ orchestrator. You answer questions about a program by grounding
every claim in the user's work context (emails, meetings, Teams chats, files,
people, and the Dataverse milestone tracker) using the tools you have been given.

You have two tool surfaces, both backed by the same Work IQ engine:

  * workiq-mcp  (Tools surface, low-level)
        - ask_work_iq(question)            -> a cited grounded answer
        - fetch(table, filter)             -> read rows from tables like
                                              "milestone_tracker"
        - create_entity(table, record)     -> insert a row (idempotent)
        - update_entity(table, id, patch)  -> patch an existing row

  * workiq-a2a  (Chat surface, remote sub-agent)
        - send a natural-language question; returns a finished, cited answer

Routing rules:
  - For natural-language analysis / summarisation, prefer workiq-a2a.
  - For data operations (read or write to tables), use workiq-mcp tools.
  - For compound tasks ("summarise the blockers AND open a risk item for each"),
    chain: ask via workiq-a2a, then call workiq-mcp.create_entity per blocker.

Honesty & governance:
  - Never invent facts. If a tool returns no citations, say so.
  - If the response includes a governance / "withheld" note, surface it verbatim;
    do not try to reason about the redacted content.
  - Always show the citation IDs (e.g. EML-001, MTG-002) you relied on.
"""


# ---------------------------------------------------------------------------- #
# Foundry client (Entra ID, no keys)                                           #
# ---------------------------------------------------------------------------- #

def build_chat_client() -> tuple[OpenAIChatClient, AzureCliCredential]:
    """Construct an OpenAI-compatible chat client backed by Azure AI Foundry.

    Uses AzureCliCredential explicitly (instead of DefaultAzureCredential) so the
    identity matches whatever `az account show` reports. DefaultAzureCredential
    walks a chain (env vars -> Workload Identity -> VS Code -> shared cache ->
    az CLI) and frequently returns a token from the WRONG tenant when any of
    the earlier sources are signed into a different tenant. Switching to
    AzureCliCredential makes the auth deterministic.

    The openai SDK accepts a callable for `api_key`; the token provider is
    invoked per request so tokens refresh automatically.
    """
    credential = AzureCliCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://ai.azure.com/.default"
    )
    openai_client = AsyncOpenAI(
        base_url=FOUNDRY_ENDPOINT,
        api_key=token_provider,  # type: ignore[arg-type]
    )
    chat_client = OpenAIChatClient(
        model=DEPLOYMENT,
        async_client=openai_client,
    )
    return chat_client, credential


# ---------------------------------------------------------------------------- #
# Tool surfaces                                                                #
# ---------------------------------------------------------------------------- #

def build_mcp_tool() -> MCPStdioTool:
    """Spawn the local Work IQ MCP server as a child process and surface its
    tools to the model. Persona is injected via env so RBAC kicks in.
    """
    if not VENV_PY.exists():
        raise RuntimeError(
            f"Python interpreter not found at {VENV_PY}. "
            "Activate or recreate the .venv first."
        )
    if not MCP_SCRIPT.exists():
        raise RuntimeError(f"MCP server script not found at {MCP_SCRIPT}")

    return MCPStdioTool(
        name="workiq-mcp",
        description=(
            "Local Work IQ simulator (MCP stdio). Tools: ask_work_iq, fetch, "
            "create_entity, update_entity."
        ),
        command=str(VENV_PY),
        args=[str(MCP_SCRIPT)],
        env={
            **os.environ,
            "WORKIQ_SIM_PERSONA": PERSONA,
            "WORKIQ_SIM_SCENARIO": SCENARIO,
        },
    )


def build_a2a_agent() -> A2AAgent:
    """Wrap the running Work IQ A2A server as a sub-agent the orchestrator can
    delegate Chat-style questions to. The agent card is auto-discovered from
    /.well-known/agent-card.json under the given base URL.
    """
    # A2AAgent wants the BASE URL of the remote agent (it appends the card path
    # and the /a2a/ endpoint itself). Derive it from WORKIQ_A2A_CARD by stripping
    # the well-known suffix.
    base_url = A2A_CARD_URL.split("/.well-known/", 1)[0]
    return A2AAgent(
        name="workiq-a2a",
        description=(
            "Remote Work IQ chat agent (A2A protocol). Send a question, "
            "receive a finished, cited natural-language answer."
        ),
        url=base_url,
    )


# ---------------------------------------------------------------------------- #
# Orchestration                                                                #
# ---------------------------------------------------------------------------- #

async def run(question: str | None) -> None:
    chat_client, credential = build_chat_client()

    mcp_tool = build_mcp_tool()
    a2a_agent = build_a2a_agent()

    # Both surfaces are async-context-managed: MCP starts the subprocess and
    # tears it down; A2A opens / closes the HTTP session and pulls the card.
    async with mcp_tool, a2a_agent:
        agent = Agent(
            client=chat_client,
            name="workiq-orchestrator",
            instructions=INSTRUCTIONS,
            tools=[mcp_tool, a2a_agent.as_tool()],
        )

        banner = (
            f"\nWork IQ orchestrator ready.\n"
            f"  Foundry deployment : {DEPLOYMENT}\n"
            f"  Persona            : {PERSONA}\n"
            f"  Scenario           : {SCENARIO}\n"
            f"  MCP child          : {MCP_SCRIPT.name}\n"
            f"  A2A card           : {A2A_CARD_URL}\n"
        )
        print(banner)

        if question:
            await _ask_and_print(agent, question)
            return

        # REPL
        print("Type a question, or '/quit' to exit.")
        while True:
            try:
                user = input("\nask> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                continue
            if user in ("/quit", "/exit"):
                break
            await _ask_and_print(agent, user)

    # azure.identity.aio credentials hold an aiohttp session.
    await credential.close()


async def _ask_and_print(agent: Agent, question: str) -> None:
    print("\n--- agent reasoning ---")
    response = await agent.run(question)
    # The framework returns a message-like object; render its text payload.
    text = getattr(response, "text", None) or getattr(response, "content", None) or str(response)
    print(text)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Work IQ orchestrator — Foundry LLM over MCP + A2A"
    )
    ap.add_argument("--ask", help="ask a single question and exit")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.ask))
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
