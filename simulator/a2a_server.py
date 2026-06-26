r"""
Local Work IQ Simulator — A2A (Agent-to-Agent) JSON-RPC server (scenario-driven).

Metadata
--------
Created:   15-JUN-2026
Component: a2a_server.py
Role:      Exposes the simulator's Work IQ **Chat** capability over the **A2A protocol**
           so agent-to-agent callers can reach it exactly as they would the real Work IQ
           A2A gateway. Companion to `server.py` (which exposes the same engine over MCP).
           Faithful to the documented Work IQ A2A contract:
             * Wire format: JSON-RPC 2.0; POST the method in the BODY to the base URL
               (`/a2a/`), not in the path.
             * Method names: `SendMessage` (A2A v1.0, selected via `A2A-Version: 1.0`
               header) and the open-standard alias `message/send` (a2a-protocol.org).
             * Multi-turn: pass `contextId` from the prior response into the next message.
             * Auth (real service): scope `WorkIQAgent.Ask`, audience
               `api://workiq.svc.cloud.microsoft`. The SIMULATOR does NOT enforce auth —
               it is a local mock; persona scoping stands in for identity.
           Agent discovery: an A2A Agent Card is served at
           `/.well-known/agent-card.json` (and the legacy `/.well-known/agent.json`).

Scope note
----------
A2A maps to the Work IQ **Chat** domain (a finished, cited answer). Tool actions
(`fetch` / `create_entity` / `update_entity`) belong to the MCP surface (`server.py`)
and are intentionally NOT exposed here.

Environment
-----------
  WORKIQ_SIM_SCENARIO   Scenario dir (abs or relative to this file). Default c1-northbridge.
  WORKIQ_SIM_PERSONA    Default persona id for permission trimming. Default new_pm.
                        Unset/"all" => full visibility. Override per-request via the
                        message metadata `persona` field or the `X-WorkIQ-Persona` header.
  WORKIQ_A2A_HOST       Bind host. Default 127.0.0.1.
  WORKIQ_A2A_PORT       Bind port. Default 8920. Use 0 for an ephemeral port.

Run:
    .\.venv\Scripts\python.exe simulator\a2a_server.py
Then POST JSON-RPC to http://127.0.0.1:8920/a2a/ or GET the agent card from
http://127.0.0.1:8920/.well-known/agent-card.json
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))

import engine  # noqa: E402

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A2A methods we accept. `SendMessage` is the Work IQ v1.0 name; `message/send` is the
# open-standard (a2a-protocol.org) name. Both dispatch to the same handler.
SEND_METHODS = {"SendMessage", "message/send"}

AGENT_CARD_PATHS = {"/.well-known/agent-card.json", "/.well-known/agent.json"}
RPC_PATHS = {"/a2a/", "/a2a", "/"}


def _scenario_dir() -> Path:
    raw = os.environ.get("WORKIQ_SIM_SCENARIO")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (SERVER_DIR / p)
    return SERVER_DIR / "scenarios" / "c1-northbridge"


def _default_persona() -> str | None:
    p = os.environ.get("WORKIQ_SIM_PERSONA", "quality_pm")
    if not p or p.lower() == "all":
        return None
    return p


SCENARIO = engine.load_scenario(_scenario_dir())
DEFAULT_PERSONA = _default_persona()


def _agent_card(public_url: str) -> dict[str, Any]:
    """Build an A2A Agent Card describing this simulated Work IQ agent."""
    return {
        "protocolVersion": "1.0",
        "name": "workiq-simulator",
        "description": (
            "Local Work IQ simulator — answers questions grounded in the active persona's "
            "synthetic M365 work context (email, meetings, chats, files, people, and "
            "Dataverse tables) with citations and permission-aware trimming. Mirrors the "
            "real Work IQ Chat capability over A2A."
        ),
        "url": public_url,
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {
            "workiq_oauth": {
                "type": "oauth2",
                "description": (
                    "Real Work IQ requires scope WorkIQAgent.Ask "
                    "(audience api://workiq.svc.cloud.microsoft). The simulator does NOT "
                    "enforce auth; supply persona scoping via message metadata instead."
                ),
            }
        },
        "skills": [
            {
                "id": "ask_work_iq",
                "name": "Ask Work IQ",
                "description": (
                    "Ask a natural-language question; returns a cited answer grounded in "
                    "the caller persona's work context, with restricted sources trimmed."
                ),
                "tags": ["chat", "grounding", "citations", "m365", "work-context"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "Summarize the latest design-review decisions on the 45621-B program.",
                    "Who owns the qualification test plan and what is the open supplier risk?",
                ],
            }
        ],
    }


def _persona_from_params(params: dict[str, Any], header_persona: str | None) -> str | None:
    """Resolve the effective persona for a request.

    Precedence: message metadata `persona` -> params metadata `persona` ->
    X-WorkIQ-Persona header -> server default. Blank/whitespace values are treated as
    "not provided" (they fall through to the next source) so they can never silently
    widen visibility. The literal "all" (case-insensitive) means full visibility (None);
    in the real service that path is gated by the WorkIQAgent.Ask scope.
    """
    candidates: list[Any] = []
    message = params.get("message")
    if isinstance(message, dict) and isinstance(message.get("metadata"), dict):
        candidates.append(message["metadata"].get("persona"))
    if isinstance(params.get("metadata"), dict):
        candidates.append(params["metadata"].get("persona"))
    candidates.append(header_persona)

    for cand in candidates:
        if cand is None:
            continue
        s = str(cand).strip()
        if not s:
            continue
        if s.lower() == "all":
            return None
        return s
    return DEFAULT_PERSONA


def _text_from_message(message: Any) -> str:
    """Concatenate the text of every TextPart in an A2A message."""
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        # A2A v1.0 TextPart uses {"kind": "text", "text": ...}; tolerate {"type": "text"};
        # a2a-sdk 1.x (protobuf) serializes a bare {"text": ..., "metadata": {}} with no
        # discriminator, so also accept any part carrying a string `text` value.
        if part.get("kind") == "text" or part.get("type") == "text" or isinstance(part.get("text"), str):
            txt = part.get("text")
            if isinstance(txt, str):
                chunks.append(txt)
    return "\n".join(chunks).strip()


def _is_proto_dialect(message: dict[str, Any]) -> bool:
    """True if the caller speaks the protobuf a2a-sdk JSON dialect (no `kind` discriminators).

    The Microsoft Agent Framework's A2AAgent (a2a-sdk 1.x) emits enum roles like ROLE_USER and
    bare text parts; the A2A JSON spec uses `user` plus `{"kind": "text"}`. Detect so we can
    reply in the same dialect the caller parses.
    """
    if str(message.get("role", "")).upper().startswith("ROLE_"):
        return True
    for part in message.get("parts") or []:
        if (
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and "kind" not in part
            and "type" not in part
        ):
            return True
    return False


def _handle_send(params: dict[str, Any], header_persona: str | None) -> dict[str, Any]:
    """Handle SendMessage / message/send. Returns the JSON-RPC `result` (an A2A Message)."""
    message = params.get("message")
    if not isinstance(message, dict):
        raise _RpcError(INVALID_PARAMS, "params.message is required and must be an object")

    question = _text_from_message(message)
    if not question:
        raise _RpcError(INVALID_PARAMS, "params.message.parts must contain a non-empty text part")

    # Multi-turn continuity: reuse the caller's contextId if supplied, else mint one.
    context_id = (
        message.get("contextId")
        or message.get("context_id")
        or params.get("contextId")
        or f"ctx-{uuid.uuid4().hex[:12]}"
    )

    persona = _persona_from_params(params, header_persona)
    result = engine.ask(SCENARIO, question, persona_id=persona)

    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    meta = {
        "conversationId": result["conversationId"],
        "citations": result["citations"],
        "trimmed": result.get("trimmed", []),
        "persona": persona or "all",
    }

    # Two A2A JSON dialects in the wild:
    #   * JSON spec (a2a_e2e.py / curl): `result` is a bare Message with `kind` discriminators.
    #   * protobuf a2a-sdk 1.x (Microsoft Agent Framework's A2AAgent): `result` is a oneof
    #     {"message": <Message>}, no `kind` fields, enum role ROLE_AGENT, Part.data carries the
    #     payload directly. Reply in whichever dialect the caller used.
    if _is_proto_dialect(message):
        return {
            "message": {
                "messageId": message_id,
                "contextId": context_id,
                "role": "ROLE_AGENT",
                "parts": [
                    {"text": result["response"]},
                    {"data": {"citations": result["citations"]}},
                ],
                "metadata": meta,
            }
        }

    return {
        "kind": "message",
        "role": "agent",
        "messageId": message_id,
        "contextId": context_id,
        "parts": [
            {"kind": "text", "text": result["response"]},
            {"kind": "data", "data": {"citations": result["citations"]}},
        ],
        "metadata": meta,
    }


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def dispatch(request: Any, header_persona: str | None) -> dict[str, Any] | None:
    """Dispatch a single JSON-RPC request object. Returns a response dict, or None when
    (and only when) the request is a VALID notification (no `id`). An invalid request is
    never treated as a notification — it always yields an error with `id: null`."""
    if not isinstance(request, dict):
        return _error_response(None, INVALID_REQUEST, "request must be a JSON object")

    # Validate shape BEFORE deciding notification status: a structurally invalid object
    # is not a notification, so it must produce an Invalid Request error (id: null).
    if request.get("jsonrpc") != "2.0":
        return _error_response(request.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = request.get("method")
    if not isinstance(method, str):
        return _error_response(request.get("id"), INVALID_REQUEST, "method must be a string")

    req_id = request.get("id")
    is_notification = "id" not in request
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return None if is_notification else _error_response(
            req_id, INVALID_PARAMS, "params must be an object")

    try:
        if method in SEND_METHODS:
            result = _handle_send(params, header_persona)
        else:
            raise _RpcError(METHOD_NOT_FOUND, f"unknown method '{method}'")
    except _RpcError as e:
        return None if is_notification else _error_response(req_id, e.code, e.message, e.data)
    except Exception as e:  # noqa: BLE001 — surface engine faults as JSON-RPC errors
        return None if is_notification else _error_response(req_id, INTERNAL_ERROR, str(e))

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


class A2AHandler(BaseHTTPRequestHandler):
    server_version = "WorkIQSimA2A/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Route access logs to stderr so they never pollute stdout consumers.
        sys.stderr.write("[workiq-a2a] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in AGENT_CARD_PATHS:
            host = self.headers.get("Host", f"{self.server.server_address[0]}:{self.server.server_address[1]}")
            self._send_json(200, _agent_card(f"http://{host}/a2a/"))
            return
        self._send_json(404, {"error": "not found", "hint": "GET /.well-known/agent-card.json"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in RPC_PATHS:
            self._send_json(404, {"error": "not found", "hint": "POST JSON-RPC to /a2a/"})
            return

        length_raw = self.headers.get("Content-Length", 0) or 0
        try:
            length = int(length_raw)
        except (ValueError, TypeError):
            self._send_json(200, _error_response(None, PARSE_ERROR, "invalid Content-Length"))
            return
        raw = self.rfile.read(length) if length else b""
        header_persona = self.headers.get("X-WorkIQ-Persona")

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_json(200, _error_response(None, PARSE_ERROR, f"invalid JSON: {e}"))
            return

        if isinstance(payload, list):
            # JSON-RPC batch. An empty array is itself an Invalid Request (spec).
            if not payload:
                self._send_json(200, _error_response(
                    None, INVALID_REQUEST, "batch must contain at least one request"))
                return
            responses = [r for r in (dispatch(item, header_persona) for item in payload) if r is not None]
            # A batch of only notifications produces no response objects -> return nothing.
            if not responses:
                self.send_response(204)
                self.end_headers()
                return
            self._send_json(200, responses)
            return

        response = dispatch(payload, header_persona)
        # Notifications get HTTP 204 with no body.
        if response is None:
            self.send_response(204)
            self.end_headers()
            return
        self._send_json(200, response)


def build_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    h = host if host is not None else os.environ.get("WORKIQ_A2A_HOST", "127.0.0.1")
    p = port if port is not None else int(os.environ.get("WORKIQ_A2A_PORT", "8920"))
    return ThreadingHTTPServer((h, p), A2AHandler)


def main() -> None:
    httpd = build_server()
    host, port = httpd.server_address
    if DEFAULT_PERSONA and DEFAULT_PERSONA not in SCENARIO.persona_ids():
        print(
            f"[workiq-a2a][WARN] persona '{DEFAULT_PERSONA}' not in this scenario; it will "
            f"see only public content. Valid personas: {SCENARIO.persona_ids()}",
            file=sys.stderr,
        )
    print(
        f"[workiq-a2a] scenario={SCENARIO.root.name} default_persona={DEFAULT_PERSONA or 'all'} "
        f"golden={len(SCENARIO.golden)} listening=http://{host}:{port}/a2a/ "
        f"card=http://{host}:{port}/.well-known/agent-card.json",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
