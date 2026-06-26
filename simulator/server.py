r"""
Local Work IQ Simulator — MCP stdio server (scenario-driven).

Metadata
--------
Created:   14-JUN-2026
Component: server.py
Role:      Exposes the EXACT real Work IQ tool contract (`ask_work_iq`) plus the Tools
           surface the challenges need (`fetch`, `create_entity`, `update_entity`) over
           MCP stdio, backed by the synthetic engine. Drop-in replacement for the real
           `workiq` MCP server: participants only swap the `command`/`args` in their MCP
           config — the tool names and shapes are identical.

Environment
-----------
  WORKIQ_SIM_SCENARIO   Absolute or relative path to the scenario dir.
                        Default: scenarios/c1-northbridge (next to this file).
  WORKIQ_SIM_PERSONA    Active persona id for permission trimming
                        (ops_director | quality_pm | credentialing_lead | vendor_liaison).
                        Default: quality_pm. Unset/"all" => full visibility.
  OPENAI_API_KEY/...    Optional. Enables LLM fallback for ad-hoc (non-golden) questions.

Run:
    .\.venv\Scripts\python.exe simulator\server.py
Register in an MCP client (e.g. Copilot CLI) with command=python, args=[server.py].
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))

import engine  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402


def _scenario_dir() -> Path:
    raw = os.environ.get("WORKIQ_SIM_SCENARIO")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (SERVER_DIR / p)
    return SERVER_DIR / "scenarios" / "c1-northbridge"


def _persona() -> str | None:
    p = os.environ.get("WORKIQ_SIM_PERSONA", "quality_pm")
    if not p or p.lower() == "all":
        return None
    return p


SCENARIO = engine.load_scenario(_scenario_dir())
PERSONA = _persona()

mcp = FastMCP("workiq-simulator")


@mcp.tool()
def ask_work_iq(question: str, fileUrls: list[str] | None = None) -> str:
    """Ask a question to Microsoft 365 Copilot (Work IQ).

    Grounds the answer in the active persona's work context (email, meetings, chats,
    files, people, and the Dataverse milestone tracker). Returns JSON with 'response'
    (the answer), the 'conversationId', and 'citations' back to the source signals.

    This mirrors the real Work IQ `ask_work_iq` contract exactly so the simulator is a
    drop-in backend; `fileUrls` is accepted for contract parity (unused in the sim).
    """
    result = engine.ask(SCENARIO, question, persona_id=PERSONA)
    payload = {
        "response": result["response"],
        "conversationId": result["conversationId"],
        "citations": result["citations"],
    }
    return json.dumps(payload, indent=2)


@mcp.tool()
def fetch(table: str, filter: dict[str, Any] | None = None) -> str:
    """Read rows from a Work IQ Tools-backed table (e.g. the Dataverse milestone
    tracker). Optionally filter by exact field match. Returns JSON list of rows."""
    try:
        rows = engine.fetch(SCENARIO, table, filter)
    except ValueError as e:
        return _table_error(e)
    return json.dumps({"rows": rows, "count": len(rows)}, indent=2)


@mcp.tool()
def create_entity(table: str, record: dict[str, Any]) -> str:
    """Create (append) a row in a Work IQ Tools-backed table — e.g. open a tracked risk
    item in the Dataverse milestone tracker. Idempotent: re-creating the same logical
    row (same id, or same milestone+owner) returns the existing row instead of
    duplicating. Returns JSON describing whether a row was created."""
    try:
        res = engine.create_entity(SCENARIO, table, record, persist=False)
    except ValueError as e:
        return _table_error(e)
    return json.dumps(res, indent=2)


@mcp.tool()
def update_entity(table: str, id: str, patch: dict[str, Any]) -> str:
    """Patch fields on an existing row (by id) in a Work IQ Tools-backed table — e.g.
    move a milestone date or change a status. Returns JSON describing the update."""
    try:
        res = engine.update_entity(SCENARIO, table, id, patch, persist=False)
    except ValueError as e:
        return _table_error(e)
    return json.dumps(res, indent=2)


def _table_error(exc: Exception) -> str:
    """Return a structured error an LLM agent can self-correct from."""
    return json.dumps(
        {"error": str(exc), "available_tables": SCENARIO.table_names()},
        indent=2,
    )


if __name__ == "__main__":
    # Startup diagnostics go to stderr so they don't corrupt the stdio JSON-RPC stream.
    if PERSONA and PERSONA not in SCENARIO.persona_ids():
        print(
            f"[workiq-simulator][WARN] persona '{PERSONA}' is not defined in this scenario; "
            f"it will see only public (acl=all) content. Valid personas: {SCENARIO.persona_ids()}",
            file=sys.stderr,
        )
    if not SCENARIO.golden:
        print(
            "[workiq-simulator][WARN] scenario loaded 0 golden answers — check "
            "WORKIQ_SIM_SCENARIO points at a populated scenario directory.",
            file=sys.stderr,
        )
    print(
        f"[workiq-simulator] scenario={SCENARIO.root.name} persona={PERSONA or 'all'} "
        f"golden={len(SCENARIO.golden)} tools=ask_work_iq,fetch,create_entity,update_entity",
        file=sys.stderr,
    )
    mcp.run()
