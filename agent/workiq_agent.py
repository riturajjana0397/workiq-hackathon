r"""
Work IQ orchestrator — deterministic three-hop A2A pipeline.

Architecture
------------
This is a *thin* orchestrator. It owns no LLM calls of its own. Every turn is
a fixed A2A pipeline against three sub-agents, each speaking JSON-RPC 2.0 on
its own port:

    user question
         │
         ▼
    ┌────────────────────┐   A2A   ┌────────────────────┐
    │  Intent Detection  │◄────────┤    Orchestrator    │
    │  (port 8930)       │────────►│  (this file)       │
    └────────────────────┘         │                    │
                                   │  1. classify intent│
    ┌────────────────────┐   A2A   │  2. plan + execute │
    │   Tool Planner     │◄────────┤  3. format cites   │
    │   (port 8931)      │────────►│                    │
    │   owns MCP + sim   │         │                    │
    └────────────────────┘         │                    │
                                   │                    │
    ┌────────────────────┐   A2A   │                    │
    │  Citation Builder  │◄────────┤                    │
    │  (port 8932)       │────────►│                    │
    └────────────────────┘         └────────────────────┘

Each sub-agent is launched separately (see agent/README.md). This file is
the only entry point the user talks to.

Environment
-----------
  WORKIQ_INTENT_URL      default http://127.0.0.1:8930/a2a/
  WORKIQ_PLANNER_URL     default http://127.0.0.1:8931/a2a/
  WORKIQ_CITATION_URL    default http://127.0.0.1:8932/a2a/
  WORKIQ_SIM_PERSONA     persona forwarded to sub-agents (default quality_pm)

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
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any

import httpx

from telemetry import setup_telemetry, span_context_attributes


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- #
# Configuration                                                                #
# ---------------------------------------------------------------------------- #

PERSONA = os.environ.get("WORKIQ_SIM_PERSONA", "quality_pm")
SCENARIO = os.environ.get("WORKIQ_SIM_SCENARIO", "scenarios/c1-northbridge")
DEPLOYMENT = os.environ.get("AZURE_AI_FOUNDRY_DEPLOYMENT", "gpt-4o-mini")

INTENT_URL = os.environ.get("WORKIQ_INTENT_URL", "http://127.0.0.1:8930/a2a/")
PLANNER_URL = os.environ.get("WORKIQ_PLANNER_URL", "http://127.0.0.1:8931/a2a/")
CITATION_URL = os.environ.get("WORKIQ_CITATION_URL", "http://127.0.0.1:8932/a2a/")

REFUSAL_SENTENCE = (
    "I am an agent who helps bring context using organziational data like emails "
    ",teams and messages .Please use another llm for getting answers to these "
    "generic questions"
)

# Fallback: some intent-model replies wrap the JSON in fences or prose. Extract
# the first {...} block so a chatty classifier still parses.
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


# ---------------------------------------------------------------------------- #
# A2A JSON-RPC client                                                          #
# ---------------------------------------------------------------------------- #

async def _a2a_send(
    http: httpx.AsyncClient,
    url: str,
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """POST a single `SendMessage` to an A2A sub-agent and return the result dict.

    The wire format matches simulator/a2a_server.py and agent/a2a_serve.py.
    Uses the JSON-spec dialect (`kind` discriminators, role="user"); every Work
    IQ A2A endpoint accepts it.
    """
    message: dict[str, Any] = {
        "kind": "message",
        "role": "user",
        "messageId": f"msg-{uuid.uuid4().hex[:12]}",
        "parts": [{"kind": "text", "text": text}],
    }
    if metadata:
        message["metadata"] = metadata
    if context_id:
        message["contextId"] = context_id

    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {"message": message},
    }
    resp = await http.post(url, json=payload)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"A2A error {err.get('code')} from {url}: {err.get('message')}")
    result = body.get("result") or {}
    # Result may be a bare Message (JSON dialect) or {"message": ...} (proto dialect).
    if isinstance(result, dict) and "message" in result and isinstance(result["message"], dict):
        result = result["message"]
    return result


def _extract_text(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _extract_citations(message: dict[str, Any]) -> list[dict[str, Any]]:
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    cites = meta.get("citations")
    if isinstance(cites, list):
        return cites
    # Some sub-agents also mirror citations into a data part.
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("data"), dict):
            payload_cites = part["data"].get("citations")
            if isinstance(payload_cites, list):
                return payload_cites
    return []


def _extract_usage(message: dict[str, Any]) -> dict[str, int]:
    """Pull a {prompt_tokens, completion_tokens, total_tokens} dict from a reply."""
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    usage = meta.get("usage")
    if isinstance(usage, dict):
        # Coerce to ints; drop non-numeric values silently.
        clean: dict[str, int] = {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            try:
                if v is not None:
                    clean[k] = int(v)
            except (TypeError, ValueError):
                continue
        return clean
    return {}


def _add_usage(dst: dict[str, int], src: dict[str, int]) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if k in src:
            dst[k] = dst.get(k, 0) + src[k]


def _parse_intent(text: str) -> dict[str, Any]:
    """Parse the Intent Detection sub-agent's JSON reply defensively."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("intent classifier returned unparseable text: %r", text[:200])
    # Fail open — treat as retrieve so the user still gets an answer.
    return {"intent": "retrieve", "entities": {}, "confidence": 0.0}


# ---------------------------------------------------------------------------- #
# Orchestration pipeline                                                       #
# ---------------------------------------------------------------------------- #

async def orchestrate(
    http: httpx.AsyncClient,
    question: str,
    telemetry,
    *,
    persona: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Run one turn through Intent -> Planner -> Citation.

    Returns a dict with:
      - ``answer``: final user-facing text
      - ``usage``: aggregated token totals with a ``by_subagent`` breakdown
    """
    context_id = context_id or f"ctx-{uuid.uuid4().hex[:12]}"
    persona = persona or PERSONA
    meta = {"persona": persona}

    by_subagent: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}

    def _record(subagent: str, usage: dict[str, int], span) -> None:
        # Print to stderr so it's visible in the orchestrator/web console even
        # without OTel exporters configured. Helps diagnose empty-usage cases.
        print(f"[orchestrator] {subagent} usage={usage}", file=sys.stderr)
        if not usage:
            return
        by_subagent[subagent] = usage
        _add_usage(totals, usage)
        for k, v in usage.items():
            span.set_attribute(f"workiq.{subagent}.{k}", v)

    # 1) Intent Detection ----------------------------------------------------
    with telemetry.tracer.start_as_current_span(
        "workiq.orchestrator.intent",
        attributes=span_context_attributes(hop="intent", persona=persona),
    ) as span:
        intent_msg = await _a2a_send(
            http, INTENT_URL, question, metadata=meta, context_id=context_id
        )
        intent_text = _extract_text(intent_msg)
        intent = _parse_intent(intent_text)
        _record("intent", _extract_usage(intent_msg), span)
        span.set_attribute("workiq.intent.kind", str(intent.get("intent")))
        span.set_attribute("workiq.intent.confidence", float(intent.get("confidence") or 0))

    # Short-circuit on scope refusal — no need to spin up planner or citation.
    if intent.get("intent") == "refuse":
        return {
            "answer": REFUSAL_SENTENCE,
            "usage": {**totals, "by_subagent": by_subagent},
        }

    # 2) Tool Planner --------------------------------------------------------
    planner_prompt = (
        f"intent: {json.dumps(intent, separators=(',', ':'))}\n\nuser: {question}"
    )
    planner_meta = {**meta, "intent": intent}
    with telemetry.tracer.start_as_current_span(
        "workiq.orchestrator.planner",
        attributes=span_context_attributes(hop="planner", persona=persona),
    ) as span:
        planner_msg = await _a2a_send(
            http, PLANNER_URL, planner_prompt, metadata=planner_meta, context_id=context_id
        )
        draft = _extract_text(planner_msg)
        citations = _extract_citations(planner_msg)
        _record("planner", _extract_usage(planner_msg), span)
        span.set_attribute("workiq.planner.citations", len(citations))
        span.set_attribute("workiq.planner.draft_chars", len(draft))

    # If the planner already emitted the exact refusal sentence, don't re-format.
    if draft.strip() == REFUSAL_SENTENCE:
        return {
            "answer": REFUSAL_SENTENCE,
            "usage": {**totals, "by_subagent": by_subagent},
        }

    # 3) Citation Builder ----------------------------------------------------
    citation_payload = json.dumps({"draft": draft, "citations": citations})
    with telemetry.tracer.start_as_current_span(
        "workiq.orchestrator.citation",
        attributes=span_context_attributes(hop="citation", persona=persona),
    ) as span:
        cite_msg = await _a2a_send(
            http, CITATION_URL, citation_payload, metadata=meta, context_id=context_id
        )
        final = _extract_text(cite_msg)
        _record("citation", _extract_usage(cite_msg), span)
        span.set_attribute("workiq.citation.chars", len(final))

    # Roll totals up into the orchestrator's own counters as well so a single
    # backend query on `workiq_*_tokens_total` reflects the full pipeline.
    if "prompt_tokens" in totals:
        telemetry.prompt_tokens.add(totals["prompt_tokens"])
    if "completion_tokens" in totals:
        telemetry.completion_tokens.add(totals["completion_tokens"])
    if "total_tokens" in totals:
        telemetry.total_tokens.add(totals["total_tokens"])

    return {
        "answer": final or draft,
        "usage": {**totals, "by_subagent": by_subagent},
    }


# ---------------------------------------------------------------------------- #
# Entry points                                                                 #
# ---------------------------------------------------------------------------- #

async def _ask_and_print(http: httpx.AsyncClient, question: str, telemetry) -> None:
    logger.info("user submitted question: %s", question)
    started = time.perf_counter()
    with telemetry.tracer.start_as_current_span(
        "workiq.agent.turn",
        attributes=span_context_attributes(
            service="workiq-orchestrator",
            scenario=SCENARIO,
            persona=PERSONA,
            deployment=DEPLOYMENT,
            question=question,
            question_chars=len(question),
        ),
    ) as span:
        try:
            result = await orchestrate(http, question, telemetry)
            answer = result.get("answer", "") if isinstance(result, dict) else str(result)
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            print(answer)
            if usage:
                by = usage.get("by_subagent") or {}
                summary = (
                    f"[tokens] prompt={usage.get('prompt_tokens', 0)} "
                    f"completion={usage.get('completion_tokens', 0)} "
                    f"total={usage.get('total_tokens', 0)}"
                )
                if by:
                    parts = [
                        f"{name}={u.get('total_tokens', 0)}" for name, u in by.items()
                    ]
                    summary += "  (" + ", ".join(parts) + ")"
                print(summary, file=sys.stderr)
        except Exception:
            telemetry.failures.add(1)
            span.record_exception(sys.exc_info()[1])
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            telemetry.requests.add(1)
            telemetry.latency_ms.record(
                elapsed_ms,
                attributes=span_context_attributes(
                    service="workiq-orchestrator",
                    scenario=SCENARIO,
                    persona=PERSONA,
                    deployment=DEPLOYMENT,
                ),
            )
            span.set_attribute("workiq.request.duration_ms", elapsed_ms)


async def run(question: str | None) -> None:
    telemetry = setup_telemetry("workiq-agent")
    headers = {"X-WorkIQ-Persona": PERSONA}
    async with httpx.AsyncClient(headers=headers, timeout=180.0) as http:
        if question:
            await _ask_and_print(http, question, telemetry)
            return
        # REPL
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
            try:
                await _ask_and_print(http, user, telemetry)
            except Exception as e:  # noqa: BLE001 — REPL should survive one bad turn
                print(f"[orchestrator error] {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Work IQ orchestrator — three-hop A2A pipeline (intent -> planner -> citation)"
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
