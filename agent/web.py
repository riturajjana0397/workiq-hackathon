r"""
Work IQ orchestrator — tiny web UI (FastAPI + inline HTML).

Runs the same agent as `workiq_agent.py` but behind an HTTP endpoint and serves
a one-page chat interface. The agent (and its MCP child process + A2A session)
is built ONCE at startup and reused across requests for low latency.

Run
---
  .\.venv\Scripts\python.exe -m pip install fastapi uvicorn
  # in another terminal: start the A2A side of the simulator
  .\.venv\Scripts\python.exe simulator\a2a_server.py
  # then start the web app
  .\.venv\Scripts\python.exe agent\web.py
  # open http://127.0.0.1:8000

Environment
-----------
Same as workiq_agent.py:
  AZURE_AI_FOUNDRY_ENDPOINT, AZURE_AI_FOUNDRY_DEPLOYMENT, AZURE_AI_FOUNDRY_API_VERSION,
  WORKIQ_SIM_PERSONA, WORKIQ_A2A_CARD
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Make the sibling workiq_agent module importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workiq_agent import (  # noqa: E402
    Agent,
    INSTRUCTIONS,
    PERSONA,
    SCENARIO,
    DEPLOYMENT,
    A2A_CARD_URL,
    MCP_SCRIPT,
    build_a2a_agent,
    build_chat_client,
    build_mcp_tool,
)
from telemetry import (
    record_usage,
    setup_telemetry,
    span_context_attributes,
)  # noqa: E402


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- #
# Lifespan: build the agent once, tear it down cleanly on shutdown.            #
# ---------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry = setup_telemetry("workiq-web")
    chat_client, credential = build_chat_client()
    mcp_tool = build_mcp_tool()
    a2a_agent = build_a2a_agent()

    # Manually enter the async contexts so the agent stays alive across requests.
    await mcp_tool.__aenter__()
    await a2a_agent.__aenter__()

    agent = Agent(
        client=chat_client,
        name="workiq-orchestrator",
        instructions=INSTRUCTIONS,
        tools=[mcp_tool, a2a_agent.as_tool()],
    )
    app.state.agent = agent
    app.state._mcp = mcp_tool
    app.state._a2a = a2a_agent
    app.state._credential = credential
    app.state._telemetry = telemetry

    try:
        yield
    finally:
        await a2a_agent.__aexit__(None, None, None)
        await mcp_tool.__aexit__(None, None, None)
        await credential.close()


app = FastAPI(title="Work IQ Orchestrator UI", lifespan=lifespan)


# ---------------------------------------------------------------------------- #
# API                                                                           #
# ---------------------------------------------------------------------------- #


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
async def ask(req: AskRequest, request: Request) -> dict:
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question is required")

    telemetry = request.app.state._telemetry
    started = time.perf_counter()
    try:
        with telemetry.tracer.start_as_current_span(
            "workiq.web.ask",
            attributes=span_context_attributes(
                service="workiq-web",
                scenario=SCENARIO,
                persona=PERSONA,
                deployment=DEPLOYMENT,
            question=q,
                question_chars=len(q),
            ),
        ) as span:
            response = await request.app.state.agent.run(q)
            usage = record_usage(telemetry, response, span)
            text = (
                getattr(response, "text", None)
                or getattr(response, "content", None)
                or str(response)
            )
            if usage:
                span.set_attribute("workiq.total_tokens", usage.get("total_tokens", 0))
            return {"answer": text}
    except HTTPException:
        raise
    except Exception as exc:
        telemetry.failures.add(1)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        telemetry.requests.add(1)
        telemetry.latency_ms.record(
            elapsed_ms,
            attributes=span_context_attributes(
                service="workiq-web",
                scenario=SCENARIO,
                persona=PERSONA,
                deployment=DEPLOYMENT,
            ),
        )


# ---------------------------------------------------------------------------- #
# UI                                                                            #
# ---------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Work IQ — Orchestrator</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- markdown rendering for the agent's reply -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 0;
    background: #0f1116; color: #e6e6e6;
    display: flex; flex-direction: column; height: 100vh;
  }
  header {
    padding: 12px 20px; background: #1a1d26; border-bottom: 1px solid #2a2d38;
  }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header .sub { font-size: 12px; color: #8a8f9c; margin-top: 2px; }
  #log {
    flex: 1; overflow-y: auto; padding: 20px; max-width: 900px;
    width: 100%; margin: 0 auto; box-sizing: border-box;
  }
  .msg { margin-bottom: 18px; line-height: 1.5; }
  .msg.user { color: #9cc7ff; }
  .msg.user::before { content: "you ▸ "; color: #5a6173; font-weight: 600; }
  .msg.agent { background: #161922; padding: 14px 18px; border-radius: 8px;
               border: 1px solid #242838; }
  .msg.agent::before { content: "work iq ▸ "; color: #5a6173; font-weight: 600;
                       display: block; margin-bottom: 6px; font-size: 12px; }
  .msg.error { color: #ff7a7a; }
  .msg pre, .msg code { background: #0c0e14; padding: 2px 6px; border-radius: 4px; }
  .msg pre { padding: 10px; overflow-x: auto; }
  .msg a { color: #6fb3ff; }
  .thinking { color: #5a6173; font-style: italic; }
  form {
    display: flex; gap: 8px; padding: 16px 20px; background: #161922;
    border-top: 1px solid #2a2d38; max-width: 900px; width: 100%;
    margin: 0 auto; box-sizing: border-box;
  }
  textarea {
    flex: 1; background: #0f1116; color: #e6e6e6;
    border: 1px solid #2a2d38; border-radius: 6px;
    padding: 10px; font: inherit; resize: none; min-height: 44px;
  }
  button {
    background: #2563eb; color: white; border: 0; border-radius: 6px;
    padding: 0 18px; font-weight: 600; cursor: pointer;
  }
  button:disabled { background: #3a3f4f; cursor: not-allowed; }
</style>
</head>
<body>
<header>
  <h1>Work IQ Orchestrator</h1>
  <div class="sub">NorthBridge Health Network</div>
</header>

<div id="log"></div>

<form id="form">
  <textarea id="q" placeholder="Ask something — e.g. 'what's blocking PPAP qualification?'" required></textarea>
  <button id="send" type="submit">Send</button>
</form>

<script>
const log  = document.getElementById('log');
const form = document.getElementById('form');
const q    = document.getElementById('q');
const send = document.getElementById('send');

function add(kind, html) {
  const div = document.createElement('div');
  div.className = 'msg ' + kind;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function escape(s) {
  return s.replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = q.value.trim();
  if (!text) return;

  add('user', escape(text));
  q.value = '';
  send.disabled = true;
  const placeholder = add('agent thinking', 'thinking…');

  try {
    const r = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text }),
    });
    const raw = await r.text();
    let data = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      data = null;
    }
    if (!r.ok) {
      throw new Error((data && data.detail) || raw || ('HTTP ' + r.status));
    }
    placeholder.classList.remove('thinking');
    placeholder.innerHTML = marked.parse((data && data.answer) || raw || '(no answer)');
  } catch (err) {
    placeholder.remove();
    add('error', 'error: ' + escape(String(err.message || err)));
  } finally {
    send.disabled = false;
    q.focus();
  }
});

// submit on Enter, newline on Shift+Enter
q.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});
q.focus();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


# ---------------------------------------------------------------------------- #
# Entrypoint                                                                    #
# ---------------------------------------------------------------------------- #


def main() -> int:
    uvicorn.run(
        "web:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
