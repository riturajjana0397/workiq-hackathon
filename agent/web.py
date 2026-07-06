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
from dataclasses import dataclass
import html
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import time

import msal
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
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
from telemetry import (
    record_usage,
    setup_telemetry,
    span_context_attributes,
)  # noqa: E402
from agent_framework import AgentSession, function_middleware  # noqa: E402

# Load scenario data for citation lookup
sys.path.insert(0, str(REPO_ROOT / "simulator"))
from engine import load_scenario  # noqa: E402

_scenario = load_scenario(REPO_ROOT / "simulator" / SCENARIO)



logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
  value = os.environ.get(name)
  if value is None:
    return default
  return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
  value = os.environ.get(name)
  if value is None:
    return default
  try:
    parsed = int(value.strip())
  except (TypeError, ValueError):
    logger.warning("Invalid %s=%r; using default %d", name, value, default)
    return default
  return parsed if parsed >= 0 else 0


TOKEN_LIMIT_PER_SESSION = _env_int("WORKIQ_WEB_TOKEN_LIMIT", default=0)
COOLDOWN_SECONDS = _env_int("WORKIQ_WEB_COOLDOWN_SECONDS", default=0)
UI_ENABLE_CLIENT_COOLDOWN = _env_flag("WORKIQ_WEB_UI_ENABLE_CLIENT_COOLDOWN", default=True)
UI_SHOW_COOLDOWN_TIMER = _env_flag("WORKIQ_WEB_UI_SHOW_COOLDOWN_TIMER", default=True)
UI_SHOW_TOKEN_REMAINING = _env_flag("WORKIQ_WEB_UI_SHOW_TOKEN_REMAINING", default=True)

PWA_APP_NAME = "Work IQ Orchestrator"
PWA_SHORT_NAME = "WorkIQ"
PWA_THEME_COLOR = "#0f1116"
PWA_BACKGROUND_COLOR = "#0f1116"
PWA_CACHE_NAME = "workiq-web-v1"
PWA_ICON_PATH = Path(__file__).resolve().parent / "complog.svg"


@dataclass(frozen=True)
class EntraAuthConfig:
  tenant_id: str
  client_id: str
  client_secret: str
  session_secret: str
  scopes: tuple[str, ...]
  secure_cookies: bool

  @property
  def authority(self) -> str:
    return f"https://login.microsoftonline.com/{self.tenant_id}"


def _load_auth_config() -> EntraAuthConfig | None:
  if not _env_flag("WORKIQ_WEB_AUTH_ENABLED", default=False):
    return None

  client_id = os.environ.get("WORKIQ_WEB_AUTH_CLIENT_ID", "").strip()
  client_secret = os.environ.get("WORKIQ_WEB_AUTH_CLIENT_SECRET", "").strip()
  tenant_id = os.environ.get("WORKIQ_WEB_AUTH_TENANT_ID", "").strip()
  session_secret = os.environ.get("WORKIQ_WEB_AUTH_SESSION_SECRET", "").strip()
  configured = {
    "WORKIQ_WEB_AUTH_CLIENT_ID": client_id,
    "WORKIQ_WEB_AUTH_CLIENT_SECRET": client_secret,
    "WORKIQ_WEB_AUTH_TENANT_ID": tenant_id,
    "WORKIQ_WEB_AUTH_SESSION_SECRET": session_secret,
  }
  present = [name for name, value in configured.items() if value]
  if not present:
    return None

  missing = [name for name, value in configured.items() if not value]
  if missing:
    raise RuntimeError(
      "Microsoft Entra web auth is partially configured. Missing: "
      + ", ".join(missing)
    )
  if len(session_secret) < 32:
    raise RuntimeError(
      "WORKIQ_WEB_AUTH_SESSION_SECRET must be at least 32 characters long."
    )

  scopes_raw = os.environ.get(
    "WORKIQ_WEB_AUTH_SCOPES",
    "email",
  )
  reserved_scopes = {"openid", "profile", "offline_access"}
  scopes = tuple(part for part in scopes_raw.split() if part and part.lower() not in reserved_scopes)
  if not scopes:
    scopes = ("email",)

  return EntraAuthConfig(
    tenant_id=tenant_id,
    client_id=client_id,
    client_secret=client_secret,
    session_secret=session_secret,
    scopes=scopes,
    secure_cookies=_env_flag("WORKIQ_WEB_AUTH_SECURE_COOKIES", default=False),
  )


AUTH_CONFIG = _load_auth_config()
SessionMiddleware = None
if AUTH_CONFIG is not None:
  from starlette.middleware.sessions import SessionMiddleware


def _build_msal_app(config: EntraAuthConfig) -> msal.ConfidentialClientApplication:
  return msal.ConfidentialClientApplication(
    client_id=config.client_id,
    client_credential=config.client_secret,
    authority=config.authority,
  )


def _build_login_url(request: Request) -> str:
  next_path = request.url.path
  if request.url.query:
    next_path = f"{next_path}?{request.url.query}"
  return str(request.url_for("auth_login").include_query_params(next=next_path))


def _get_redirect_uri(request: Request) -> str:
  explicit = os.environ.get("WORKIQ_WEB_AUTH_REDIRECT_URI", "").strip()
  if explicit:
    return explicit
  return str(request.url_for("auth_callback"))


def _session_user(request: Request) -> dict | None:
  if AUTH_CONFIG is None:
    return None
  return request.session.get("user")


def _require_browser_auth(request: Request) -> RedirectResponse | None:
  if AUTH_CONFIG is None or _session_user(request):
    return None
  return RedirectResponse(_build_login_url(request), status_code=307)


def _require_api_auth(request: Request) -> JSONResponse | None:
  if AUTH_CONFIG is None or _session_user(request):
    return None
  return JSONResponse(
    status_code=401,
    content={
      "detail": "Authentication required",
      "login_url": _build_login_url(request),
    },
  )


def _render_auth_error(message: str, *, status_code: int = 401) -> HTMLResponse:
  safe_message = html.escape(message)
  return HTMLResponse(
    status_code=status_code,
    content=(
      "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
      "<title>Authentication Error</title>"
      "<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0f1116;"
      "color:#e6e6e6;display:grid;place-items:center;min-height:100vh;margin:0;}"
      ".card{max-width:560px;padding:24px;border:1px solid #2a2d38;border-radius:12px;"
      "background:#161922;}a{color:#6fb3ff;}</style></head><body>"
      f"<div class=\"card\"><h1>Authentication failed</h1><p>{safe_message}</p>"
      "<p><a href=\"/auth/login\">Try signing in again</a></p></div></body></html>"
    ),
  )


def _truncate_text(value: str, limit: int = 220) -> str:
  if len(value) <= limit:
    return value
  return value[: limit - 1] + "..."


def _json_safe(value):
  """Convert runtime objects into JSON-serializable, compact values."""
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  if isinstance(value, Decimal):
    return float(value)
  if isinstance(value, dict):
    return {str(k): _json_safe(v) for k, v in value.items()}
  if isinstance(value, (list, tuple, set)):
    return [_json_safe(v) for v in value]
  # Handle Content objects from agent_framework
  if hasattr(value, "text") and hasattr(value, "content_type"):
    try:
      return _json_safe({"text": getattr(value, "text", ""), "content_type": getattr(value, "content_type", "unknown")})
    except Exception:
      pass
  # Handle dataclass or Pydantic models
  if hasattr(value, "model_dump"):
    try:
      return _json_safe(value.model_dump())
    except Exception:
      pass
  if hasattr(value, "dict"):
    try:
      return _json_safe(value.dict())
    except Exception:
      pass
  # For objects with __dict__, try to extract readable attributes
  if hasattr(value, "__dict__"):
    try:
      attrs = {k: _json_safe(v) for k, v in value.__dict__.items() if not k.startswith("_")}
      if attrs:
        return attrs
    except Exception:
      pass
  return _truncate_text(str(value), 400)

# ---------------------------------------------------------------------------- #
# Lifespan: build the agent once, tear it down cleanly on shutdown.            #
# ---------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry = setup_telemetry("workiq-web")
    app.state._telemetry = telemetry
    # One chat client shared by every persona's agent.
    app.state.chat_client = build_chat_client()
    # persona id -> {"agent", "mcp", "a2a"} built lazily on first use.
    app.state.agents = {}
    app.state.build_lock = asyncio.Lock()
    # session_key -> AgentSession for conversation memory
    app.state.sessions = {}
    # session_key -> cumulative total tokens used in this process
    app.state.session_token_usage = {}
    # session_key -> next unix timestamp when a request is allowed
    app.state.session_next_allowed_at = {}

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
if AUTH_CONFIG is not None:
  app.add_middleware(
    SessionMiddleware,
    secret_key=AUTH_CONFIG.session_secret,
    same_site="lax",
    https_only=AUTH_CONFIG.secure_cookies,
    max_age=8 * 60 * 60,
  )


class AskRequest(BaseModel):
    question: str
    persona: str | None = None
    session_id: str | None = None


@app.get("/personas")
async def personas(request: Request):
  """List selectable personas (id + human label) for the UI dropdown."""
  auth = _require_api_auth(request)
  if auth is not None:
    return auth
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


@app.get("/auth/login", name="auth_login")
async def auth_login(request: Request):
    if AUTH_CONFIG is None:
        return RedirectResponse(url="/", status_code=307)

    try:
        state = secrets.token_urlsafe(32)
        next_path = request.query_params.get("next", "/")
        if not next_path.startswith("/"):
            next_path = "/"

        request.session["auth_state"] = state
        request.session["post_login_redirect"] = next_path

        auth_url = _build_msal_app(AUTH_CONFIG).get_authorization_request_url(
            scopes=list(AUTH_CONFIG.scopes),
            state=state,
            redirect_uri=_get_redirect_uri(request),
            prompt="select_account",
        )
        return RedirectResponse(url=auth_url, status_code=307)
    except Exception as exc:
        logger.exception("Failed to build Microsoft login redirect")
        return _render_auth_error(
            "Could not start Microsoft sign-in. "
            f"Check WORKIQ_WEB_AUTH_* settings. Details: {exc}",
            status_code=500,
        )


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    if AUTH_CONFIG is None:
        return RedirectResponse(url="/", status_code=307)

    try:
        error = request.query_params.get("error")
        if error:
            description = request.query_params.get("error_description") or error
            return _render_auth_error(description)

        state = request.query_params.get("state")
        expected_state = request.session.get("auth_state")
        if not state or state != expected_state:
            return _render_auth_error("The sign-in response state did not match the original request.")

        code = request.query_params.get("code")
        if not code:
            return _render_auth_error("The sign-in response did not include an authorization code.")

        result = _build_msal_app(AUTH_CONFIG).acquire_token_by_authorization_code(
            code=code,
            scopes=list(AUTH_CONFIG.scopes),
            redirect_uri=_get_redirect_uri(request),
        )
        if "error" in result:
            description = result.get("error_description") or result["error"]
            return _render_auth_error(description)

        claims = result.get("id_token_claims") or {}
        request.session.pop("auth_state", None)
        request.session["user"] = {
            "name": claims.get("name") or claims.get("preferred_username") or claims.get("oid") or "unknown",
            "preferred_username": claims.get("preferred_username"),
            "oid": claims.get("oid"),
            "tid": claims.get("tid"),
        }
        destination = request.session.pop("post_login_redirect", "/")
        if not isinstance(destination, str) or not destination.startswith("/"):
            destination = "/"
        return RedirectResponse(url=destination, status_code=307)
    except Exception as exc:
        logger.exception("Microsoft sign-in callback failed")
        return _render_auth_error(
            f"Microsoft sign-in callback failed: {exc}",
            status_code=500,
        )


@app.get("/auth/logout")
async def auth_logout(request: Request):
    if AUTH_CONFIG is not None:
        request.session.clear()
    return RedirectResponse(url="/", status_code=307)


@app.post("/ask")
async def ask(req: AskRequest, request: Request):
    auth = _require_api_auth(request)
    if auth is not None:
        return auth

    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question is required")

    telemetry = request.app.state._telemetry
    persona = (req.persona or PERSONA).strip()
    agent = await get_persona_agent(request.app, persona)

    # Get or create a session for conversation memory.
    session_id = req.session_id or "default"
    session_key = f"{persona}:{session_id}"
    if session_key not in request.app.state.sessions:
        request.app.state.sessions[session_key] = AgentSession(session_id=session_key)
    session = request.app.state.sessions[session_key]
    used_tokens = int(request.app.state.session_token_usage.get(session_key, 0))
    now_ts = time.time()
    next_allowed_at = float(request.app.state.session_next_allowed_at.get(session_key, 0.0))

    if COOLDOWN_SECONDS > 0 and now_ts < next_allowed_at:
      retry_after = max(int(next_allowed_at - now_ts), 1)
      raise HTTPException(
        status_code=429,
        detail=(
          "Cooldown active for this chat session. "
          f"Try again in {retry_after}s."
        ),
        headers={"Retry-After": str(retry_after)},
      )

    if TOKEN_LIMIT_PER_SESSION > 0 and used_tokens >= TOKEN_LIMIT_PER_SESSION:
      raise HTTPException(
        status_code=429,
        detail=(
          "Token limit reached for this chat session "
          f"({used_tokens}/{TOKEN_LIMIT_PER_SESSION}). "
          "Start a new chat or increase WORKIQ_WEB_TOKEN_LIMIT."
        ),
      )

    started = time.perf_counter()
    elapsed_ms = 0.0
    usage: dict[str, int] = {}
    answer_text = ""
    tool_trail: list[dict] = []
    turn_started_at = datetime.now(timezone.utc).isoformat()

    @function_middleware
    async def _tool_trace_middleware(context, call_next):
      tool_started = time.perf_counter()
      call_id = context.metadata.get("call_id")
      args = _json_safe(context.arguments)
      status = "ok"
      error = None
      try:
        await call_next()
      except Exception as exc:
        status = "error"
        error = str(exc)
        raise
      finally:
        duration_ms = (time.perf_counter() - tool_started) * 1000
        result_obj = getattr(context, "result", None)
        result_serialized = _json_safe(result_obj)
        result_preview = _truncate_text(
          str(result_serialized) if result_serialized is not None else "",
          220
        )
        tool_trail.append(
          {
            "index": len(tool_trail) + 1,
            "tool": context.function.name,
            "call_id": call_id,
            "status": status,
            "duration_ms": round(duration_ms, 2),
            "args": args,
            "result_preview": result_preview,
            "error": _truncate_text(error, 220) if error else None,
          }
        )

    try:
        with telemetry.tracer.start_as_current_span(
            "workiq.web.ask",
            attributes=span_context_attributes(
                service="workiq-web",
                scenario=SCENARIO,
                persona=persona,
                deployment=DEPLOYMENT,
                question=q,
                question_chars=len(q),
            ),
        ) as span:
            response = await agent.run(
              q,
              session=session,
              client_kwargs={"middleware": [_tool_trace_middleware]},
            )
            usage = record_usage(telemetry, response, span)
            answer_text = (
                getattr(response, "text", None)
                or getattr(response, "content", None)
                or str(response)
            )
            if usage:
                span.set_attribute("workiq.total_tokens", usage.get("total_tokens", 0))
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
                persona=persona,
                deployment=DEPLOYMENT,
            ),
        )

    # Convert bare citation IDs into links for the UI.
    import re

    def _linkify_citation(m):
        cid = m.group(0)
        entry = _scenario.index.get(cid)
        if entry:
            kind, _rec = entry
            return f"[{cid}](https://simulator.local/{kind}/{cid})"
        return cid

    answer_text = re.sub(
        r"(?<![/\[] )\b(MTG|EML|MSG|FILE|PPL|CAPA|AI)-\d{3}\b(?!\])".replace(" ", ""),
        _linkify_citation,
        answer_text,
    )

    turn_total_tokens = int(usage.get("total_tokens", 0) or 0)
    used_tokens_after = used_tokens + turn_total_tokens
    request.app.state.session_token_usage[session_key] = used_tokens_after
    if COOLDOWN_SECONDS > 0:
      request.app.state.session_next_allowed_at[session_key] = time.time() + COOLDOWN_SECONDS

    return {
        "answer": answer_text,
        "persona": persona,
        "usage": usage,
        "latency_ms": round(elapsed_ms, 2),
      "trail": tool_trail,
      "turn_started_at": turn_started_at,
      "token_limit": TOKEN_LIMIT_PER_SESSION,
      "tokens_used": used_tokens_after,
      "tokens_remaining": (
          max(TOKEN_LIMIT_PER_SESSION - used_tokens_after, 0)
          if TOKEN_LIMIT_PER_SESSION > 0
          else None
      ),
      "cooldown_seconds": COOLDOWN_SECONDS,
      "next_allowed_at": request.app.state.session_next_allowed_at.get(session_key),
    }


# ---------------------------------------------------------------------------- #
# UI                                                                            #
# ---------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Work IQ — Orchestrator</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1116">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="WorkIQ">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/svg+xml" sizes="any" href="/complog.svg">
<link rel="apple-touch-icon" href="/complog.svg">
<script>
  (function () {
    try {
      const theme = localStorage.getItem('workiq_theme_v1') || 'clinical';
      document.documentElement.setAttribute('data-theme', theme);
    } catch (err) {
      document.documentElement.setAttribute('data-theme', 'clinical');
    }
  })();
</script>
<!-- markdown rendering for the agent's reply -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    color-scheme: light;
    --bg-page: #eef4fa;
    --bg-page-accent: radial-gradient(1200px 540px at 90% -12%, #d8eafc 0%, rgba(216, 234, 252, 0) 62%), radial-gradient(920px 460px at -10% 110%, #deecfa 0%, rgba(222, 236, 250, 0) 58%);
    --bg-sidebar: rgba(250, 252, 255, 0.92);
    --bg-header: rgba(250, 252, 255, 0.88);
    --bg-surface: #fafcff;
    --bg-elevated: #e8f0f8;
    --bg-input: #ffffff;
    --overlay: rgba(15, 23, 42, 0.34);
    --text-main: #0f172a;
    --text-muted: #334155;
    --text-faint: #64748b;
    --text-inverse: #f8fbff;
    --border: #c4d2e3;
    --border-strong: #8ea2bf;
    --accent: #0ea5a4;
    --accent-strong: #0f766e;
    --accent-soft: #dff6f4;
    --link: #0369a1;
    --danger: #dc2626;
    --danger-soft: #fee2e2;
    --warning: #b45309;
    --success: #0f766e;
  }

  html[data-theme="ops"] {
    color-scheme: dark;
    --bg-page: #0b1220;
    --bg-page-accent: radial-gradient(1100px 520px at 88% -14%, #13325a 0%, rgba(19, 50, 90, 0) 62%), radial-gradient(980px 560px at -8% 115%, #102a4a 0%, rgba(16, 42, 74, 0) 58%);
    --bg-sidebar: rgba(15, 23, 42, 0.85);
    --bg-header: rgba(17, 24, 39, 0.78);
    --bg-surface: #111827;
    --bg-elevated: #161f33;
    --bg-input: #0b1220;
    --overlay: rgba(0, 0, 0, 0.58);
    --text-main: #e5e7eb;
    --text-muted: #94a3b8;
    --text-faint: #64748b;
    --text-inverse: #f8fbff;
    --border: #334155;
    --border-strong: #475569;
    --accent: #22c55e;
    --accent-strong: #16a34a;
    --accent-soft: rgba(34, 197, 94, 0.18);
    --link: #38bdf8;
    --danger: #ef4444;
    --danger-soft: rgba(239, 68, 68, 0.18);
    --warning: #f59e0b;
    --success: #34d399;
  }

  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 0;
    background: var(--bg-page);
    background-image: var(--bg-page-accent);
    color: var(--text-main);
    display: flex; flex-direction: row; height: 100vh; overflow: hidden;
    transition: background-color .3s ease, color .3s ease;
  }

  #mobile-topbar {
    display: none;
  }
  #mobile-menu-btn {
    border: 1px solid var(--border);
    background: var(--bg-surface);
    color: var(--text-main);
    border-radius: 8px;
    width: 40px;
    height: 40px;
    padding: 0;
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
  }
  #mobile-title {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: .02em;
    color: var(--text-main);
  }
  #mobile-backdrop {
    display: none;
  }

  #welcome-splash {
    position: fixed;
    inset: 0;
    z-index: 200;
    display: grid;
    place-items: center;
    padding: 24px;
    background:
      radial-gradient(640px 320px at 78% 18%, rgba(14, 165, 164, 0.26), rgba(14, 165, 164, 0) 62%),
      radial-gradient(680px 360px at 16% 78%, rgba(3, 105, 161, 0.24), rgba(3, 105, 161, 0) 64%),
      linear-gradient(145deg, #071529 0%, #0e2744 42%, #12345a 100%);
    color: #f8fbff;
    transition: opacity .7s ease, visibility .7s ease;
  }
  #welcome-splash.is-exiting {
    opacity: 0;
  }
  #welcome-splash.is-hidden {
    visibility: hidden;
    pointer-events: none;
  }
  .welcome-panel {
    width: min(620px, 100%);
    text-align: center;
    padding: 28px 24px;
    border-radius: 18px;
    border: 1px solid rgba(255, 255, 255, 0.2);
    background: rgba(8, 24, 46, 0.48);
    backdrop-filter: blur(8px);
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.34);
  }
  .welcome-logo {
    width: 96px;
    height: 96px;
    border-radius: 24px;
    display: block;
    margin: 0 auto;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.32);
    animation: welcome-float 2.6s ease-in-out infinite;
  }
  .welcome-title {
    margin: 16px 0 6px;
    font-size: clamp(28px, 4vw, 40px);
    letter-spacing: .01em;
    font-weight: 700;
    animation: welcome-rise .9s ease both;
  }
  .welcome-sub {
    margin: 0;
    color: rgba(248, 251, 255, 0.84);
    font-size: clamp(14px, 2.1vw, 18px);
    animation: welcome-rise 1.15s ease both;
  }
  .welcome-bar {
    margin: 20px auto 0;
    width: min(320px, 100%);
    height: 4px;
    border-radius: 999px;
    background: rgba(248, 251, 255, 0.22);
    overflow: hidden;
  }
  .welcome-progress {
    height: 100%;
    width: 0;
    border-radius: inherit;
    background: linear-gradient(90deg, #22d3ee 0%, #34d399 100%);
    animation: welcome-progress 3s linear forwards;
  }

  @keyframes welcome-float {
    0%, 100% { transform: translateY(0px); }
    50% { transform: translateY(-8px); }
  }
  @keyframes welcome-rise {
    from {
      transform: translateY(10px);
      opacity: 0;
    }
    to {
      transform: translateY(0);
      opacity: 1;
    }
  }
  @keyframes welcome-progress {
    from { width: 0; }
    to { width: 100%; }
  }

  @media (prefers-reduced-motion: reduce) {
    .welcome-logo,
    .welcome-title,
    .welcome-sub,
    .welcome-progress {
      animation: none;
    }
  }

  /* Sidebar */
  #sidebar {
    width: 270px; min-width: 270px; background: var(--bg-sidebar);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
    height: 100vh;
    backdrop-filter: blur(12px);
  }
  #sidebar .brand { padding: 14px 16px; border-bottom: 1px solid var(--border); }
  #sidebar .brand .brand-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  #sidebar .brand .brand-text {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  #sidebar .brand .brand-title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    line-height: 1.2;
  }
  #sidebar .brand .brand-logo {
    width: 50px;
    height: 50px;
    flex: 0 0 auto;
    display: block;
    border-radius: 50%;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.14);
  }
  #sidebar .brand .sub { font-size: 11px; color: var(--text-faint); margin-top: 2px; }
  #new-chat {
    margin: 12px 16px; padding: 10px 12px; background: var(--accent); color: var(--text-inverse);
    border: 0; border-radius: 8px; font-weight: 600; cursor: pointer;
    box-shadow: 0 10px 26px color-mix(in srgb, var(--accent) 26%, transparent);
  }
  #new-chat:hover { background: var(--accent-strong); }
  #clear-all {
    margin: 0 16px 12px; padding: 8px 12px; background: transparent; color: var(--danger);
    border: 1px solid var(--border); border-radius: 8px; font-weight: 600; cursor: pointer;
    font-size: 12px;
  }
  #clear-all:hover { background: var(--danger-soft); border-color: var(--danger); }
  .sessions-label {
    font-size: 10px; text-transform: uppercase; letter-spacing: .5px;
    color: var(--text-faint); padding: 4px 16px 6px;
  }
  #sessions { flex: 1; overflow-y: auto; padding: 0 8px 12px; }
  .session-item {
    padding: 9px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 2px;
    border: 1px solid transparent;
  }
  .session-item:hover { background: var(--bg-elevated); }
  .session-item.active { background: var(--accent-soft); border-color: var(--border-strong); }
  .session-item .title {
    font-size: 13px; color: var(--text-main); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .session-item .persona-tag {
    font-size: 10px; color: var(--text-faint); margin-top: 3px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .session-item .del {
    float: right; color: var(--text-faint); font-size: 13px; visibility: hidden;
    border: 0; background: none; cursor: pointer; padding: 0 2px;
  }
  .session-item:hover .del { visibility: visible; }
  .session-item .del:hover { color: var(--danger); }

  #main { flex: 1; display: flex; flex-direction: column; height: 100vh; min-width: 0; }
  header {
    padding: 12px 20px; background: var(--bg-header); border-bottom: 1px solid var(--border);
    backdrop-filter: blur(10px);
  }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header .sub { font-size: 12px; color: var(--text-faint); margin-top: 2px; }
  #log {
    flex: 1; overflow-y: auto; padding: 20px; max-width: 900px;
    width: 100%; margin: 0 auto; box-sizing: border-box;
  }
  .msg { margin-bottom: 18px; line-height: 1.5; }
  .msg.user { color: var(--accent-strong); }
  .msg.user::before { content: "you ▸ "; color: var(--text-faint); font-weight: 600; }
  .msg.agent { background: var(--bg-surface); padding: 14px 18px; border-radius: 8px;
               border: 1px solid var(--border); box-shadow: 0 8px 20px rgba(2, 6, 23, 0.08); }
  .msg.agent::before { content: "work iq ▸ "; color: var(--text-faint); font-weight: 600;
                       display: block; margin-bottom: 6px; font-size: 12px; }
  .msg.error { color: var(--danger); }
  .msg pre, .msg code { background: var(--bg-elevated); padding: 2px 6px; border-radius: 4px; }
  .msg pre { padding: 10px; overflow-x: auto; }
  .msg a { color: var(--link); }
  .thinking { color: var(--text-faint); font-style: italic; }
  form {
    display: flex; gap: 8px; padding: 16px 20px; background: var(--bg-header);
    border-top: 1px solid var(--border); max-width: 900px; width: 100%;
    margin: 0 auto; box-sizing: border-box;
  }
  textarea {
    flex: 1; background: var(--bg-input); color: var(--text-main);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 10px; font: inherit; resize: none; min-height: 44px;
  }
  textarea::placeholder { color: var(--text-faint); }
  button {
    background: var(--accent); color: var(--text-inverse); border: 0; border-radius: 6px;
    padding: 0 18px; font-weight: 600; cursor: pointer;
  }
  button:disabled { background: var(--border-strong); cursor: not-allowed; }
  #usage {
    margin: 0 8px 12px; background: color-mix(in srgb, var(--bg-surface) 82%, transparent);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px;
    font-size: 12px; line-height: 1.45;
    flex-shrink: 0;
  }
  #usage .title { color: var(--accent-strong); font-weight: 700; margin-bottom: 6px; }
  #usage .row { display: flex; justify-content: space-between; gap: 12px; }
  #usage .label {
    color: var(--text-faint);
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  #usage .value { color: var(--text-main); font-variant-numeric: tabular-nums; }
  #usage .hint { margin-top: 8px; color: var(--text-faint); }
  #usage .info {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--accent-strong);
    font-size: 10px;
    line-height: 1;
    cursor: help;
    flex: 0 0 auto;
  }
  @media (max-width: 780px) {
    #usage { margin: 0 8px 12px; }
  }
  header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .persona-box { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
  .persona-box label { font-size: 10px; text-transform: uppercase; letter-spacing: .5px; color: var(--text-faint); }
  #persona {
    background: var(--bg-input); color: var(--text-main); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font: inherit; font-size: 13px;
    max-width: 360px; cursor: pointer;
  }
  #theme-toggle-wrap {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    max-width: 360px;
  }
  #theme-toggle-wrap .chip {
    font-size: 11px;
    color: var(--text-faint);
    min-width: 36px;
    text-align: center;
  }
  .theme-switch {
    position: relative;
    display: inline-block;
    width: 46px;
    height: 26px;
  }
  .theme-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  .theme-slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 999px;
    transition: .25s;
  }
  .theme-slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 3px;
    top: 3px;
    border-radius: 50%;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    transition: .25s;
  }
  .theme-switch input:checked + .theme-slider {
    background: var(--accent-soft);
    border-color: var(--accent-strong);
  }
  .theme-switch input:checked + .theme-slider:before {
    transform: translateX(20px);
    background: var(--accent);
    border-color: var(--accent-strong);
  }
  #persona:disabled { opacity: .65; cursor: not-allowed; }
  #persona-desc { font-size: 11px; color: var(--text-faint); max-width: 360px; text-align: right; }
  #persona-lock { font-size: 10px; color: var(--warning); }
  .empty-state { color: var(--text-faint); text-align: center; margin-top: 80px; font-size: 14px; }
  .trail-row {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px dashed var(--border-strong);
    font-size: 12px;
    color: var(--text-faint);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .trail-row .trail-num {
    border: 1px solid var(--accent-strong);
    background: color-mix(in srgb, var(--accent) 22%, var(--bg-surface));
    color: var(--accent-strong);
    border-radius: 999px;
    min-width: 24px;
    height: 24px;
    padding: 0 8px;
    font-size: 12px;
    line-height: 22px;
    cursor: pointer;
    font-weight: 700;
  }
  .trail-row .trail-num:hover { background: color-mix(in srgb, var(--accent) 30%, var(--bg-surface)); }
  .trail-row .trail-meta { color: var(--text-faint); }
  #trail-modal {
    position: fixed;
    inset: 0;
    background: var(--overlay);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 40;
    padding: 16px;
  }
  #trail-modal.open { display: flex; }
  .trail-panel {
    width: min(840px, 100%);
    max-height: 82vh;
    overflow: auto;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 20px 44px rgba(0, 0, 0, 0.45);
  }
  .trail-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
  }
  .trail-head h3 { margin: 0; font-size: 15px; }
  .trail-close {
    border: 1px solid var(--border-strong);
    background: transparent;
    color: var(--text-main);
    border-radius: 8px;
    padding: 4px 10px;
    cursor: pointer;
  }
  .trail-body { padding: 14px 16px 16px; }
  .trail-summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 10px;
    margin-bottom: 12px;
  }
  .trail-kpi {
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
  }
  .trail-kpi .k { color: var(--text-faint); font-size: 11px; }
  .trail-kpi .v { color: var(--text-main); font-weight: 700; margin-top: 2px; }
  .call {
    border: 1px solid var(--border);
    background: var(--bg-elevated);
    border-radius: 10px;
    padding: 10px;
    margin-top: 10px;
  }
  .call-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    font-size: 12px;
    margin-bottom: 8px;
  }
  .call-title { color: var(--accent-strong); font-weight: 700; }
  .call-time { color: var(--text-muted); font-variant-numeric: tabular-nums; }
  .call pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    padding: 8px;
    border-radius: 8px;
    color: var(--text-muted);
    font-size: 11px;
    line-height: 1.4;
  }
  .json-section {
    margin-top: 8px;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .json-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 10px;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
  }
  .json-header:hover { background: color-mix(in srgb, var(--bg-elevated) 70%, var(--accent-soft)); }
  .json-toggle {
    display: inline-block;
    width: 14px;
    height: 14px;
    line-height: 14px;
    text-align: center;
    color: var(--text-faint);
    font-size: 10px;
  }
  .json-header.collapsed .json-toggle::after { content: '▶'; }
  .json-header:not(.collapsed) .json-toggle::after { content: '▼'; }
  .json-header-label { font-size: 12px; color: var(--accent-strong); font-weight: 600; }
  .json-body {
    padding: 10px;
    max-height: 300px;
    overflow-y: auto;
    font-family: 'Courier New', monospace;
    font-size: 11px;
    line-height: 1.5;
  }
  .json-header.collapsed ~ .json-body { display: none; }
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
  }
  .status-ok {
    background: rgba(16, 185, 129, 0.15);
    color: var(--success);
  }
  .status-error {
    background: rgba(239, 68, 68, 0.15);
    color: var(--danger);
  }
  .status-ok::before { content: '✓'; }
  .status-error::before { content: '✕'; }

  .sidebar-divider {
    border-top: 1px solid var(--border);
    margin: 8px 0;
  }

  @media (max-width: 980px) {
    body {
      flex-direction: column;
      height: 100dvh;
      min-height: 100dvh;
      overflow: hidden;
    }
    #mobile-topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      height: 56px;
      padding: 8px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-header);
      backdrop-filter: blur(10px);
      position: sticky;
      top: 0;
      z-index: 90;
    }
    #mobile-backdrop {
      position: fixed;
      inset: 56px 0 0 0;
      background: var(--overlay);
      z-index: 79;
    }
    body.sidebar-open #mobile-backdrop {
      display: block;
    }
    #sidebar {
      position: fixed;
      top: 56px;
      left: 0;
      bottom: 0;
      width: min(88vw, 320px);
      min-width: 0;
      max-height: none;
      height: auto;
      border-right: 1px solid var(--border);
      border-bottom: 0;
      z-index: 80;
      transform: translateX(-105%);
      transition: transform .28s ease;
      box-shadow: 0 20px 36px rgba(0, 0, 0, 0.28);
    }
    body.sidebar-open #sidebar {
      transform: translateX(0);
    }
    #sessions {
      max-height: none;
    }
    #sidebar .brand .brand-logo {
      width: 30px;
      height: 30px;
    }
    #usage {
      display: none;
    }
    #main {
      height: auto;
      min-height: 0;
      flex: 1;
    }
    header {
      flex-direction: column;
      align-items: flex-start;
      gap: 10px;
    }
    .persona-box {
      width: 100%;
      align-items: flex-start;
    }
    #persona,
    #theme-toggle-wrap,
    #persona-desc {
      width: 100%;
      max-width: 100%;
      text-align: left;
    }
    #log {
      padding: 14px;
    }
    form {
      padding: 12px;
      position: sticky;
      bottom: 0;
      z-index: 20;
    }
    textarea {
      min-height: 52px;
    }
  }
</style>
</head>
<body>
<div id="welcome-splash" aria-live="polite" role="status">
  <div class="welcome-panel">
    <img class="welcome-logo" src="/complog.svg" alt="Work IQ logo">
    <h1 class="welcome-title">Welcome to Northbridge</h1>
    <p class="welcome-sub">Preparing your workspace...</p>
    <div class="welcome-bar" aria-hidden="true">
      <div class="welcome-progress"></div>
    </div>
  </div>
</div>

<div id="mobile-topbar">
  <button id="mobile-menu-btn" type="button" aria-label="Open chats menu" aria-expanded="false">☰</button>
  <div id="mobile-title">Work IQ Orchestrator</div>
  <div style="width:40px;height:40px;"></div>
</div>

<div id="mobile-backdrop" aria-hidden="true"></div>

<div id="sidebar">
  <div class="brand">
    <div class="brand-header">
      <div class="brand-text">
        <h2 class="brand-title">Work IQ Orchestrator</h2>
        <div class="sub">NorthBridge Health Network</div>
      </div>
      <img class="brand-logo" src="/complog.svg" alt="NorthBridge Health Network logo">
    </div>
  </div>
  <button id="new-chat" type="button">+ New chat</button>
  <button id="clear-all" type="button">🗑 Clear all</button>
  <div class="sessions-label">Chats</div>
  <div id="sessions"></div>
  <div class="sidebar-divider"></div>

<aside id="usage" aria-live="polite">
  <div class="title">Usage this session</div>
  <div class="row"><span class="label">Turns <span class="info" title="How many questions you have sent in this session.">i</span></span><span class="value" id="u-turns">0</span></div>
  <div class="row"><span class="label">Prompt tokens <span class="info" title="Tokens in your question and conversation context sent to the model.">i</span></span><span class="value" id="u-prompt">0</span></div>
  <div class="row"><span class="label">Completion tokens <span class="info" title="Tokens generated by the model in its answer.">i</span></span><span class="value" id="u-completion">0</span></div>
  <div class="row"><span class="label">Total tokens <span class="info" title="Prompt tokens + completion tokens for the turn.">i</span></span><span class="value" id="u-total">0</span></div>
  <div class="row" id="u-remaining-row"><span class="label">Tokens remaining <span class="info" title="Remaining token budget for the current chat (if limit enabled).">i</span></span><span class="value" id="u-remaining">Unlimited</span></div>
  <div class="row" id="u-cooldown-row"><span class="label">Cooldown <span class="info" title="Time until you can send the next message.">i</span></span><span class="value" id="u-cooldown">Ready</span></div>
  <div class="row"><span class="label">Last latency <span class="info" title="How long the most recent request took end to end.">i</span></span><span class="value" id="u-latency">0 ms</span></div>
  <div class="hint">Use this as a proxy for model credits consumed.</div>
</aside>
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
      <label for="theme-toggle">Theme</label>
      <div id="theme-toggle-wrap">
        <span class="chip" aria-hidden="true">☀️</span>
        <label class="theme-switch">
          <input id="theme-toggle" type="checkbox" aria-label="Toggle Night mode">
          <span class="theme-slider"></span>
        </label>
        <span class="chip" aria-hidden="true">🌙</span>
      </div>
      <div id="persona-desc"></div>
      <div id="persona-lock"></div>
    </div>
  </header>

  <div id="log"></div>

<form id="form">
  <textarea id="q" placeholder="Ask something — e.g. 'what's blocking PPAP qualification?'" required></textarea>
  <button id="send" type="submit">Send</button>
</form>
</div>

<div id="trail-modal" aria-hidden="true" role="dialog" aria-label="Execution trail details">
  <div class="trail-panel">
    <div class="trail-head">
      <h3 id="trail-title">Execution trail</h3>
      <button class="trail-close" id="trail-close" type="button">Close</button>
    </div>
    <div class="trail-body" id="trail-body"></div>
  </div>
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
const usageState = {
  turns: 0,
  promptTokens: 0,
  completionTokens: 0,
  totalTokens: 0,
  tokenLimit: 0,
  tokensRemaining: null,
};
const SERVER_CONFIG = "__WORKIQ_SERVER_CONFIG__";
const uiConfig = (SERVER_CONFIG && SERVER_CONFIG.ui) || {};
const clientCooldownEnabled = Boolean(uiConfig.enable_client_cooldown);
const showCooldownTimer = Boolean(uiConfig.show_cooldown_timer);
const showTokenRemaining = Boolean(uiConfig.show_token_remaining);
const serverTokenLimit = Number(SERVER_CONFIG.token_limit || 0);
const serverCooldownSeconds = Number(SERVER_CONFIG.cooldown_seconds || 0);
const uTurns = document.getElementById('u-turns');
const uPrompt = document.getElementById('u-prompt');
const uCompletion = document.getElementById('u-completion');
const uTotal = document.getElementById('u-total');
const uRemaining = document.getElementById('u-remaining');
const uRemainingRow = document.getElementById('u-remaining-row');
const uCooldown = document.getElementById('u-cooldown');
const uCooldownRow = document.getElementById('u-cooldown-row');
const uLatency = document.getElementById('u-latency');
let cooldownUntilMs = 0;
let cooldownTimer = null;
const trailModal = document.getElementById('trail-modal');
const trailBody = document.getElementById('trail-body');
const trailTitle = document.getElementById('trail-title');
const trailClose = document.getElementById('trail-close');
const themeToggle = document.getElementById('theme-toggle');
const themeColorMeta = document.querySelector('meta[name="theme-color"]');
const welcomeSplash = document.getElementById('welcome-splash');
const mobileMenuBtn = document.getElementById('mobile-menu-btn');
const mobileBackdrop = document.getElementById('mobile-backdrop');
const main = document.getElementById('main');
const THEME_STORE_KEY = 'workiq_theme_v1';

function isMobileLayout() {
  return window.matchMedia('(max-width: 980px)').matches;
}

function setSidebarOpen(open) {
  if (!isMobileLayout()) {
    document.body.classList.remove('sidebar-open');
    if (mobileMenuBtn) mobileMenuBtn.setAttribute('aria-expanded', 'false');
    return;
  }
  document.body.classList.toggle('sidebar-open', Boolean(open));
  if (mobileMenuBtn) mobileMenuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeSidebar() {
  setSidebarOpen(false);
}

function toggleSidebar() {
  const isOpen = document.body.classList.contains('sidebar-open');
  setSidebarOpen(!isOpen);
}

function startWelcomeSplash() {
  if (!welcomeSplash) return;
  window.setTimeout(() => {
    welcomeSplash.classList.add('is-exiting');
    window.setTimeout(() => {
      welcomeSplash.classList.add('is-hidden');
      welcomeSplash.setAttribute('aria-hidden', 'true');
    }, 700);
  }, 3000);
}

function themeColorFor(theme) {
  return theme === 'ops' ? '#0b1220' : '#f4f7fb';
}

function applyTheme(theme, persist) {
  const selected = (theme === 'ops') ? 'ops' : 'clinical';
  document.documentElement.setAttribute('data-theme', selected);
  if (themeToggle) themeToggle.checked = selected === 'ops';
  if (themeColorMeta) themeColorMeta.setAttribute('content', themeColorFor(selected));
  if (persist !== false) {
    try {
      localStorage.setItem(THEME_STORE_KEY, selected);
    } catch (err) {
      // Ignore storage failures and keep runtime theme only.
    }
  }
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
      console.warn('service worker registration failed', err);
    });
  });
}

if (themeToggle) {
  themeToggle.addEventListener('change', () => {
    applyTheme(themeToggle.checked ? 'ops' : 'clinical', true);
  });
}

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
    if (r.status === 401 && data && data.login_url) {
      window.location = data.login_url;
      return;
    }
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
  closeSidebar();
  q.focus();
}

function newChat(persona) {
  const s = {
    id: newId(),
    persona: persona || personaSel.value || defaultPersona,
    title: 'New chat',
    html: '',
    trails: {},
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

function toPrettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (err) {
    return String(value);
  }
}

function formatArgs(args) {
  if (!args || typeof args !== 'object') return escape(String(args || ''));
  if (Array.isArray(args)) {
    return args.map(item => escape(String(item))).join('\n');
  }
  // Format as plain key: value pairs
  const pairs = Object.entries(args).map(([k, v]) => {
    let val;
    if (typeof v === 'object' && v !== null) {
      val = JSON.stringify(v);
    } else {
      val = String(v);
    }
    return escape(k) + ': ' + escape(val);
  });
  return pairs.join('\n');
}

function formatPreviewValue(val) {
  if (val === null || val === undefined) return '(empty)';
  if (typeof val === 'string') return val;
  if (typeof val === 'number' || typeof val === 'boolean') return String(val);
  if (Array.isArray(val)) {
    // For arrays of objects, show key: value pairs per item
    if (val.length === 0) return '(empty array)';
    if (typeof val[0] === 'object' && val[0] !== null) {
      return val.map((item, i) => {
        if (typeof item === 'object') {
          const pairs = Object.entries(item).map(([k, v]) => k + ': ' + String(v)).join(', ');
          return pairs;
        }
        return String(item);
      }).join('\n');
    }
    return val.join(', ');
  }
  if (typeof val === 'object') {
    // For plain objects, show as key: value pairs
    const pairs = Object.entries(val).map(([k, v]) => {
      if (typeof v === 'object') return k + ': ' + JSON.stringify(v);
      return k + ': ' + String(v);
    });
    return pairs.join('\n');
  }
  return String(val);
}

function renderTrailModal(trailEntry) {
  if (!trailEntry) return;
  const calls = Array.isArray(trailEntry.tool_calls) ? trailEntry.tool_calls : [];
  trailTitle.textContent = 'Execution trail #' + trailEntry.turn;
  const summaryHtml =
    '<div class="trail-summary">' +
      '<div class="trail-kpi"><div class="k">Question</div><div class="v" style="word-break:break-word;font-weight:400;margin-top:6px;">' + escape((trailEntry.question || '').substring(0, 60)) + (trailEntry.question && trailEntry.question.length > 60 ? '...' : '') + '</div></div>' +
      '<div class="trail-kpi"><div class="k">Persona</div><div class="v">' + escape(trailEntry.persona || '') + '</div></div>' +
      '<div class="trail-kpi"><div class="k">Tool calls</div><div class="v">' + String(calls.length) + '</div></div>' +
      '<div class="trail-kpi"><div class="k">Turn latency</div><div class="v">' + Number(trailEntry.latency_ms || 0).toFixed(0) + ' ms</div></div>' +
    '</div>';

  const callsHtml = calls.length
    ? calls.map(call => {
        const statusClass = (call.status === 'error' ? 'status-error' : 'status-ok');
        const argsText = formatArgs(call.args || {});
        const resultPreviewVal = call.result_preview ? formatPreviewValue(call.result_preview) : '';
        const callId = call.call_id ? escape(String(call.call_id)) : '—';
        const errorMsg = call.error ? escape(String(call.error)) : null;
        return (
          '<div class="call">' +
            '<div class="call-head">' +
              '<div>' +
                '<div style="color:var(--accent-strong);font-weight:700;">' + String(call.index || '?') + '. ' + escape(String(call.tool || 'unknown')) + '</div>' +
                '<div style="font-size:10px;color:var(--text-faint);margin-top:2px;">call: ' + callId + '</div>' +
              '</div>' +
              '<div style="text-align:right;">' +
                '<div class="' + statusClass + ' status-badge">' + (call.status === 'error' ? 'Error' : 'Success') + '</div>' +
                '<div style="font-size:11px;color:var(--text-muted);margin-top:4px;font-variant-numeric:tabular-nums;">' + Number(call.duration_ms || 0).toFixed(2) + ' ms</div>' +
              '</div>' +
            '</div>' +
            '<div class="json-section">' +
              '<div class="json-header" onclick="this.classList.toggle(\'collapsed\');"><span class="json-toggle"></span><span class="json-header-label">Arguments</span></div>' +
              '<div class="json-body" style="color:var(--text-faint);white-space:pre-wrap;word-wrap:break-word;">' + argsText + '</div>' +
            '</div>' +
            (resultPreviewVal ? '<div class="json-section"><div class="json-header" onclick="this.classList.toggle(\'collapsed\');"><span class="json-toggle"></span><span class="json-header-label">Result preview</span></div><div class="json-body" style="color:var(--text-faint);white-space:pre-wrap;word-wrap:break-word;">' + resultPreviewVal + '</div></div>' : '') +
            (errorMsg ? '<div style="margin-top:8px;padding:8px;background:var(--danger-soft);border-left:2px solid var(--danger);color:var(--danger);font-size:11px;"><span style="color:var(--danger);font-weight:600;">Error:</span> ' + errorMsg + '</div>' : '') +
          '</div>'
        );
      }).join('')
    : '<div class="call" style="text-align:center;padding:20px;color:var(--text-faint);">No tool calls were made for this turn.</div>';

  trailBody.innerHTML = summaryHtml + callsHtml;
  trailModal.classList.add('open');
  trailModal.setAttribute('aria-hidden', 'false');
}

function hideTrailModal() {
  trailModal.classList.remove('open');
  trailModal.setAttribute('aria-hidden', 'true');
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

function refreshUsage(latencyMs) {
  uTurns.textContent = String(usageState.turns);
  uPrompt.textContent = String(usageState.promptTokens);
  uCompletion.textContent = String(usageState.completionTokens);
  uTotal.textContent = String(usageState.totalTokens);
  if (showTokenRemaining && usageState.tokenLimit > 0) {
    uRemainingRow.style.display = 'flex';
    uRemaining.textContent = String(Math.max(Number(usageState.tokensRemaining || 0), 0));
  } else if (showTokenRemaining) {
    uRemainingRow.style.display = 'flex';
    uRemaining.textContent = 'Unlimited';
  } else {
    uRemainingRow.style.display = 'none';
  }
  uLatency.textContent = Number.isFinite(latencyMs) ? `${latencyMs.toFixed(0)} ms` : '0 ms';
}

function updateCooldownUi() {
  if (!showCooldownTimer) {
    uCooldownRow.style.display = 'none';
    return;
  }
  uCooldownRow.style.display = 'flex';
  const now = Date.now();
  const remainingMs = Math.max(cooldownUntilMs - now, 0);
  const remainingSec = Math.ceil(remainingMs / 1000);
  if (remainingSec > 0) {
    uCooldown.textContent = `${remainingSec}s`;
    if (clientCooldownEnabled) send.disabled = true;
  } else {
    uCooldown.textContent = 'Ready';
    if (clientCooldownEnabled) send.disabled = false;
    if (cooldownTimer) {
      clearInterval(cooldownTimer);
      cooldownTimer = null;
    }
  }
}

function setCooldown(seconds) {
  const sec = Number(seconds || 0);
  if (sec <= 0) {
    cooldownUntilMs = 0;
    updateCooldownUi();
    return;
  }
  cooldownUntilMs = Date.now() + (sec * 1000);
  updateCooldownUi();
  if (!cooldownTimer) {
    cooldownTimer = setInterval(updateCooldownUi, 250);
  }
}
newChatBtn.addEventListener('click', () => newChat(personaSel.value || defaultPersona));

if (mobileMenuBtn) {
  mobileMenuBtn.addEventListener('click', toggleSidebar);
}
if (mobileBackdrop) {
  mobileBackdrop.addEventListener('click', closeSidebar);
}
if (main) {
  main.addEventListener('click', () => {
    if (isMobileLayout()) closeSidebar();
  });
}
window.addEventListener('resize', () => {
  if (!isMobileLayout()) closeSidebar();
});

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
    trails: {},
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
  if (clientCooldownEnabled && Date.now() < cooldownUntilMs) {
    const secondsLeft = Math.ceil((cooldownUntilMs - Date.now()) / 1000);
    add('error', 'cooldown active: try again in ' + String(secondsLeft) + 's');
    return;
  }
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
    const raw = await r.text();
    let data = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      data = null;
    }
    if (r.status === 401 && data && data.login_url) {
      window.location = data.login_url;
      return;
    }
    if (!r.ok) {
      if (r.status === 429) {
        const retryAfter = Number(r.headers.get('Retry-After') || 0);
        if (retryAfter > 0) setCooldown(retryAfter);
      }
      throw new Error((data && data.detail) || raw || ('HTTP ' + r.status));
    }
    const turnUsage = (data && data.usage) || {};
    usageState.turns += 1;
    const turnNumber = usageState.turns;
    usageState.promptTokens += Number(turnUsage.prompt_tokens || 0);
    usageState.completionTokens += Number(turnUsage.completion_tokens || 0);
    usageState.totalTokens += Number(turnUsage.total_tokens || 0);
    usageState.tokenLimit = Number((data && data.token_limit) || serverTokenLimit || 0);
    usageState.tokensRemaining = (data && data.tokens_remaining) == null ? null : Number(data.tokens_remaining);
    const nextAllowedAt = Number((data && data.next_allowed_at) || 0);
    if (nextAllowedAt > 0) {
      const remaining = Math.max(nextAllowedAt - (Date.now() / 1000), 0);
      setCooldown(remaining);
    } else if (serverCooldownSeconds > 0) {
      setCooldown(serverCooldownSeconds);
    }
    refreshUsage(Number(data && data.latency_ms));
    placeholder.classList.remove('thinking');
    let answer = ((data && data.answer) || raw || '(no answer)').replace(/https:\/\/simulator\.local\//g, '/citation/');
    placeholder.innerHTML = marked.parse(answer);
    placeholder.querySelectorAll('a[href^="/citation/"]').forEach(a => a.target = '_blank');

    if (!s.trails) s.trails = {};
    const trailEntry = {
      turn: turnNumber,
      question: text,
      persona: persona,
      latency_ms: Number(data && data.latency_ms) || 0,
      turn_started_at: (data && data.turn_started_at) || null,
      tool_calls: (data && data.trail) || [],
    };
    s.trails[String(turnNumber)] = trailEntry;
    const toolCallCount = Array.isArray(trailEntry.tool_calls) ? trailEntry.tool_calls.length : 0;
    const trailNote = document.createElement('div');
    trailNote.className = 'trail-row';
    trailNote.innerHTML =
      '<span>trail</span>' +
      '<button class="trail-num" type="button" data-turn="' + String(turnNumber) + '">' + String(turnNumber) + '</button>' +
      '<span class="trail-meta">' + String(toolCallCount) + ' tool call' + (toolCallCount === 1 ? '' : 's') +
      ' • ' + Number(trailEntry.latency_ms).toFixed(0) + ' ms</span>';
    placeholder.appendChild(trailNote);
  } catch (err) {
    placeholder.remove();
    add('error', 'error: ' + escape(String(err.message || err)));
  } finally {
    persistActiveHtml();
    if (!clientCooldownEnabled || Date.now() >= cooldownUntilMs) {
      send.disabled = false;
    }
    q.focus();
  }
});

log.addEventListener('click', (e) => {
  const btn = e.target.closest('.trail-num');
  if (!btn) return;
  const s = activeSession();
  if (!s || !s.trails) return;
  const turn = btn.getAttribute('data-turn');
  if (!turn) return;
  renderTrailModal(s.trails[turn]);
});

trailClose.addEventListener('click', hideTrailModal);
trailModal.addEventListener('click', (e) => {
  if (e.target === trailModal) hideTrailModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    hideTrailModal();
    closeSidebar();
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
  startWelcomeSplash();
  closeSidebar();

  let savedTheme = 'clinical';
  try {
    savedTheme = localStorage.getItem(THEME_STORE_KEY) || document.documentElement.getAttribute('data-theme') || 'clinical';
  } catch (err) {
    savedTheme = document.documentElement.getAttribute('data-theme') || 'clinical';
  }
  applyTheme(savedTheme, false);

  usageState.tokenLimit = serverTokenLimit;
  usageState.tokensRemaining = serverTokenLimit > 0 ? serverTokenLimit : null;
  refreshUsage(NaN);
  if (serverCooldownSeconds > 0) {
    setCooldown(0);
  } else {
    updateCooldownUi();
  }
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
async def index(request: Request):
    auth = _require_browser_auth(request)
    if auth is not None:
        return auth
    server_config = {
      "token_limit": TOKEN_LIMIT_PER_SESSION,
      "cooldown_seconds": COOLDOWN_SECONDS,
      "ui": {
        "enable_client_cooldown": UI_ENABLE_CLIENT_COOLDOWN,
        "show_cooldown_timer": UI_SHOW_COOLDOWN_TIMER,
        "show_token_remaining": UI_SHOW_TOKEN_REMAINING,
      },
    }
    return INDEX_HTML.replace('"__WORKIQ_SERVER_CONFIG__"', json.dumps(server_config))


@app.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    return JSONResponse(
      {
        "name": PWA_APP_NAME,
        "short_name": PWA_SHORT_NAME,
        "description": "Work IQ orchestrator chat UI for mobile and desktop.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": PWA_BACKGROUND_COLOR,
        "theme_color": PWA_THEME_COLOR,
        "icons": [
          {
            "src": "/complog.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
          }
        ],
      },
      media_type="application/manifest+json",
      headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
async def service_worker() -> Response:
    script = f"""const CACHE_NAME = '{PWA_CACHE_NAME}';
const SHELL_URLS = [
  '/',
  '/manifest.webmanifest',
  '/complog.svg',
  'https://cdn.jsdelivr.net/npm/marked/marked.min.js',
];

self.addEventListener('install', (event) => {{
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    ))
  );
  self.clients.claim();
}});

self.addEventListener('fetch', (event) => {{
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  const isSameOrigin = url.origin === self.location.origin;

  if (!isSameOrigin) {{
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
    return;
  }}

  if (url.pathname.startsWith('/ask')) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {{
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      }})
      .catch(() => caches.match(event.request))
  );
}});
"""
    return Response(
      content=script,
      media_type="application/javascript",
      headers={"Cache-Control": "no-cache"},
    )


@app.get("/complog.svg")
async def pwa_icon() -> Response:
    return FileResponse(
      path=PWA_ICON_PATH,
      media_type="image/svg+xml",
      headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/citation/{kind}/{cid}", response_class=HTMLResponse)
async def citation_detail(kind: str, cid: str, request: Request) -> str:
    """Serve a simple HTML page showing the full citation record."""
    auth = _require_browser_auth(request)
    if auth is not None:
        return auth
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
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "web:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    proxy_headers=True,
    forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
