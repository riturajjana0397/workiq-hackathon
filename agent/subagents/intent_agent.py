r"""Intent Detection sub-agent — A2A JSON-RPC server on port 8930.

Role
----
First hop in the Work IQ orchestration chain. Classifies the incoming user turn
into a structured intent object that the orchestrator uses to decide whether to
invoke the Tool Planner or short-circuit with a scope-refusal.

Contract
--------
Input:  raw user question (text part of the A2A message)
Output: JSON string with the shape:
    {
      "intent": "retrieve" | "act" | "compound" | "refuse",
      "entities": { "tables": [...], "keywords": [...] },
      "confidence": 0.0 - 1.0,
      "refusal_reason": "..."   // only when intent == "refuse"
    }

No tools are wired in — this is a pure classifier and stays fast/cheap.

Run
---
  .\.venv\Scripts\python.exe agent\subagents\intent_agent.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow `python agent/subagents/intent_agent.py` from repo root without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_framework import RawAgent as ChatAgent  # noqa: E402
from opentelemetry import trace  # noqa: E402

from _foundry import build_chat_client  # noqa: E402
from a2a_serve import serve_forever  # noqa: E402
from telemetry import extract_usage, record_usage, setup_telemetry, span_context_attributes  # noqa: E402


HOST = os.environ.get("WORKIQ_INTENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("WORKIQ_INTENT_PORT", "8930"))


def _usage_from_maf(response) -> dict:
    """Convert MAF's AgentResponse.usage_details -> our token dict."""
    details = getattr(response, "usage_details", None)
    if details is None:
        return {}
    if not isinstance(details, dict):
        for attr in ("model_dump", "dict"):
            fn = getattr(details, attr, None)
            if callable(fn):
                try:
                    details = fn()
                    break
                except Exception:  # noqa: BLE001
                    return {}
        else:
            return {}
    out: dict = {}
    if details.get("input_token_count") is not None:
        out["prompt_tokens"] = int(details["input_token_count"])
    if details.get("output_token_count") is not None:
        out["completion_tokens"] = int(details["output_token_count"])
    if details.get("total_token_count") is not None:
        out["total_tokens"] = int(details["total_token_count"])
    elif "prompt_tokens" in out and "completion_tokens" in out:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out

INSTRUCTIONS = """\
You are the Work IQ Intent Detection sub-agent. Given a single user turn,
classify it and return ONLY a compact JSON object — no prose, no code fence.

Schema:
{
  "intent": "retrieve" | "act" | "compound" | "refuse",
  "entities": {"tables": [string], "keywords": [string]},
  "confidence": number between 0 and 1,
  "refusal_reason": string   // present only when intent == "refuse"
}

Intent definitions:
  - "retrieve"  : the user wants information grounded in this org's work
                  context (emails, meetings, Teams chats, files, people, or
                  Dataverse tables such as capa_tracker).
  - "act"       : the user wants to write / update / create / flag / escalate
                  something in a Dataverse table.
  - "compound"  : the turn contains both a retrieval and a write step
                  ("summarise the blockers AND open a risk item for each").
  - "refuse"    : the turn is clearly generic — general knowledge, coding,
                  math, trivia, translations, world facts, opinions, creative
                  writing — anything unrelated to this org's work context.

Rules:
  - Known tables today: ["capa_tracker"]. Fill entities.tables when a table is
    named or clearly implied; leave empty otherwise.
  - Populate entities.keywords with the 1-5 most salient nouns/phrases.
  - Ambiguity defaults to "retrieve" (never to "refuse").
  - Only set intent="refuse" for clearly out-of-scope requests. In that case
    set refusal_reason to a short phrase like "generic knowledge question".
  - Output MUST be valid JSON parseable by json.loads. No markdown.
"""


async def _setup():
    client = build_chat_client()
    agent = ChatAgent(client, instructions=INSTRUCTIONS, name="workiq-intent")
    telemetry = setup_telemetry("workiq-intent")

    async def handle(question: str, _meta: dict) -> dict:
        with telemetry.tracer.start_as_current_span(
            "workiq.subagent.intent",
            attributes=span_context_attributes(subagent="intent"),
        ) as span:
            response = await agent.run(question)
            text = (getattr(response, "text", None) or str(response)).strip()
            usage = record_usage(telemetry, response, span=span)
            if not usage:
                # Fallback: try to pull directly from MAF's usage_details.
                usage = _usage_from_maf(response)
            print(f"[workiq-intent] usage={usage}", file=sys.stderr)
            span.set_attribute("workiq.subagent", "intent")
        return {
            "response": text,
            "citations": [],
            "metadata": {"stage": "intent", "usage": usage, "subagent": "intent"},
        }

    return handle


def main() -> int:
    asyncio.run(
        serve_forever(
            host=HOST,
            port=PORT,
            agent_name="workiq-intent",
            agent_description=(
                "Work IQ Intent Detection sub-agent. Classifies a user turn "
                "into {retrieve|act|compound|refuse} with entities."
            ),
            skill_id="classify_intent",
            setup=_setup,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
