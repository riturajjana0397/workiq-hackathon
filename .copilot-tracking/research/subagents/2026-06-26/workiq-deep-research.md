# WorkIQ Hackathon — Deep Research Document

**Status:** Complete
**Date:** 2026-06-26
**Scope:** simulator/ + agent/ — all 5 research categories
**Files read:** engine.py, server.py, a2a_server.py, requirements.txt, demo.py, tests/smoke.py, tests/mcp_e2e.py, tests/a2a_e2e.py, tests/validate_scenario.py, agent/workiq_agent.py, agent/web.py, README.md, simulator/README.md, scenarios/c1-northbridge/golden.json, scenarios/c1-northbridge/personas.json

---

## Research Topics

1. Functional Requirements
2. Non-Functional Requirements
3. Edge Cases
4. Security Considerations
5. Dependencies

---

## 1. FUNCTIONAL REQUIREMENTS

### 1.1 MCP Tool Surface (server.py)

**Evidence — server.py lines 1-130:**

```python
@mcp.tool()
def ask_work_iq(question: str, fileUrls: list[str] | None = None) -> str:
    """Ask a question to Microsoft 365 Copilot (Work IQ)..."""

@mcp.tool()
def fetch(table: str, filter: dict[str, Any] | None = None) -> str:
    """Read rows from a Work IQ Tools-backed table..."""

@mcp.tool()
def create_entity(table: str, record: dict[str, Any]) -> str:
    """Create (append) a row..."""

@mcp.tool()
def update_entity(table: str, id: str, patch: dict[str, Any]) -> str:
    """Patch fields on an existing row (by id)..."""
```

**4 MCP tools exposed over stdio:**
- `ask_work_iq(question, fileUrls?)` — grounded Q&A, cited answer, persona-aware
- `fetch(table, filter?)` — read rows from a Dataverse-style table with optional exact-match filter
- `create_entity(table, record)` — idempotent append to a table; dedupes on `id` and on `(milestone, owner)` for milestone_tracker
- `update_entity(table, id, patch)` — patch fields on existing row by id; atomically rekeys citation index if `id` changes

**`fileUrls` is accepted for contract parity but unused in the simulator** (server.py, docstring).

### 1.2 A2A Protocol Surface (a2a_server.py)

**Evidence — a2a_server.py lines 1-80:**

- Wire format: JSON-RPC 2.0, POST method in body to `/a2a/` (NOT in path)
- Two accepted method names: `SendMessage` (Work IQ A2A v1.0) and `message/send` (open-standard a2a-protocol.org alias)
- Agent Card served at `/.well-known/agent-card.json` AND `/.well-known/agent.json` (legacy)
- Multi-turn: `contextId` from prior response echoed back; new one minted if not supplied
- Batch JSON-RPC: array of requests supported; batch of only notifications yields HTTP 204

**A2A scopes only the Chat domain** (ask_work_iq equivalent). Tools (fetch/create/update) are intentionally NOT on A2A — they belong to MCP only (a2a_server.py scope note).

### 1.3 Golden-Answer System

**Evidence — engine.py lines 292-400:**

```python
def match_golden(sc: Scenario, question: str, threshold: float = 0.5) -> dict | None:
    """Return the best-matching golden entry at/above the fraction `threshold`."""
    best: dict | None = None
    best_key: tuple[int, float] = (-1, -1.0)
    for g in sc.golden:
        hits, frac = _match_stats(question, g)
        if frac >= threshold and (hits, frac) > best_key:
            best_key = (hits, frac)
            best = g
    return best
```

- Keyword-phrase matching: every word in a phrase must appear in the stemmed question (order-independent)
- Threshold: default 0.5 (50% of keyword phrases must match)
- Ranking: `(absolute hits, fraction)` — absolute hits wins on ties so short generic entries cannot hijack specific ones
- Deterministic: first entry in declaration order wins exact ties
- Morphology: light suffix stripper (`_stem()`) applied symmetrically to both keywords and question; `blocking`/`blockers` → `block`

**Golden entry schema (golden.json):**
```json
{
  "id": "Q1",
  "tier": "single-signal",
  "question": "...",
  "keywords": ["keyword phrase 1", "keyword phrase 2"],
  "answer": "full authorized answer",
  "citations": ["EML-001", "MTG-001"],
  "restricted_citations": ["EML-001"],
  "trimmed_answer": "redacted answer for unauthorized personas",
  "required_sources": 1,
  "tool": null
}
```

### 1.4 Persona-Based Permission Model

**Evidence — engine.py lines 215-240:**

```python
def can_see(record: dict, persona_id: str | None) -> bool:
    acl = _acl_of(record)
    if "all" in acl:
        return True
    if persona_id is None:
        return True
    return persona_id in acl
```

- Every fixture carries an `acl` list of persona ids or `["all"]`
- `persona_id=None` = full admin/dev visibility (unscoped session)
- `"all"` in acl → everyone can see it
- Persona sees fixture iff its id is in the acl
- Action items inherit the parent meeting's acl (engine.py `_build_index`)

**Persona resolution in A2A (a2a_server.py lines 143-175):**
Precedence chain: `message.metadata.persona` → `params.metadata.persona` → `X-WorkIQ-Persona` header → server default env var. Blank/whitespace falls through; `"all"` (case-insensitive) means full visibility.

### 1.5 Citation Resolution

**Evidence — engine.py lines 260-295:**

```python
def resolve_citations(sc, citation_ids, persona_id):
    """Returns (visible_citations, trimmed_ids)."""
    for cid in citation_ids:
        entry = sc.index.get(cid)
        if entry is None:
            continue  # unknown citation — silently skipped
        kind, record = entry
        if not can_see(record, persona_id):
            trimmed.append(cid)
            continue
        visible.append({
            "citation_id": cid, "source_index": source_index,
            "title": _title_for(kind, record), "kind": kind,
            "sensitivity": record.get("sensitivity", "internal"),
            "url": record.get("url", f"https://simulator.local/{kind}/{cid}"),
        })
```

- citation `sensitivity` defaults to `"internal"` if not in fixture
- `url` is a placeholder (`https://simulator.local/{kind}/{cid}`) when not set in fixture
- Unknown citation ids are silently skipped (no error raised)

### 1.6 RBAC / Governance Note

**Evidence — engine.py lines 447-480:**

```python
GOVERNANCE_NOTE = (
    "\n\n[Governance] {n} source(s) were withheld from this answer because the active "
    "persona ('{persona}') does not have access to restricted/customer-confidential "
    "material: {ids}. Switch to a leadership persona to see them."
)
```

Fail-closed logic when a persona is trimmed:
1. If `set(trimmed) <= set(restricted_citations)` AND `trimmed_answer` is authored → serve `trimmed_answer`
2. Otherwise → serve generic fail-closed message (starts with "A complete answer to this question draws on sources...")
3. Always append governance note with `n`, `persona label`, and withheld ids

### 1.7 LLM Fallback

**Evidence — engine.py lines 360-420:**

- Triggered only when no golden match AND `OPENAI_API_KEY` env var is set
- Uses `openai.OpenAI` (sync) with configurable `OPENAI_BASE_URL` and `MODEL` env vars
- Context: top 6 snippets from BM25-style term-overlap retrieval
- Graceful degradation (3 levels): golden → LLM → retrieval-only bullet list → empty message
- LLM fallback does NOT apply persona trimming beyond the retrieval step (snippets are permission-filtered before entering context)

### 1.8 CRUD on Tables

**Evidence — engine.py lines 555-680:**

**`fetch`:** exact field-match filter; raises `ValueError` for unknown tables.

**`create_entity`:**
- Idempotent on `id`: if id exists in index → returns `{created: False, reason: "id_exists"}`
- Rejects id collision with entities in OTHER tables/fixture types → `{created: False, reason: "id_collision"}`
- Logical dedupe for `milestone_tracker`: same `(milestone, owner)` pair → returns existing row
- Auto-generates id via `_next_id()` if not provided; uses max-suffix+1 (not len) to be sparse-safe
- Inherits ACL from first row in table that declares one (least-privilege)
- `persist=False` in server.py (mutations are in-memory only, not written to disk)

**`update_entity`:**
- Patches by id; if `id` field is changed, atomically rekeys the citation index
- Rejects id changes that would collide with an existing index entry
- Returns `{updated: False, reason: "not_found"}` for unknown ids

### 1.9 Scenario Loading

**Evidence — engine.py lines 113-175:**

FIXTURE_FILES map: `people.json → people`, `emails.json → emails`, `meetings.json → meetings`, `teams.json → teams_messages`, `files.json → files`, `personas.json → personas`, `golden.json → golden`.

Tables auto-discovered: any `*.json` under `tables/`. Supports both bare list format and `{stem: [...]}` dict format. Warns to stderr on format mismatch (falls back to first list-valued key).

### 1.10 Agent Orchestration (workiq_agent.py)

- Builds `ChatAgent` via Microsoft Agent Framework, powered by Azure AI Foundry model
- Wires both transports: MCP (stdio child process) + A2A (HTTP remote sub-agent)
- Routing rules (in INSTRUCTIONS): A2A for natural-language analysis; MCP for data operations; chain both for compound tasks
- REPL + `--ask` one-shot modes

### 1.11 Web UI (web.py)

- FastAPI + uvicorn, serves chat interface at `http://127.0.0.1:8000`
- Agent built once at startup via `lifespan`, reused across requests (low latency)
- `POST /ask` → `{answer: str}`
- HTML/JS inline; uses `marked.min.js` from CDN for markdown rendering
- `GET /` → HTML page

### 1.12 Multi-A2A Dialect Support

**Evidence — a2a_server.py lines 190-240:**

```python
def _is_proto_dialect(message: dict[str, Any]) -> bool:
    if str(message.get("role", "")).upper().startswith("ROLE_"):
        return True
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str)
                and "kind" not in part and "type" not in part:
            return True
    return False
```

Two dialects:
- **JSON spec** (curl / `a2a_e2e.py`): `{"kind": "message", "role": "agent"}`, TextParts with `"kind": "text"`
- **Protobuf a2a-sdk 1.x** (Microsoft Agent Framework `A2AAgent`): `{"role": "ROLE_AGENT"}`, bare `{"text": "..."}` parts, result wrapped in `{"message": <Message>}`

Server detects dialect from incoming message and replies in kind.

---

## 2. NON-FUNCTIONAL REQUIREMENTS

### 2.1 Python Version

**Evidence — README.md:** "Need: Python 3.10+ on your PATH."
`from __future__ import annotations` in all modules (union type hints via `X | Y` at runtime require 3.10+).

### 2.2 Drop-In MCP Contract

**Evidence — server.py docstring:**
> "Drop-in replacement for the real `workiq` MCP server: participants only swap the `command`/`args` in their MCP config — the tool names and shapes are identical."

Tool names (`ask_work_iq`, `fetch`, `create_entity`, `update_entity`) and return shapes match the real Work IQ MCP server exactly. Going from simulator to real Work IQ is a config change only.

### 2.3 Stdio JSON-RPC Purity

**Evidence — server.py line 140:**
> "Startup diagnostics go to stderr so they don't corrupt the stdio JSON-RPC stream."

ALL diagnostic/log output from server.py goes to `sys.stderr`. stdout is reserved exclusively for MCP JSON-RPC frames. The a2a_server similarly routes access logs to stderr.

### 2.4 No PHI

**Evidence — golden.json comment (c1-northbridge):**
> "Administrative/non-clinical only — NO PHI."

All synthetic fixture data is governance/operations/engineering — no patient health information in any scenario.

### 2.5 Deterministic Golden Answers (No Model Required)

**Evidence — engine.py docstring:**
> "Golden answers guarantee deterministic, citable responses for the 8 scripted C1 questions even when no model is configured."

The 8 per-scenario golden Q&As work without OPENAI_API_KEY. LLM fallback is opt-in via env var.

### 2.6 Scalability: Zero Engine Changes for New Scenarios

**Evidence — simulator/README.md:**
> "Tables are auto-discovered. Any `*.json` under a scenario's `tables/` folder becomes a Tools-backed table... adding Challenges 3–6 is data-only — no engine changes."

Adding a new challenge scenario requires only new JSON fixture files — no code modifications to engine.py.

### 2.7 Maintainability: Scenario-Agnostic Validation

`tests/validate_scenario.py` is a generic acceptance gate; works against any scenario directory with no per-scenario code. 6-check suite: golden self-match, unique top match, citation resolution, trimmed_answer presence, RBAC actually trims, Tools round-trip.

### 2.8 Threading Model (A2A Server)

**Evidence — a2a_server.py:** Uses `ThreadingHTTPServer` — one thread per concurrent HTTP request. Appropriate for hackathon scale; not suitable for high-concurrency production.

### 2.9 Persistence

**Evidence — server.py:** `persist=False` for both `create_entity` and `update_entity` in server.py.

Mutations are in-memory only during a server session. Disk persistence is implemented (`_persist_table`) but deliberately disabled at the server layer to keep sessions clean.

### 2.10 Compatibility: A2A Batch Requests

**Evidence — a2a_server.py lines 368-395:**
JSON-RPC batch (array) requests are supported. Empty array → Invalid Request error. Batch of only notifications → HTTP 204 with no body.

---

## 3. EDGE CASES

### 3.1 Borderline Golden Match Threshold

**Evidence — engine.py lines 330-360:**

```python
def match_golden(sc, question, threshold: float = 0.5) -> dict | None:
    ...
    if frac >= threshold and (hits, frac) > best_key:
```

- At exactly 0.5 (50%): entry matches (>= not >). A question matching exactly half the keyword phrases gets the golden answer.
- Two golden entries with identical `(hits, frac)` → FIRST entry in declaration order wins. This means golden.json keyword phrase order is load-order-sensitive for tied questions.
- `validate_scenario.py` checks for rivals: `engine._match_stats(g["question"], o) >= my_key` (uses `>=`), so any tie is flagged as a uniqueness failure.

**Gap:** No test covers a question that scores exactly 0.5 against two different golden entries simultaneously. The tie-break is deterministic (first declaration) but not explicitly tested at that boundary.

### 3.2 Unknown Citation ID in Golden

**Evidence — engine.py lines 260-275:**

```python
entry = sc.index.get(cid)
if entry is None:
    continue  # silently skipped
```

If a golden entry references a citation id that doesn't exist in the index (e.g., typo in fixtures), it is silently dropped from visible_citations. No error is raised. The answer text may reference that source but no citation object appears in the output.

**validate_scenario.py checks this:** `check(f"{qid}: citation {cid} resolves", cid in sc.index)` — so it's caught at validation time, not runtime.

### 3.3 Table Row Missing `id`

**Evidence — engine.py lines 195-205:**

```python
rid = row.get("id")
if not rid:
    sys.stderr.write(
        f"[workiq-sim] WARNING: row in table '{table}' is missing an 'id'; "
        f"skipping it (not citable): {row}\n"
    )
    continue
```

A table row without an `id` is indexed as skipped (written to stderr) and therefore:
- Cannot be cited (not in the index)
- Can still be returned by `fetch()` (fetches the raw table list, not the index)
- Cannot be updated by `update_entity()` (searches by `row.get("id") == id`)

**Gap:** `fetch()` returns rows including ones with no id. An agent calling `update_entity` on a row retrieved via `fetch` that has no id would get `{updated: False, reason: "not_found"}`.

### 3.4 Unknown Persona

**Evidence — server.py lines 128-140:**

```python
if PERSONA and PERSONA not in SCENARIO.persona_ids():
    print(
        f"[workiq-simulator][WARN] persona '{PERSONA}' is not defined in this scenario; "
        f"it will see only public (acl=all) content. Valid personas: {SCENARIO.persona_ids()}",
        file=sys.stderr,
    )
```

An unknown persona id (not defined in personas.json) is warned at startup but NOT rejected. The engine's `can_see()` checks `persona_id in acl` — an unknown persona id will never match any restricted acl entry, so it effectively sees only `acl=["all"]` fixtures. This is equivalent to a least-privilege anonymous session.

**Same behavior in A2A server** (a2a_server.py lines ~500). Demo repl validates persona against `sc.persona_ids()` and rejects unknown personas with an error message (demo.py lines 97-103).

### 3.5 Mixed-Sensitivity Content for a Persona

**Evidence — engine.py `ask()` (lines 447-508):**

When a golden entry has both visible and restricted citations for a persona:
1. Restricted citations are trimmed
2. If `set(trimmed) <= set(restricted_citations)` AND `trimmed_answer` is present → serve authored redaction
3. If the persona is blocked from MORE citations than `restricted_citations` anticipated → **fail closed** with generic message

**The fail-closed path is the key edge case:** A persona that can't see ANY citation in a golden entry (e.g., a new persona not anticipated when authoring) will hit the fail-closed branch regardless of whether a trimmed_answer is authored. The condition `set(trimmed) <= restricted_set` fails.

**validate_scenario.py explicitly tests this** (lines 140-165).

### 3.6 Over-Trimmed Persona (New Persona Added to Fixture Without Updating Golden)

**Evidence — validate_scenario.py lines 140-165:**

```python
else:
    # over-trimmed: must be the generic fail-closed message
    fail_closed = body.startswith("A complete answer to this question")
    check(f"{qid}/{pid}: over-trim fails closed", ...)
```

If a new persona is added to personas.json with restricted ACLs on fixtures that were not anticipated in `restricted_citations`, the engine fails closed. The authored `trimmed_answer` is NOT served in this case, which is the correct security behavior.

### 3.7 A2A Dialect Mismatch

**Evidence — a2a_server.py `_is_proto_dialect()` (lines 190-215):**

The server detects dialect from the incoming message. If a caller sends an ambiguous message (has `"text"` but also has `"kind"` — edge case), it will NOT be detected as proto dialect, and the JSON spec reply format is returned.

**Risk:** Agent Framework's A2AAgent might send a message with partial field overlap. The detection logic uses `"kind" not in part` as the key discriminator. A message with both `"kind"` and protobuf-style `ROLE_USER` role would be classified as proto dialect (role check takes precedence).

### 3.8 `create_entity` Idempotency Edge Cases

**Evidence — engine.py lines 595-640:**

Three idempotency paths:
1. Same `id` as existing row in same table → `{created: False, reason: "id_exists"}`
2. Same `id` as entity in a DIFFERENT table/fixture type (email, person, etc.) → `{created: False, reason: "id_collision"}` (not a duplicate create — a security/correctness rejection)
3. Milestone_tracker-specific: same `(milestone, owner)` → `{created: False, reason: "duplicate_milestone_owner"}`

**Gap:** id_collision with a different-table entity returns `created: False` with `reason: "id_collision"` — but the caller's intent was "create this row". The error message `"id '{new_id}' is already used by another entity"` is informative, but an agent that auto-generates ids could silently fail to create a row if it happens to generate an id already used by an email or person fixture.

### 3.9 LLM Fallback Failure / `OPENAI_API_KEY` Absent

**Evidence — engine.py lines 356-430:**

```python
def _llm_answer(question, context_snippets) -> str | None:
    if not _llm_available():
        return None
    try:
        ...
    except Exception:
        return None  # any exception → None, caller degrades
```

- `OPENAI_API_KEY` absent → `_llm_available()` returns False → returns None immediately
- Any exception from OpenAI call (network error, model error, auth failure) → returns None
- Caller falls through to retrieval-only response (bullet list of top snippets)
- **No error is surfaced to the user** — silent degradation. The source field in the response is `"retrieval-only"` which provides diagnostic signal.

### 3.10 Table File Format Mismatch (Wrong Inner Key)

**Evidence — engine.py lines 145-175:**

```python
if rows is None:
    list_keys = [k for k, v in data.items()
                 if k != "_comment" and isinstance(v, list)]
    if list_keys:
        sys.stderr.write(f"[workiq-sim] WARNING: table '{tf.name}' has no '{stem}' key; "
                         f"using '{list_keys[0]}' instead.\n")
        rows = data[list_keys[0]]
    else:
        sys.stderr.write(f"[workiq-sim] WARNING: table '{tf.name}' has no list rows; "
                         f"registering it empty.\n")
        rows = []
```

If a table JSON file uses a different inner key than the file stem (e.g., file `milestone_tracker.json` has `{"milestones": [...]}` instead of `{"milestone_tracker": [...]}`), the engine falls back to the first list-valued key. Warning goes to stderr. This is a silent data recovery path that a scenario author might not notice.

### 3.11 `persist=False` in Server — Data Loss on Restart

Server.py calls `create_entity(persist=False)` and `update_entity(persist=False)`. All mutations (creates and updates) are lost when the MCP server process exits. An agent that creates risk items and then restarts the server will find the table in its original state.

**This is intentional design** — but an agent builder who assumes persistence will be surprised.

### 3.12 Empty Persona String in A2A Request

**Evidence — a2a_server.py `_persona_from_params()` (lines 143-175):**

```python
s = str(cand).strip()
if not s:
    continue  # blank/whitespace falls through
```

A blank `persona` field in the message metadata is treated as "not provided" and falls through to the next source. This prevents accidentally setting an empty string as a persona (which would match nothing and behave like unknown persona).

---

## 4. SECURITY CONSIDERATIONS

### 4.1 ACL/RBAC Design

**Evidence — engine.py lines 215-240 and personas.json:**

- Per-fixture ACL list: `["all"]` = public, `["persona_id_1", "persona_id_2"]` = restricted
- Personas are role-based (ops_director, quality_pm, credentialing_lead, vendor_liaison, contractor, etc.)
- Missing `acl` field defaults to `["all"]` (world-readable) via `_acl_of()` returning `["all"]`
- Action items inherit parent meeting's ACL unless they declare their own

**Sensitivity levels:** fixtures have a `sensitivity` field ("internal" / "restricted"). Default is "internal" when not declared. This field is included in citation output but is NOT used for ACL enforcement — it is informational only. Enforcement is solely via `acl` list.

### 4.2 Fail-Closed RBAC for Prose Leakage

**Evidence — engine.py lines 456-480:**

```python
if authored and set(trimmed) <= restricted:
    response = authored
else:
    response = (
        "A complete answer to this question draws on sources the active "
        "persona is not authorized to see, and no persona-safe redaction is "
        "available for this set of restrictions. Switch to a persona with "
        "broader access to view it."
    )
```

**Key security property:** The engine does NOT serve the full answer text if ANY citation is trimmed. It cannot serve a canned answer that prose-narrates restricted facts just because citations were stripped. The fail-closed path serves a generic refusal instead.

**Smoke test validates this** (smoke.py lines 95-115):
```python
leak_terms = ["03-JUL", "Karen Vance", "flight-test"]
check("Q2: contractor text redacted", not any(t in r2_contractor["response"] for t in leak_terms))
```

### 4.3 Auth Contract for Real Work IQ

**Evidence — a2a_server.py Agent Card, lines 107-125:**

```python
"securitySchemes": {
    "workiq_oauth": {
        "type": "oauth2",
        "description": (
            "Real Work IQ requires scope WorkIQAgent.Ask "
            "(audience api://workiq.svc.cloud.microsoft). The simulator does NOT "
            "enforce auth; supply persona scoping via message metadata instead."
        ),
    }
}
```

Real service: OAuth2, scope `WorkIQAgent.Ask`, audience `api://workiq.svc.cloud.microsoft`.
Simulator: auth NOT enforced. Persona scoping substitutes for identity.

### 4.4 Simulator Deliberately Does NOT Enforce Auth

This is documented in three places:
- a2a_server.py docstring: "The SIMULATOR does NOT enforce auth — it is a local mock; persona scoping stands in for identity."
- Agent Card security description (above)
- a2a_server.py `_persona_from_params()`: "in the real service that path is gated by the WorkIQAgent.Ask scope"

**Risk for participants:** code that works against the simulator with persona headers will need proper OAuth token handling against the real service.

### 4.5 PHI Prohibition

**Evidence — golden.json (c1-northbridge) `_comment` field:**
> "Administrative/non-clinical only — NO PHI."

All 6 scenarios are explicitly designed to contain no Patient Health Information. Scenarios cover: healthcare governance (c1), aerospace manufacturing (c2), professional services (c3), facilities maintenance (c4), accreditation (c5), engineering (c6).

### 4.6 Token Handling in workiq_agent.py

**Evidence — workiq_agent.py lines 100-130:**

```python
def build_chat_client():
    credential = AzureCliCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://ai.azure.com/.default"
    )
    openai_client = AsyncOpenAI(
        base_url=FOUNDRY_ENDPOINT,
        api_key=token_provider,  # type: ignore[arg-type]
    )
```

- Uses `AzureCliCredential` explicitly (not `DefaultAzureCredential`) for determinism — avoids wrong-tenant token from VS Code or workload identity sources
- Token passed as a callable (`api_key=token_provider`) so it refreshes per request automatically
- No hardcoded keys; no secrets in env vars for Foundry auth
- The `api_version` variable is set to `""` (empty string) — this may cause issues with some Foundry endpoint versions that require a non-empty api-version

**Comment in agent.py explains the credential choice:** "DefaultAzureCredential walks a chain and frequently returns a token from the WRONG tenant when any of the earlier sources are signed into a different tenant."

### 4.7 Least-Privilege ACL Inheritance for New Rows

**Evidence — engine.py `_default_table_acl()` (lines 555-565):**

```python
def _default_table_acl(rows: list[dict], table: str) -> list[str] | None:
    """ACL to apply to a new row that omits one: inherit from the first existing row
    that declares an `acl` (least-privilege — a new row on a restricted table must not
    default to world-readable)."""
```

A new row that omits `acl` inherits the table's dominant ACL rather than defaulting to `["all"]`. This prevents accidentally world-readable rows on restricted tables.

### 4.8 Hardcoded Foundry Endpoint

**Evidence — workiq_agent.py line 76:**

```python
FOUNDRY_ENDPOINT = "https://aparnaram-foundry-subdomain.services.ai.azure.com/openai/v1"
```

The Foundry endpoint is hardcoded to a specific participant's subdomain. This should be overridden via `AZURE_AI_FOUNDRY_ENDPOINT` env var — but the env var override is NOT implemented in the file (the variable reads `AZURE_AI_FOUNDRY_DEPLOYMENT` but the base URL is hardcoded).

**Security gap:** Other participants using this agent code will POST to a specific individual's Foundry endpoint by default. Tokens generated by their `AzureCliCredential` would not be valid against another tenant's endpoint (Entra auth will reject), but the endpoint itself could log requests.

---

## 5. DEPENDENCIES

### 5.1 Python Packages (simulator)

**Evidence — simulator/requirements.txt:**

```
mcp>=1.27           # MCP Python SDK (FastMCP stdio server + client)
openai>=1.0         # OPTIONAL: ad-hoc LLM fallback for non-golden questions
```

- `mcp`: required for server.py (FastMCP) and mcp_e2e.py (ClientSession, stdio_client)
- `openai`: optional — only needed for LLM fallback (`_llm_answer`)
- `a2a_server.py`: **stdlib-only** (http.server, json, threading, uuid) — no pip package needed
- `tests/a2a_e2e.py`: **stdlib-only** (http.client, threading) — no pip package needed

### 5.2 Python Packages (agent)

**Evidence — README.md and agent/workiq_agent.py imports:**

```python
from openai import AsyncOpenAI
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from agent_framework import Agent, MCPStdioTool
from agent_framework.openai import OpenAIChatClient
from agent_framework_a2a import A2AAgent
```

Install command (README.md):
```
pip install agent-framework agent-framework-foundry agent-framework-a2a azure-identity openai
```

**agent/web.py additional deps:**
```python
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
```
Install: `pip install fastapi uvicorn`

**agent/test.py additional dep:**
```python
import jwt
```
(PyJWT package — not in any requirements.txt; implicit dependency)

### 5.3 Azure Services

- **Azure AI Foundry:** model inference endpoint (e.g., `gpt-4o-mini` deployment). Requires an Azure subscription with AI Foundry project.
- **Entra ID (Azure AD):** `AzureCliCredential` / `DefaultAzureCredential` for Foundry auth. `az login` required.
- **Real Work IQ (Path B only):** M365 tenant + Copilot license + admin consent for `WorkIQAgent.Ask` scope.

### 5.4 Protocol Dependencies

| Protocol | Used by | Implementation |
|---|---|---|
| MCP stdio (JSON-RPC over pipes) | server.py ↔ agent/workiq_agent.py | FastMCP (mcp>=1.27) |
| A2A JSON-RPC 2.0 over HTTP | a2a_server.py ↔ agent | stdlib http.server; agent uses agent_framework_a2a |
| HTTP REST | web.py (FastAPI) | uvicorn + fastapi |
| OpenAI Chat API | engine.py LLM fallback | openai>=1.0 |
| Azure AI Foundry API | workiq_agent.py | AsyncOpenAI + azure-identity |

### 5.5 Environment Variables

| Var | Used in | Purpose | Required? |
|---|---|---|---|
| `WORKIQ_SIM_SCENARIO` | server.py, a2a_server.py | Scenario dir path | No (defaults to c1-northbridge) |
| `WORKIQ_SIM_PERSONA` | server.py, a2a_server.py, workiq_agent.py | Active persona | No (defaults to quality_pm) |
| `OPENAI_API_KEY` | engine.py | LLM fallback | No |
| `OPENAI_BASE_URL` | engine.py | Custom OpenAI endpoint | No |
| `MODEL` | engine.py | Model name for LLM | No (defaults to gpt-4o-mini) |
| `AZURE_AI_FOUNDRY_ENDPOINT` | workiq_agent.py | Foundry base URL | **YES** (hardcoded fallback exists) |
| `AZURE_AI_FOUNDRY_DEPLOYMENT` | workiq_agent.py | Foundry model name | No (defaults to gpt-4o-mini) |
| `WORKIQ_A2A_CARD` | workiq_agent.py, web.py | A2A agent card URL | No (defaults to localhost:8920) |
| `WORKIQ_A2A_HOST` | a2a_server.py | A2A bind host | No (defaults to 127.0.0.1) |
| `WORKIQ_A2A_PORT` | a2a_server.py | A2A bind port | No (defaults to 8920; 0=ephemeral) |
| `AZURE_AI_FOUNDRY_API_VERSION` | web.py docstring mentions it | Foundry API version | No |

---

## KEY DISCOVERIES SUMMARY

### Architecture

```
workiq-hackathon/
  simulator/
    engine.py       # Core: scenario load, ACL, golden-match, citation-resolve, LLM fallback, CRUD
    server.py       # MCP stdio: exposes ask_work_iq + fetch + create_entity + update_entity
    a2a_server.py   # A2A HTTP JSON-RPC: exposes ask only (Chat domain)
  agent/
    workiq_agent.py # Orchestrator: Azure AI Foundry LLM + MCP child + A2A remote sub-agent
    web.py          # FastAPI web UI wrapping workiq_agent
```

### Critical Design Facts

1. **Dual transport:** Same engine powers both MCP stdio and A2A HTTP. MCP = Tools + Chat; A2A = Chat only.
2. **Fail-closed RBAC:** Restricted facts are never leaked via prose even when citations are stripped. Validated by smoke test with literal string checks.
3. **No persistence by default:** All mutations are in-memory per server session.
4. **No auth in simulator:** Persona scoping replaces identity; real Work IQ requires `WorkIQAgent.Ask` OAuth scope.
5. **Threshold 0.5:** Golden matching uses 50% keyword-phrase coverage. Exact ties broken by declaration order.
6. **LLM fallback is silent:** Any error → returns None → retrieval-only degradation. No user-visible error.
7. **A2A handles both JSON spec and protobuf dialects** of the a2a-protocol, detected from incoming message shape.
8. **Hardcoded Foundry endpoint** in workiq_agent.py (participant-specific subdomain) — needs env var override.

---

## IDENTIFIED GAPS / AMBIGUITIES

| # | Gap | File | Confidence |
|---|---|---|---|
| G1 | `api_version=""` in workiq_agent.py — may fail with Foundry endpoints requiring non-empty version | workiq_agent.py line 80 | High |
| G2 | Hardcoded `FOUNDRY_ENDPOINT` — env var `AZURE_AI_FOUNDRY_ENDPOINT` not read (the var is declared but URL is hard-coded string) | workiq_agent.py line 76 | High |
| G3 | `jwt` package used in agent/test.py not in any requirements file | agent/test.py line 1 | High |
| G4 | `fileUrls` param accepted by `ask_work_iq` but silently ignored — no validation or warning | server.py | Medium |
| G5 | Unknown citation ids in golden silently dropped — could mask authoring errors at runtime (only caught by validate_scenario.py) | engine.py line 268 | Medium |
| G6 | New rows created via `create_entity` with no `acl` inherit table's first-row ACL — but the first row's ACL may not match intended access level for the new row | engine.py lines 635-642 | Medium |
| G7 | Golden match tie-break (first declaration order) is not documented — scenario authors may create ambiguous golden sets unintentionally | engine.py line 344 | Medium |
| G8 | LLM fallback uses top 6 snippets (k=6 hardcoded in `_retrieve()`) and passes to `client.chat.completions.create` with `messages=[{"role":"user"...}]` only — no system message, no temperature guard on very large prompts | engine.py lines 435-445 | Low |
| G9 | A2A server does not validate Content-Type header on POST — accepts any body as JSON | a2a_server.py | Low |
| G10 | `update_entity` allows removing `acl` from a row by passing `{"acl": null}` in the patch — would make a previously restricted row world-readable | engine.py `update_entity` | Medium |

---

## CONFIDENCE LEVELS BY CATEGORY

- **Functional Requirements:** HIGH — code read directly, cross-validated with tests
- **Non-Functional Requirements:** HIGH — explicit in docstrings, READMEs, and test assertions
- **Edge Cases:** HIGH for documented ones; MEDIUM for gaps not covered by tests
- **Security Considerations:** HIGH — fail-closed RBAC proven by smoke test; auth contract from docstrings
- **Dependencies:** HIGH — requirements.txt + import statements read directly

---

## RECOMMENDED FOLLOW-UP RESEARCH

- [ ] Read scenarios c3-c6 golden.json and personas.json to verify RBAC patterns are consistent across all 6 scenarios
- [ ] Check `agent_framework` and `agent_framework_a2a` package APIs to confirm `MCPStdioTool`, `A2AAgent.as_tool()`, and `Agent` constructor signatures match the code in workiq_agent.py
- [ ] Verify whether `AZURE_AI_FOUNDRY_ENDPOINT` env var is actually read anywhere in workiq_agent.py (current reading suggests the hardcoded URL is always used)
- [ ] Investigate whether `api_version=""` causes 404 or falls back gracefully on the Foundry `/openai/v1` endpoint
- [ ] Read `challenge-pack/WorkIQ-Hackathon-Challenge-Pack_14-JUN-2026.pdf` for challenge-specific scoring criteria that may impose additional functional requirements
- [ ] Confirm `agent_framework` pip package name maps to the Microsoft Agent Framework (could be a different package than what's on PyPI)
- [ ] Test whether `A2AAgent` in agent_framework_a2a correctly handles the JSON spec dialect (non-proto) returned by the simulator when the Framework sends proto-dialect messages

---

## CLARIFYING QUESTIONS (Require User/Stakeholder Input)

1. Is `AZURE_AI_FOUNDRY_ENDPOINT` intended to be read as an env var override in workiq_agent.py, or is the hardcoded subdomain intentional for this participant's setup?
2. Should `create_entity` mutations be optionally persisted (i.e., should `persist` be configurable via env var)?
3. Is the `api_version=""` intentional — does the participant's Foundry endpoint accept a blank api-version?
4. Are there additional scenarios (c7+) planned beyond the 6 in the repo?
