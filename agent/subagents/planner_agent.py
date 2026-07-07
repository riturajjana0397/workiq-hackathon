r"""Tool Planner sub-agent — A2A JSON-RPC server on port 8931.

Role
----
Second hop in the Work IQ orchestration chain. Owns every tool surface:
  * workiq-mcp  (spawned simulator/server.py) — fetch, create_entity, update_entity, ask_work_iq
  * workiq-a2a  (running simulator/a2a_server.py) — chat-style grounded answers

Given a user turn plus the intent classification from the Intent Detection
sub-agent (delivered as a `intent` field in the A2A message metadata, and
mirrored verbatim in the message body for LLM visibility), the planner
sequences the right tool calls and returns a *draft* answer together with the
raw citations array. Final formatting is the Citation Builder's job — this
sub-agent MUST NOT invent markdown links.

Run
---
  .\.venv\Scripts\python.exe agent\subagents\planner_agent.py

Depends on
----------
  Simulator A2A server running on port 8920 (WORKIQ_A2A_CARD).
  Local .venv at ../../.venv with the simulator's requirements installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from agent_framework import RawAgent as ChatAgent, MCPStdioTool  # noqa: E402
from agent_framework_a2a import A2AAgent  # noqa: E402
from opentelemetry import trace  # noqa: E402

from _foundry import build_chat_client  # noqa: E402
from a2a_serve import serve_forever  # noqa: E402
from telemetry import extract_usage, record_usage, setup_telemetry, span_context_attributes  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
MCP_SCRIPT = REPO_ROOT / "simulator" / "server.py"

HOST = os.environ.get("WORKIQ_PLANNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("WORKIQ_PLANNER_PORT", "8931"))

PERSONA = os.environ.get("WORKIQ_SIM_PERSONA", "quality_pm")
SCENARIO = os.environ.get("WORKIQ_SIM_SCENARIO", "scenarios/c1-northbridge")
A2A_CARD_URL = os.environ.get(
    "WORKIQ_A2A_CARD",
    "http://127.0.0.1:8920/.well-known/agent-card.json",
)


def _usage_from_maf(response) -> dict:
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
You are the Work IQ Tool Planner sub-agent. Another agent (Intent Detection)
has already classified the user turn. You will receive that classification
inside the request body as a JSON block prefixed with `intent:`. Use it to
plan the minimum set of tool calls needed, execute them, and produce a
grounded draft answer.

You have two tool surfaces, both backed by the Work IQ engine:

  * workiq-mcp  (Tools surface, low-level)
        - ask_work_iq(question)            -> a cited grounded answer
        - fetch(table, filter)             -> read rows from a table
        - create_entity(table, record)     -> insert a row (idempotent)
        - update_entity(table, id, patch)  -> patch an existing row

  * workiq-a2a  (Chat surface, remote sub-agent)
        - send a natural-language question; returns a finished, cited answer

Available tables: capa_tracker
  Fields: id, action, committee, owner, status, opened_date, due_date,
          past_due, acl.

Routing rules (driven by the intent classification):
  - intent == "retrieve"  -> prefer workiq-a2a for narrative summarisation,
                             workiq-mcp.fetch when the user wants raw rows.
  - intent == "act"       -> workiq-mcp.fetch first (find the target rows),
                             then update_entity / create_entity per row.
  - intent == "compound"  -> ask via workiq-a2a first, then execute writes
                             via workiq-mcp for each identified item.
  - intent == "refuse"    -> reply with EXACTLY this sentence and nothing else:
      I am an agent who helps bring context using organziational data like emails ,teams and messages .Please use another llm for getting answers to these generic questions

Action execution rules:
  - When the user asks to update, flag, escalate, or modify records, you MUST
    actually execute the writes — do NOT just describe what should be done.
  - Step 1: fetch("capa_tracker") to see current rows.
  - Step 2: identify rows matching the criteria.
  - Step 3: update_entity("capa_tracker", "<id>", {<patch>}) per row.
  - Step 4: list each row id and the fields you patched.

Output format (STRICT):
  Produce your draft answer as plain text WITHOUT markdown links. After the
  answer, emit a single fenced JSON block tagged `citations` with the raw
  citations you relied on. Example:

    <your draft answer text here>

    ```citations
    [
      {"id": "MTG-001", "title": "...", "url": "..."},
      {"id": "EML-004", "title": "...", "url": "..."}
    ]
    ```

  If a tool returned no citations, emit an empty array. NEVER invent
  citations. NEVER format citations as markdown links yourself — that is the
  Citation Builder sub-agent's job.

Honesty:
  - If a tool returns no data, say so plainly.
  - Surface any governance / "withheld" note verbatim.
"""


CITATION_BLOCK = re.compile(r"```citations\s*(\[[\s\S]*?\])\s*```", re.IGNORECASE)


def _split_answer_and_citations(text: str) -> tuple[str, list]:
    """Peel the ```citations``` JSON block off the draft answer, if present."""
    match = CITATION_BLOCK.search(text)
    if not match:
        return text.strip(), []
    try:
        citations = json.loads(match.group(1))
        if not isinstance(citations, list):
            citations = []
    except json.JSONDecodeError:
        citations = []
    answer = (text[: match.start()] + text[match.end():]).strip()
    return answer, citations


def _build_mcp_tool(persona: str | None = None) -> MCPStdioTool:
    if not VENV_PY.exists():
        raise RuntimeError(f"Python interpreter not found at {VENV_PY}")
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
            "WORKIQ_SIM_PERSONA": persona or PERSONA,
            "WORKIQ_SIM_SCENARIO": SCENARIO,
        },
    )


def _build_simulator_a2a(persona: str | None = None) -> A2AAgent:
    base_url = A2A_CARD_URL.split("/.well-known/", 1)[0]
    headers = {"X-WorkIQ-Persona": persona or PERSONA}
    http_client = httpx.AsyncClient(headers=headers, timeout=60.0)
    return A2AAgent(
        name="workiq-a2a",
        description=(
            "Remote Work IQ chat agent (A2A). Send a question, receive a cited "
            "natural-language answer."
        ),
        url=base_url,
        http_client=http_client,
    )


async def _setup():
    client = build_chat_client()
    telemetry = setup_telemetry("workiq-planner")

    async def handle(question: str, meta: dict) -> dict:
        # The orchestrator passes intent JSON in the message body already; we
        # additionally accept it via metadata for programmatic callers.
        intent_meta = meta.get("intent") if isinstance(meta, dict) else None
        persona = str(meta.get("persona") or PERSONA).strip() if isinstance(meta, dict) else PERSONA
        if intent_meta:
            prompt = (
                f"intent: {json.dumps(intent_meta, separators=(',', ':'))}\n\n"
                f"user: {question}"
            )
        else:
            prompt = question
        mcp_tool = _build_mcp_tool(persona)
        simulator_a2a = _build_simulator_a2a(persona)
        async with mcp_tool, simulator_a2a:
            agent = ChatAgent(
                client,
                instructions=INSTRUCTIONS,
                name="workiq-planner",
                tools=[mcp_tool, simulator_a2a.as_tool()],
            )
            with telemetry.tracer.start_as_current_span(
                "workiq.subagent.planner",
                attributes=span_context_attributes(subagent="planner", persona=persona),
            ) as span:
                response = await agent.run(prompt)
                raw = (getattr(response, "text", None) or str(response)).strip()
                usage = record_usage(telemetry, response, span=span)
                if not usage:
                    usage = _usage_from_maf(response)
                print(f"[workiq-planner] usage={usage}", file=sys.stderr)
                span.set_attribute("workiq.subagent", "planner")
        answer, citations = _split_answer_and_citations(raw)
        return {
            "response": answer,
            "citations": citations,
            "metadata": {"stage": "planner", "usage": usage, "subagent": "planner"},
        }

    return handle


def main() -> int:
    asyncio.run(
        serve_forever(
            host=HOST,
            port=PORT,
            agent_name="workiq-planner",
            agent_description=(
                "Work IQ Tool Planner sub-agent. Sequences MCP + A2A calls, "
                "executes writes, and returns a draft answer + raw citations."
            ),
            skill_id="plan_and_execute",
            setup=_setup,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
