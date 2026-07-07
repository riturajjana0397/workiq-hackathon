r"""
Minimal A2A JSON-RPC server that wraps a Microsoft Agent Framework `ChatAgent`.

Purpose
-------
Expose any MAF agent as an A2A sub-agent so the Work IQ orchestrator can call it
via `agent_framework_a2a.A2AAgent`. The wire contract matches
`simulator/a2a_server.py` (JSON-RPC 2.0, `SendMessage` / `message/send`, agent
card at `/.well-known/agent-card.json`), which in turn matches both the a2a
JSON spec dialect and the a2a-sdk 1.x protobuf dialect that MAF's A2AAgent
client emits.

Bridging model
--------------
`BaseHTTPRequestHandler` is sync; MAF agents are async. We keep an asyncio
event loop alive on the main thread and dispatch every SendMessage into it via
`asyncio.run_coroutine_threadsafe`. This lets async tool surfaces (MCP
subprocesses, remote A2A clients) stay open for the process lifetime.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable

# JSON-RPC error codes mirrored from simulator/a2a_server.py so error shapes
# stay consistent across every Work IQ A2A endpoint.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SEND_METHODS = {"SendMessage", "message/send"}
AGENT_CARD_PATHS = {"/.well-known/agent-card.json", "/.well-known/agent.json"}
RPC_PATHS = {"/a2a/", "/a2a", "/"}


# Handler contract: given the question text and the free-form metadata dict
# from the A2A message, return a dict with keys:
#   response:   final string to hand back
#   citations:  list[dict] (may be empty)
#   metadata:   optional dict merged into the reply metadata
AgentHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _text_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if (
            part.get("kind") == "text"
            or part.get("type") == "text"
            or isinstance(part.get("text"), str)
        ):
            txt = part.get("text")
            if isinstance(txt, str):
                chunks.append(txt)
    return "\n".join(chunks).strip()


def _is_proto_dialect(message: dict[str, Any]) -> bool:
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


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _agent_card(name: str, description: str, public_url: str, skill_id: str) -> dict[str, Any]:
    return {
        "protocolVersion": "1.0",
        "name": name,
        "description": description,
        "url": public_url,
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": skill_id,
                "name": name,
                "description": description,
                "tags": ["workiq", "sub-agent"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
        ],
    }


def _make_handler_class(
    handler: AgentHandler,
    loop: asyncio.AbstractEventLoop,
    agent_name: str,
    agent_description: str,
    skill_id: str,
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = f"WorkIQSub/{agent_name}"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            sys.stderr.write(f"[{agent_name}] " + (fmt % args) + "\n")

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in AGENT_CARD_PATHS:
                host = self.headers.get(
                    "Host",
                    f"{self.server.server_address[0]}:{self.server.server_address[1]}",
                )
                self._send_json(
                    200,
                    _agent_card(agent_name, agent_description, f"http://{host}/a2a/", skill_id),
                )
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in RPC_PATHS:
                self._send_json(404, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (ValueError, TypeError):
                self._send_json(200, _error_response(None, PARSE_ERROR, "invalid Content-Length"))
                return
            raw = self.rfile.read(length) if length else b""

            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_json(200, _error_response(None, PARSE_ERROR, f"invalid JSON: {e}"))
                return

            response = self._dispatch(payload)
            if response is None:
                self.send_response(204)
                self.end_headers()
                return
            self._send_json(200, response)

        def _dispatch(self, request: Any) -> dict[str, Any] | None:
            if not isinstance(request, dict):
                return _error_response(None, INVALID_REQUEST, "request must be a JSON object")
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
                    req_id, INVALID_PARAMS, "params must be an object"
                )

            try:
                if method in SEND_METHODS:
                    result = self._handle_send(params)
                else:
                    raise _RpcError(METHOD_NOT_FOUND, f"unknown method '{method}'")
            except _RpcError as e:
                return None if is_notification else _error_response(req_id, e.code, e.message, e.data)
            except Exception as e:  # noqa: BLE001
                return None if is_notification else _error_response(req_id, INTERNAL_ERROR, str(e))

            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        def _handle_send(self, params: dict[str, Any]) -> dict[str, Any]:
            message = params.get("message")
            if not isinstance(message, dict):
                raise _RpcError(INVALID_PARAMS, "params.message required")
            question = _text_from_message(message)
            if not question:
                raise _RpcError(INVALID_PARAMS, "message.parts must contain text")

            context_id = (
                message.get("contextId")
                or message.get("context_id")
                or params.get("contextId")
                or f"ctx-{uuid.uuid4().hex[:12]}"
            )

            msg_meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            future = asyncio.run_coroutine_threadsafe(handler(question, msg_meta), loop)
            try:
                agent_result = future.result(timeout=180)
            except Exception as e:  # noqa: BLE001
                raise _RpcError(INTERNAL_ERROR, f"sub-agent failed: {e}") from e

            response_text = str(agent_result.get("response", ""))
            citations = agent_result.get("citations") or []
            extra_meta = agent_result.get("metadata") or {}
            reply_meta = {"citations": citations, **extra_meta}

            message_id = f"msg-{uuid.uuid4().hex[:12]}"
            if _is_proto_dialect(message):
                return {
                    "message": {
                        "messageId": message_id,
                        "contextId": context_id,
                        "role": "ROLE_AGENT",
                        "parts": [
                            {"text": response_text},
                            {"data": {"citations": citations}},
                        ],
                        "metadata": reply_meta,
                    }
                }
            return {
                "kind": "message",
                "role": "agent",
                "messageId": message_id,
                "contextId": context_id,
                "parts": [
                    {"kind": "text", "text": response_text},
                    {"kind": "data", "data": {"citations": citations}},
                ],
                "metadata": reply_meta,
            }

    return _Handler


async def serve_forever(
    *,
    host: str,
    port: int,
    agent_name: str,
    agent_description: str,
    skill_id: str,
    setup: Callable[[], Awaitable[AgentHandler]],
) -> None:
    """Serve a sub-agent over A2A on (host, port) until the process exits.

    `setup` runs inside the asyncio loop and returns the async handler that
    will process every SendMessage. It's an async factory so callers can open
    long-lived tool surfaces (MCP subprocesses, remote A2A clients) inside a
    single `async with` scope that stays alive for the server's lifetime.
    """
    loop = asyncio.get_running_loop()
    handler = await setup()

    handler_cls = _make_handler_class(
        handler=handler,
        loop=loop,
        agent_name=agent_name,
        agent_description=agent_description,
        skill_id=skill_id,
    )
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    bound_host, bound_port = httpd.server_address
    sys.stderr.write(
        f"[{agent_name}] listening=http://{bound_host}:{bound_port}/a2a/ "
        f"card=http://{bound_host}:{bound_port}/.well-known/agent-card.json\n"
    )

    stop_event = asyncio.Event()

    def _serve() -> None:
        try:
            httpd.serve_forever()
        finally:
            loop.call_soon_threadsafe(stop_event.set)

    thread = threading.Thread(target=_serve, name=f"{agent_name}-http", daemon=True)
    thread.start()
    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
