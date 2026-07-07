r"""Citation Builder sub-agent — A2A JSON-RPC server on port 8932.

Role
----
Final hop in the Work IQ orchestration chain. Given the Tool Planner's draft
answer and the raw citations array, produce the polished user-facing markdown
with `[Title](url)` citation links appended under a "Citations:" heading.

This sub-agent has NO tools and is not allowed to invent claims or citations.
Its whole job is deterministic formatting; the LLM only helps with minor
prose smoothing.

Wire contract
-------------
The orchestrator sends a single A2A message whose text body is a JSON payload:

    {
      "draft":     "<planner's draft answer>",
      "citations": [ { "id": "...", "title": "...", "url": "..." }, ... ]
    }

Run
---
  .\.venv\Scripts\python.exe agent\subagents\citation_agent.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_framework import RawAgent as ChatAgent  # noqa: E402
from opentelemetry import trace  # noqa: E402

from _foundry import build_chat_client  # noqa: E402
from a2a_serve import serve_forever  # noqa: E402
from telemetry import extract_usage, record_usage, setup_telemetry, span_context_attributes  # noqa: E402


HOST = os.environ.get("WORKIQ_CITATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("WORKIQ_CITATION_PORT", "8932"))


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
You are the Work IQ Citation Builder sub-agent. You receive a JSON payload
with two fields:
  * draft:     the Tool Planner's answer text (no markdown links).
  * citations: an array of {id, title, url} objects.

Your job is to return the final user-facing answer. Rules:
  - Preserve the draft's factual content exactly. Do NOT add new claims. Do
    NOT remove existing claims. You may lightly smooth grammar and remove
    redundancy, nothing more.
  - Preserve the exact refusal sentence if the draft is a scope refusal.
  - If citations is non-empty, append a "Citations:" section at the very end
    formatted as a markdown bulleted list, one entry per citation, using
    `[Title](url)` links. If a citation has no URL, render as `Title (id)`.
  - If citations is empty, do NOT add a Citations section, and add a single
    line at the end: `_no citations available_`.
  - Never fabricate a URL, title, or id. Only use values from the citations
    array as-is.
  - Return ONLY the final markdown answer — no meta commentary, no code fence.
"""


def _deterministic_fallback(draft: str, citations: list[dict]) -> str:
    """Zero-LLM formatter used when the LLM output looks malformed."""
    parts = [draft.strip()]
    if citations:
        parts.append("\nCitations:")
        for c in citations:
            title = str(c.get("title") or c.get("id") or "source")
            url = c.get("url")
            cid = c.get("id")
            if url:
                parts.append(f"- [{title}]({url})")
            elif cid:
                parts.append(f"- {title} ({cid})")
            else:
                parts.append(f"- {title}")
    else:
        parts.append("\n_no citations available_")
    return "\n".join(parts).strip()


async def _setup():
    client = build_chat_client()
    agent = ChatAgent(client, instructions=INSTRUCTIONS, name="workiq-citation")
    telemetry = setup_telemetry("workiq-citation")

    async def handle(question: str, _meta: dict) -> dict:
        try:
            payload = json.loads(question)
            draft = str(payload.get("draft", "")).strip()
            citations = payload.get("citations") or []
            if not isinstance(citations, list):
                citations = []
        except json.JSONDecodeError:
            # Caller sent plain text; treat as a draft with no citations.
            draft, citations = question.strip(), []

        prompt = json.dumps({"draft": draft, "citations": citations}, indent=2)
        usage: dict = {}
        with telemetry.tracer.start_as_current_span(
            "workiq.subagent.citation",
            attributes=span_context_attributes(subagent="citation"),
        ) as span:
            try:
                response = await agent.run(prompt)
                final = (getattr(response, "text", None) or str(response)).strip()
                usage = record_usage(telemetry, response, span=span)
                if not usage:
                    usage = _usage_from_maf(response)
                if not final:
                    final = _deterministic_fallback(draft, citations)
            except Exception:  # noqa: BLE001 — never fail the user turn on formatting
                final = _deterministic_fallback(draft, citations)
            print(f"[workiq-citation] usage={usage}", file=sys.stderr)
            span.set_attribute("workiq.subagent", "citation")

        return {
            "response": final,
            "citations": citations,
            "metadata": {"stage": "citation", "usage": usage, "subagent": "citation"},
        }

    return handle


def main() -> int:
    asyncio.run(
        serve_forever(
            host=HOST,
            port=PORT,
            agent_name="workiq-citation",
            agent_description=(
                "Work IQ Citation Builder sub-agent. Formats the planner's "
                "draft answer with markdown citation links; never invents claims."
            ),
            skill_id="format_citations",
            setup=_setup,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
