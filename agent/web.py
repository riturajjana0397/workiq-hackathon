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

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
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
    REPO_ROOT,
    build_a2a_agent,
    build_chat_client,
    build_mcp_tool,
)
from agent_framework import AgentSession  # noqa: E402

# Load scenario data for citation lookup
sys.path.insert(0, str(REPO_ROOT / "simulator"))
from engine import load_scenario  # noqa: E402

_scenario = load_scenario(REPO_ROOT / "simulator" / SCENARIO)



# ---------------------------------------------------------------------------- #
# Lifespan: build the agent once, tear it down cleanly on shutdown.            #
# ---------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    # One chat client shared by every persona's agent.
    app.state.chat_client = build_chat_client()
    # persona id -> {"agent", "mcp", "a2a"} built lazily on first use.
    app.state.agents = {}
    app.state.build_lock = asyncio.Lock()
    # session_key -> AgentSession for conversation memory
    app.state.sessions = {}

    # Warm up the default persona so the first request is fast.
    await get_persona_agent(app, PERSONA)

    # Pre-warm the remaining personas in the background so switching personas
    # never stalls a request on a cold start (each spawns its own MCP child).
    async def _prewarm():
        for p in _scenario.personas:
            pid = p["id"]
            if pid in app.state.agents:
                continue
            try:
                await get_persona_agent(app, pid)
            except Exception as exc:  # don't let one persona break the rest
                print(f"[workiq-web] prewarm failed for '{pid}': {exc}", file=sys.stderr)
    app.state._prewarm_task = asyncio.create_task(_prewarm())

    print(
        "[workiq-web] agent ready\n"
        f"  Foundry deployment : {DEPLOYMENT}\n"
        f"  Scenario           : {SCENARIO}\n"
        f"  Default persona    : {PERSONA}\n"
        f"  Personas available : {', '.join(p['id'] for p in _scenario.personas)}\n"
        f"  MCP child          : {MCP_SCRIPT.name}\n"
        f"  A2A card           : {A2A_CARD_URL}\n"
        "  Open               : http://127.0.0.1:8000",
        file=sys.stderr,
    )
    try:
        yield
    finally:
        for entry in app.state.agents.values():
            try:
                await entry["a2a"].__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await entry["mcp"].__aexit__(None, None, None)
            except Exception:
                pass


async def get_persona_agent(app: FastAPI, persona: str):
    """Return (building if needed) the cached agent stack for a persona.

    Each persona gets its own MCP child process and A2A session so the
    simulator applies that persona's RBAC. Stacks are cached for reuse.
    """
    valid = {p["id"] for p in _scenario.personas}
    if persona not in valid:
        persona = PERSONA

    cached = app.state.agents.get(persona)
    if cached:
        return cached["agent"]

    async with app.state.build_lock:
        # Re-check inside the lock in case another request built it.
        cached = app.state.agents.get(persona)
        if cached:
            return cached["agent"]

        mcp_tool = build_mcp_tool(persona)
        a2a_agent = build_a2a_agent(persona)
        await mcp_tool.__aenter__()
        await a2a_agent.__aenter__()

        agent = Agent(
            client=app.state.chat_client,
            name="workiq-orchestrator",
            instructions=INSTRUCTIONS,
            tools=[mcp_tool, a2a_agent.as_tool()],
        )
        app.state.agents[persona] = {
            "agent": agent,
            "mcp": mcp_tool,
            "a2a": a2a_agent,
        }
        print(f"[workiq-web] built agent for persona '{persona}'", file=sys.stderr)
        return agent


app = FastAPI(title="Work IQ Orchestrator UI", lifespan=lifespan)


# ---------------------------------------------------------------------------- #
# API                                                                           #
# ---------------------------------------------------------------------------- #

class AskRequest(BaseModel):
    question: str
    persona: str | None = None
    session_id: str | None = None


@app.get("/personas")
async def personas() -> dict:
    """List selectable personas (id + human label) for the UI dropdown."""
    return {
        "default": PERSONA,
        "personas": [
            {
                "id": p["id"],
                "label": p.get("label", p["id"]),
                "description": p.get("description", ""),
            }
            for p in _scenario.personas
        ],
    }


@app.post("/ask")
async def ask(req: AskRequest) -> dict:
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question is required")

    persona = (req.persona or PERSONA).strip()
    agent = await get_persona_agent(app, persona)

    # Get or create a session for conversation memory
    session_id = req.session_id or "default"
    session_key = f"{persona}:{session_id}"
    if session_key not in app.state.sessions:
        app.state.sessions[session_key] = AgentSession(session_id=session_key)
    session = app.state.sessions[session_key]

    response = await agent.run(q, session=session)
    text = (
        getattr(response, "text", None)
        or getattr(response, "content", None)
        or str(response)
    )

    # Post-process: convert bare citation IDs (e.g. MTG-001, EML-002) into
    # clickable simulator links so the UI can render them.
    # Skip IDs already inside markdown links (preceded by / or [).
    import re
    def _linkify_citation(m):
        cid = m.group(0)
        entry = _scenario.index.get(cid)
        if entry:
            kind, _rec = entry
            title = cid
            return f"[{title}](https://simulator.local/{kind}/{cid})"
        return cid
    text = re.sub(r'(?<![/\[])\b(MTG|EML|MSG|FILE|PPL|CAPA|AI)-\d{3}\b(?!\])', _linkify_citation, text)

    return {"answer": text, "persona": persona}


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
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 0;
    background: #0f1116; color: #e6e6e6;
    display: flex; flex-direction: row; height: 100vh; overflow: hidden;
  }

  /* Sidebar */
  #sidebar {
    width: 270px; min-width: 270px; background: #14161e;
    border-right: 1px solid #2a2d38; display: flex; flex-direction: column;
    height: 100vh;
  }
  #sidebar .brand { padding: 14px 16px; border-bottom: 1px solid #2a2d38; }
  #sidebar .brand h2 { margin: 0; font-size: 14px; font-weight: 600; }
  #sidebar .brand .sub { font-size: 11px; color: #8a8f9c; margin-top: 2px; }
  #new-chat {
    margin: 12px 16px; padding: 10px 12px; background: #2563eb; color: #fff;
    border: 0; border-radius: 8px; font-weight: 600; cursor: pointer;
  }
  #new-chat:hover { background: #1d4fd6; }
  #clear-all {
    margin: 0 16px 12px; padding: 8px 12px; background: transparent; color: #ff7a7a;
    border: 1px solid #2a2d38; border-radius: 8px; font-weight: 600; cursor: pointer;
    font-size: 12px;
  }
  #clear-all:hover { background: #2a1520; border-color: #ff7a7a; }
  .sessions-label {
    font-size: 10px; text-transform: uppercase; letter-spacing: .5px;
    color: #5a6173; padding: 4px 16px 6px;
  }
  #sessions { flex: 1; overflow-y: auto; padding: 0 8px 12px; }
  .session-item {
    padding: 9px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px;
    border: 1px solid transparent;
  }
  .session-item:hover { background: #1c1f2a; }
  .session-item.active { background: #232735; border-color: #2f3445; }
  .session-item .title {
    font-size: 13px; color: #e6e6e6; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .session-item .persona-tag {
    font-size: 10px; color: #8a8f9c; margin-top: 3px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .session-item .del {
    float: right; color: #5a6173; font-size: 13px; visibility: hidden;
    border: 0; background: none; cursor: pointer; padding: 0 2px;
  }
  .session-item:hover .del { visibility: visible; }
  .session-item .del:hover { color: #ff7a7a; }

  #main { flex: 1; display: flex; flex-direction: column; height: 100vh; min-width: 0; }
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
  header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .persona-box { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
  .persona-box label { font-size: 10px; text-transform: uppercase; letter-spacing: .5px; color: #5a6173; }
  #persona {
    background: #0f1116; color: #e6e6e6; border: 1px solid #2a2d38;
    border-radius: 6px; padding: 6px 10px; font: inherit; font-size: 13px;
    max-width: 360px; cursor: pointer;
  }
  #persona:disabled { opacity: .65; cursor: not-allowed; }
  #persona-desc { font-size: 11px; color: #8a8f9c; max-width: 360px; text-align: right; }
  #persona-lock { font-size: 10px; color: #d9a441; }
  .empty-state { color: #5a6173; text-align: center; margin-top: 80px; font-size: 14px; }
</style>
</head>
<body>
<div id="sidebar">
  <div class="brand">
    <h2>Work IQ Orchestrator</h2>
    <div class="sub">NorthBridge Health Network</div>
  </div>
  <button id="new-chat" type="button">+ New chat</button>
  <button id="clear-all" type="button">🗑 Clear all</button>
  <div class="sessions-label">Chats</div>
  <div id="sessions"></div>
</div>

<div id="main">
  <header>
    <div>
      <h1 id="chat-title">New chat</h1>
      <div class="sub" id="chat-sub">Pick a persona, then ask a question</div>
    </div>
    <div class="persona-box">
      <label for="persona">Acting as (RBAC persona)</label>
      <select id="persona"></select>
      <div id="persona-desc"></div>
      <div id="persona-lock"></div>
    </div>
  </header>

  <div id="log"></div>

  <form id="form">
    <textarea id="q" placeholder="Ask something — e.g. 'what's the status of the Westgate EHR rollout?'" required></textarea>
    <button id="send" type="submit">Send</button>
  </form>
</div>

<script>
const log        = document.getElementById('log');
const form       = document.getElementById('form');
const q          = document.getElementById('q');
const send       = document.getElementById('send');
const personaSel = document.getElementById('persona');
const personaDesc= document.getElementById('persona-desc');
const personaLock= document.getElementById('persona-lock');
const sessionsEl = document.getElementById('sessions');
const newChatBtn = document.getElementById('new-chat');
const chatTitle  = document.getElementById('chat-title');
const chatSub    = document.getElementById('chat-sub');

const STORE_KEY = 'workiq_sessions_v2';
let personaList = [];
let defaultPersona = null;

// ---- persistent state: many sessions, each locked to ONE persona ---------- //
let state = { sessions: [], activeId: null };
function loadState() {
  try { state = JSON.parse(localStorage.getItem(STORE_KEY)) || state; } catch (e) {}
  if (!state.sessions) state.sessions = [];
}
// Merge with whatever other tabs have written so concurrent tabs don't clobber
// each other's chats (union by id; our in-memory copy wins for shared ids).
function saveState(skipMerge) {
  if (!skipMerge) {
    let disk = { sessions: [] };
    try { disk = JSON.parse(localStorage.getItem(STORE_KEY)) || disk; } catch (e) {}
    const ourIds = new Set(state.sessions.map(s => s.id));
    const extras = (disk.sessions || []).filter(s => !ourIds.has(s.id));
    state.sessions = state.sessions.concat(extras);
  }
  localStorage.setItem(STORE_KEY, JSON.stringify(state));
}
function activeSession() { return state.sessions.find(s => s.id === state.activeId) || null; }
function sessionHasMessages(s) { return !!(s && s.html && s.html.trim()); }
function personaLabel(id) {
  const p = personaList.find(p => p.id === id);
  return p ? p.label : id;
}
function newId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }

function escape(s) {
  return s.replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

// ---- personas ------------------------------------------------------------- //
async function loadPersonas() {
  try {
    const r = await fetch('/personas');
    const data = await r.json();
    personaList = data.personas || [];
    defaultPersona = data.default;
  } catch (err) {
    personaDesc.textContent = 'could not load personas';
  }
  personaSel.innerHTML = '';
  personaList.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    personaSel.appendChild(opt);
  });
}
function updatePersonaDesc() {
  const p = personaList.find(p => p.id === personaSel.value);
  personaDesc.textContent = p ? p.description : '';
}

// ---- sidebar -------------------------------------------------------------- //
function renderSidebar() {
  sessionsEl.innerHTML = '';
  state.sessions.forEach(s => {
    const item = document.createElement('div');
    item.className = 'session-item' + (s.id === state.activeId ? ' active' : '');
    item.innerHTML =
      '<button class="del" title="Delete chat">\u2715</button>' +
      '<div class="title">' + escape(s.title || 'New chat') + '</div>' +
      '<div class="persona-tag">' + escape(personaLabel(s.persona)) + '</div>';
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('del')) return;
      switchTo(s.id);
    });
    item.querySelector('.del').addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(s.id);
    });
    sessionsEl.appendChild(item);
  });
}

// ---- render the active chat ----------------------------------------------- //
function renderActive() {
  const s = activeSession();
  if (!s) { log.innerHTML = '<div class="empty-state">Start a new chat to begin.</div>'; return; }

  // Reflect this session's locked persona in the dropdown.
  personaSel.value = s.persona;
  updatePersonaDesc();

  const locked = sessionHasMessages(s);
  personaSel.disabled = locked;
  personaLock.textContent = locked
    ? '\uD83D\uDD12 persona locked for this chat — use “New chat” to switch'
    : '';

  chatTitle.textContent = s.title || 'New chat';
  chatSub.textContent = 'Acting as ' + personaLabel(s.persona);

  if (sessionHasMessages(s)) {
    log.innerHTML = s.html;
    // Drop any stale "thinking…" placeholder saved from an interrupted request.
    log.querySelectorAll('.msg.thinking').forEach(n => n.remove());
    log.querySelectorAll('a[href^="/citation/"]').forEach(a => a.target = '_blank');
  } else {
    log.innerHTML = '<div class="empty-state">Ask a question as <b>' +
      escape(personaLabel(s.persona)) + '</b>.<br>This chat is tied to this persona only.</div>';
  }
  log.scrollTop = log.scrollHeight;
}

function switchTo(id) {
  state.activeId = id;
  saveState();
  renderSidebar();
  renderActive();
  q.focus();
}

function newChat(persona) {
  const s = {
    id: newId(),
    persona: persona || personaSel.value || defaultPersona,
    title: 'New chat',
    html: '',
  };
  state.sessions.unshift(s);
  state.activeId = s.id;
  saveState();
  renderSidebar();
  renderActive();
  q.focus();
}

function deleteSession(id) {
  state.sessions = state.sessions.filter(s => s.id !== id);
  if (state.activeId === id) {
    state.activeId = state.sessions.length ? state.sessions[0].id : null;
  }
  if (!state.sessions.length) {
    saveState();
    newChat(defaultPersona);
    return;
  }
  saveState();
  renderSidebar();
  renderActive();
}

// ---- message helpers ------------------------------------------------------ //
function persistActiveHtml() {
  const s = activeSession();
  if (s) { s.html = log.innerHTML; saveState(); }
}
function add(kind, html) {
  // Clear the empty-state placeholder on first real message.
  const es = log.querySelector('.empty-state');
  if (es) log.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'msg ' + kind;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

// ---- persona dropdown: changing persona starts a NEW chat ----------------- //
personaSel.addEventListener('change', () => {
  const s = activeSession();
  if (s && !sessionHasMessages(s)) {
    // empty chat — just retarget it to the new persona
    s.persona = personaSel.value;
    saveState();
    updatePersonaDesc();
    renderSidebar();
    renderActive();
  } else {
    // chat already has messages — different persona = new window
    newChat(personaSel.value);
  }
});

newChatBtn.addEventListener('click', () => newChat(personaSel.value || defaultPersona));

document.getElementById('clear-all').addEventListener('click', () => {
  if (!confirm('Delete all chats?')) return;
  state.sessions = [];
  state.activeId = null;
  localStorage.removeItem(STORE_KEY);
  // Create one fresh chat without merging old sessions back from disk.
  const s = {
    id: newId(),
    persona: defaultPersona || personaSel.value,
    title: 'New chat',
    html: '',
  };
  state.sessions = [s];
  state.activeId = s.id;
  saveState(true);
  renderSidebar();
  renderActive();
  q.focus();
});

// ---- send ----------------------------------------------------------------- //
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = q.value.trim();
  if (!text) return;
  let s = activeSession();
  if (!s) { newChat(defaultPersona); s = activeSession(); }

  const persona = s.persona;
  // Lock this session's persona now that it carries a conversation.
  personaSel.disabled = true;
  personaLock.textContent = '\uD83D\uDD12 persona locked for this chat — use “New chat” to switch';

  if (!sessionHasMessages(s)) {
    s.title = text.length > 40 ? text.slice(0, 40) + '\u2026' : text;
    chatTitle.textContent = s.title;
  }

  add('user', escape(text));
  q.value = '';
  send.disabled = true;
  // Persist the user message BEFORE the request so a reload mid-flight never
  // freezes on a stale "thinking…" placeholder.
  persistActiveHtml();
  renderSidebar();
  const placeholder = add('agent thinking', 'thinking\u2026');

  try {
    const r = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text, persona: persona, session_id: s.id }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    placeholder.classList.remove('thinking');
    let answer = (data.answer || '(no answer)').replace(/https:\/\/simulator\.local\//g, '/citation/');
    placeholder.innerHTML = marked.parse(answer);
    placeholder.querySelectorAll('a[href^="/citation/"]').forEach(a => a.target = '_blank');
  } catch (err) {
    placeholder.remove();
    add('error', 'error: ' + escape(String(err.message || err)));
  } finally {
    persistActiveHtml();
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

// ---- boot ----------------------------------------------------------------- //
(async function init() {
  await loadPersonas();
  loadState();
  if (!state.sessions.length) {
    newChat(defaultPersona);
  } else {
    if (!activeSession()) state.activeId = state.sessions[0].id;
    renderSidebar();
    renderActive();
  }
  q.focus();
})();

// Keep multiple open tabs in sync: when another tab saves, pull in its chats
// (but never clobber the chat the user is actively viewing here).
window.addEventListener('storage', (e) => {
  if (e.key !== STORE_KEY) return;
  let disk = { sessions: [] };
  try { disk = JSON.parse(e.newValue) || disk; } catch (err) { return; }
  const ourIds = new Set(state.sessions.map(s => s.id));
  const extras = (disk.sessions || []).filter(s => !ourIds.has(s.id));
  if (extras.length) {
    state.sessions = state.sessions.concat(extras);
    renderSidebar();
  }
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/citation/{kind}/{cid}", response_class=HTMLResponse)
async def citation_detail(kind: str, cid: str) -> str:
    """Serve a simple HTML page showing the full citation record."""
    record = _scenario.index.get(cid)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Citation {cid} not found")
    _kind, data = record
    import json as _json
    title = data.get("subject") or data.get("title") or data.get("name") or cid
    body = data.get("body") or data.get("recap") or data.get("text") or data.get("summary") or data.get("content_excerpt") or ""
    meta_fields = {k: v for k, v in data.items() if k not in ("body", "recap", "text", "summary", "content_excerpt", "acl")}
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{cid} — {title}</title>
<style>
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f1116; color: #e6e6e6; padding: 40px; max-width: 800px; margin: 0 auto; }}
  h1 {{ color: #9cc7ff; font-size: 20px; }}
  .badge {{ display: inline-block; background: #2563eb; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-right: 8px; }}
  .meta {{ background: #161922; padding: 14px; border-radius: 8px; margin: 16px 0; font-size: 13px; border: 1px solid #242838; }}
  .meta dt {{ color: #8a8f9c; float: left; width: 120px; }}
  .meta dd {{ margin-left: 130px; margin-bottom: 6px; }}
  .body {{ background: #161922; padding: 18px; border-radius: 8px; border: 1px solid #242838; white-space: pre-wrap; line-height: 1.6; }}
  a {{ color: #6fb3ff; }}
</style></head><body>
<a href="/">&larr; Back to chat</a>
<h1><span class="badge">{_kind}</span> {cid} — {title}</h1>
<dl class="meta">{''.join(f'<dt>{k}</dt><dd>{v}</dd>' for k, v in meta_fields.items())}</dl>
<div class="body">{body}</div>
</body></html>"""


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
