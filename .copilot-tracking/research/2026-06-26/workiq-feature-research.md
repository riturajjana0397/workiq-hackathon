<!-- markdownlint-disable-file -->
# Task Research: WorkIQ Hackathon — Feature Analysis

A local Work IQ simulator and orchestrator agent for a Microsoft hackathon.
The system replicates the real Work IQ MCP/A2A contract using synthetic scenario fixtures,
so participants can build and test agents without a live Microsoft 365 tenant.

## Task Implementation Requests

* Identify functional requirements of the WorkIQ system
* Identify non-functional requirements
* Identify edge cases
* Identify security considerations
* Identify all dependencies with evidence

## Scope and Success Criteria

* Scope: Covers simulator (engine.py, server.py, a2a_server.py, demo.py, tests/),
  agent (workiq_agent.py, web.py), scenario fixtures, and starter-kit config
* Assumptions: Research targets the local simulator as the primary implementation surface;
  real Work IQ (Path B) is referenced for contract parity only
* Success Criteria:
  * All 5 categories fully documented with file + line references
  * Security gaps identified with evidence
  * Edge case behaviors traced to exact code paths

## Outline

1. Functional Requirements — MCP tools, A2A Chat, golden-answer Q&A, RBAC, Tools surface CRUD
2. Non-Functional Requirements — contract parity, no-PHI, Python version, stdio purity
3. Edge Cases — threshold boundary, persona gaps, missing IDs, LLM failure modes
4. Security Considerations — ACL model, fail-closed RBAC, auth contract, known gaps
5. Dependencies — by layer (simulator, agent, web UI, auth debug)

---

## Research Executed

### File Analysis

* simulator/engine.py
  * Lines 1–30: Module docstring — purpose (golden-answer Q&A, RBAC, citation, Tools surface CRUD)
  * Lines 46–62: `FIXTURE_FILES` dict maps filenames to top-level list keys; `TABLES_DIR = "tables"` drives auto-discovery
  * Lines 64–85: `Scenario` dataclass with `people`, `emails`, `meetings`, `teams_messages`, `files`, `personas`, `golden`, `tables`, `table_formats`, `index`
  * Lines 115–175: `load_scenario` — loads fixtures + tables, handles both `{stem: list}` and bare-list JSON formats, warns on missing `stem` key
  * Lines 180–205: `_build_index` — indexes every citable entity and action items (action items inherit parent meeting ACL)
  * Lines 207–225: `can_see(record, persona_id)` — ACL check: `"all"` in acl → visible; `persona_id is None` → full visibility (admin/dev mode)
  * Lines 227–265: `resolve_citations` — trims unauthorized citations, returns visible list + trimmed id list
  * Lines 270–365: Golden-answer matching: `_normalize`, `_stem` (light suffix stripper), `_tokens`, `_match_stats`, `match_golden` with threshold=0.5 and (hits, fraction) ranking
  * Lines 367–415: LLM fallback via `OPENAI_API_KEY` — silent failure, no user-visible error
  * Lines 417–460: `_all_snippets` — flattens all visible fixtures for retrieval; restricted content excluded at this layer too
  * Lines 462–480: `_retrieve` — term overlap scoring, top-k=6
  * Lines 487–555: `ask()` — primary orchestration function: try golden → resolve_citations → if trimmed: fail-closed RBAC logic → else full answer; fallback: retrieve → optional LLM → retrieval-only bullets
  * Lines 560–640: `fetch`, `create_entity` (idempotency), `update_entity`

* simulator/server.py
  * Lines 55–90: FastMCP tool registration for `ask_work_iq`, `fetch`, `create_entity`, `update_entity`
  * Lines 40–55: `WORKIQ_SIM_SCENARIO` and `WORKIQ_SIM_PERSONA` env vars with defaults
  * Lines 110–130: Startup diagnostics to stderr — warns on unknown persona, zero golden answers

* simulator/a2a_server.py
  * Lines 1–50: Module docstring — A2A JSON-RPC 2.0, methods `SendMessage` + `message/send`, discovery at `/.well-known/agent-card.json`
  * Lines 95–145: `_agent_card` — describes capabilities, security scheme, OAuth2 scope reference (auth NOT enforced in simulator)
  * Lines 147–185: `_persona_from_params` — precedence: message metadata → params metadata → `X-WorkIQ-Persona` header → server default; blank/whitespace treated as not-provided; `"all"` (case-insensitive) → full visibility
  * Lines 185–220: `_text_from_message` — handles A2A v1.0 `{kind: text}`, `{type: text}`, and bare `{text: ...}` (proto SDK dialect)
  * Lines 220–250: `_is_proto_dialect` — detects `ROLE_USER` enum style from a2a-sdk 1.x to reply in matching dialect

* agent/workiq_agent.py
  * Lines 76–80: `FOUNDRY_ENDPOINT` is HARDCODED (`aparnaram-foundry-subdomain.services.ai.azure.com`) — `AZURE_AI_FOUNDRY_ENDPOINT` env var is DEFINED but never read back
  * Lines 82: `api_version = ""` — empty string, not passed to client; may cause issues with some Foundry endpoints
  * Lines 90–110: `build_chat_client` — uses `AzureCliCredential` explicitly (not `DefaultAzureCredential`) to ensure correct tenant
  * Lines 120–140: `build_mcp_tool` — spawns simulator as subprocess via `MCPStdioTool`; persona injected via env
  * Lines 143–155: `build_a2a_agent` — constructs `A2AAgent` from base URL derived by splitting `/.well-known/` from card URL

* agent/web.py
  * Lines 50–90: FastAPI lifespan context — agent built once at startup, shared across requests
  * Lines 93–105: `POST /ask` endpoint — validates non-empty question, raises HTTP 400 for blank input
  * Lines 107+: Inline HTML/JS chat UI — uses `marked.min.js` from CDN for markdown rendering

* simulator/requirements.txt
  * `mcp>=1.27` (required), `openai>=1.0` (optional LLM fallback)

* simulator/tests/smoke.py
  * Lines 48–60: Validates fixture counts (10 people, 6 emails, 3 meetings, 5 teams, 3 files, 8 golden, 4 tracker rows, 4 personas) for C2-Contoso
  * Lines 62–85: Asserts all 8 golden questions match correctly for `new_pm` persona
  * Lines 88–110: Persona trimming: contractor cannot see `EML-001`/`EML-002`; governance note present; literal anti-leak strings checked

### External Research

* A2A protocol: `message/send` method — open standard at a2a-protocol.org; `SendMessage` is the Microsoft Work IQ v1.0 alias. Both accepted.
* MCP SDK: `mcp>=1.27` uses `FastMCP` (high-level); stdio transport preserves JSON-RPC frames on stdout.

---

## Key Discoveries

### Project Structure

```
simulator/
  engine.py           — core retrieval, golden-answer Q&A, RBAC, Tools CRUD
  server.py           — MCP stdio server (FastMCP), wraps engine
  a2a_server.py       — A2A HTTP server (stdlib ThreadingHTTPServer), wraps engine
  demo.py             — CLI demo runner (no model needed)
  requirements.txt    — mcp>=1.27, openai>=1.0 (optional)
  scenarios/
    c1-northbridge/   — Health network scenario (6 scenarios total: c1-c6)
      golden.json     — 8 scripted Q&A with citations, keywords, RBAC info
      personas.json   — persona definitions (ops_director, quality_pm, etc.)
      emails.json     — synthetic emails with ACL
      tables/
        capa_tracker.json  — Dataverse-style mutable table
  tests/
    smoke.py          — engine unit tests
    mcp_e2e.py        — end-to-end MCP tool tests
    a2a_e2e.py        — end-to-end A2A HTTP tests
    validate_scenario.py  — scenario fixture validation

agent/
  workiq_agent.py     — Azure AI Foundry orchestrator (MCP + A2A)
  web.py              — FastAPI web UI
  test.py             — Azure credential/token debug script
```

### Implementation Patterns

**Golden-answer matching** (engine.py lines 270–365):
```python
def match_golden(sc, question, threshold=0.5):
    # ranked by (abs_hits, fraction), ties by declaration order
    # threshold=0.5 means >=50% of keyword phrases must match
```

**Fail-closed RBAC** (engine.py lines 447–480):
```python
if trimmed:
    restricted = set(golden.get("restricted_citations", []))
    authored = golden.get("trimmed_answer")
    if authored and set(trimmed) <= restricted:
        response = authored   # safe redaction exists
    else:
        response = "A complete answer... not authorized..."  # fail closed
    response += GOVERNANCE_NOTE.format(...)
```

**ACL check** (engine.py lines 207–225):
```python
def can_see(record, persona_id):
    acl = record.get("acl", ["all"])
    if "all" in acl: return True
    if persona_id is None: return True   # admin mode
    return persona_id in acl
```

**A2A dialect detection** (a2a_server.py lines 220–250):
```python
def _is_proto_dialect(message):
    if str(message.get("role","")).upper().startswith("ROLE_"): return True
    # also checks for bare text parts without "kind" discriminator
```

---

## Functional Requirements

| # | Requirement | Evidence |
|---|---|---|
| FR-01 | Expose 4 MCP tools over stdio: `ask_work_iq`, `fetch`, `create_entity`, `update_entity` | server.py lines 55–130 |
| FR-02 | `ask_work_iq(question, fileUrls?)` → JSON `{response, conversationId, citations}` — identical contract to real Work IQ | server.py lines 55–75 |
| FR-03 | `fetch(table, filter?)` → JSON list of rows from scenario tables | server.py lines 80–92 |
| FR-04 | `create_entity(table, record)` — append row; idempotent on `id` and `(milestone, owner)` logical key | server.py lines 94–107; engine.py lines 595–640 |
| FR-05 | `update_entity(table, id, patch)` — patch fields on existing row by id | server.py lines 110–122 |
| FR-06 | Expose A2A Chat capability at `/a2a/` accepting JSON-RPC 2.0 `SendMessage` and `message/send` methods | a2a_server.py lines 55–90 |
| FR-07 | Serve A2A Agent Card at `/.well-known/agent-card.json` (and legacy `/.well-known/agent.json`) | a2a_server.py lines 95–145 |
| FR-08 | Resolve persona via message metadata → params metadata → `X-WorkIQ-Persona` header → server default | a2a_server.py lines 147–185 |
| FR-09 | Golden-answer Q&A: match by keyword-phrase overlap (threshold 0.5), ranked by (abs hits, fraction), declaration-order tiebreak | engine.py lines 270–365 |
| FR-10 | LLM fallback for non-golden questions: opt-in via `OPENAI_API_KEY`, any failure degrades to retrieval-only bullets | engine.py lines 367–430 |
| FR-11 | Persona-based permission trimming on all outputs (citations, LLM context, retrieval snippets) | engine.py lines 207–460 |
| FR-12 | Fail-closed RBAC: never serve prose that contains restricted facts unless a persona-safe redaction is authored | engine.py lines 447–480 |
| FR-13 | Governance note appended when citations are trimmed: identifies count, persona label, and withheld IDs | engine.py lines 483–490 |
| FR-14 | Citation resolution: `{citation_id, source_index, title, kind, sensitivity, url}` per visible source | engine.py lines 227–265 |
| FR-15 | Tables auto-discovered from `scenarios/<name>/tables/*.json`; new scenarios need zero engine changes | engine.py lines 135–175 |
| FR-16 | Multi-turn A2A: `contextId` from prior response accepted in next message | a2a_server.py |
| FR-17 | Orchestrator agent (workiq_agent.py) wires both MCP and A2A to an Azure AI Foundry model | workiq_agent.py lines 60–200 |
| FR-18 | Web UI (web.py) exposes `POST /ask` returning `{answer}` and serves inline HTML chat at `/` | web.py lines 93–150 |

---

## Non-Functional Requirements

| # | Requirement | Evidence |
|---|---|---|
| NFR-01 | Python 3.10+ (union type syntax `X \| Y`, `from __future__ import annotations`) | engine.py line 4; requirements.txt |
| NFR-02 | Drop-in MCP contract: tool names and parameter shapes identical to real Work IQ | server.py lines 1–30 |
| NFR-03 | stdout reserved for MCP JSON-RPC frames only; all diagnostics to stderr | server.py lines 110–130 |
| NFR-04 | No PHI in any scenario fixture | emails.json `_comment` field; golden.json comments |
| NFR-05 | Mutations are in-memory per session (`persist=False`); disk state is never modified at runtime | server.py lines 96, 110; engine.py create_entity/update_entity signatures |
| NFR-06 | New scenarios require zero engine code changes (data-driven design) | engine.py lines 46–62, 135–175 |
| NFR-07 | A2A server uses stdlib `ThreadingHTTPServer` — no extra dependencies beyond engine | a2a_server.py imports |
| NFR-08 | Golden answers guarantee deterministic, citable responses with NO model configured | engine.py lines 1–20 |
| NFR-09 | `AzureCliCredential` preferred over `DefaultAzureCredential` for deterministic tenant selection | workiq_agent.py lines 90–110 |
| NFR-10 | Agent and its MCP child + A2A session built once at startup and reused for low latency (web.py) | web.py lines 50–90 |
| NFR-11 | Idempotent `create_entity`: re-creating the same logical row returns existing row, not duplicate | engine.py lines 595–640 |

---

## Edge Cases

| # | Edge Case | Behavior | Evidence |
|---|---|---|---|
| EC-01 | Golden threshold boundary: question matches exactly 50% of keyword phrases | Matched (`>=` inclusive) | engine.py `_match_stats` lines 330–360 |
| EC-02 | Two golden entries with equal (hits, fraction) score | First in declaration order wins (deterministic) | engine.py `match_golden` |
| EC-03 | Persona trimmed for MORE citations than `restricted_citations` authored | Engine fails closed with generic refusal; `trimmed_answer` NOT served | engine.py lines 455–475 |
| EC-04 | `OPENAI_API_KEY` absent or LLM call throws | Silent degradation to retrieval-only bullet list; no user-visible error; `source="retrieval-only"` in return | engine.py lines 367–380 |
| EC-05 | Table row missing `id` field | Skipped from index (stderr warning), `fetch` still returns it, `update_entity` cannot patch it | engine.py `_build_index` lines 180–205 |
| EC-06 | Unknown persona at startup | Warns to stderr; persona sees only `acl=["all"]` content (least-privilege) | server.py lines 110–120 |
| EC-07 | Table file uses different inner key than its stem (e.g. file is `capa_tracker.json` but inner key is `capas`) | Falls back to first list-valued key with a warning; data not silently dropped | engine.py lines 145–160 |
| EC-08 | Table file has no list-valued keys | Registered empty with warning | engine.py lines 160–168 |
| EC-09 | `create_entity` auto-generates an id that collides with an existing fixture id (cross-table collision) | Rejected with `id_collision` reason; agent may need to retry with different record | engine.py create_entity logic |
| EC-10 | A2A caller uses protobuf SDK dialect (`ROLE_USER`, bare text parts without `kind`) | Detected via `_is_proto_dialect`; response mirrored in same dialect | a2a_server.py lines 220–250 |
| EC-11 | A2A `SendMessage` vs `message/send` method name | Both dispatched to same handler | a2a_server.py `SEND_METHODS` set |
| EC-12 | Empty or blank question to `POST /ask` | HTTP 400 with `detail: "question is required"` | web.py lines 100–103 |
| EC-13 | `persona_id=None` (admin/dev mode) | Full visibility — all content visible regardless of ACL | engine.py `can_see` line 215 |
| EC-14 | `"all"` passed as persona (case-insensitive) | Treated as full-visibility (`persona_id=None`) in A2A; similar in MCP env var | a2a_server.py `_persona_from_params` line 173 |
| EC-15 | `fileUrls` parameter in `ask_work_iq` | Accepted for contract parity; ignored in simulator | server.py lines 55–75 |
| EC-16 | Question retrieves top-k=6 snippets; question has many matching terms spread across many docs | Only top 6 by overlap count used for LLM context; rare long-tail answers may be under-supported | engine.py `_retrieve` lines 462–480 |

---

## Security Considerations

| # | Consideration | Status | Evidence |
|---|---|---|---|
| SC-01 | ACL enforcement: `persona_id in acl` list check; `sensitivity` field is informational only (NOT enforced as a separate gate) | Design | engine.py `can_see` lines 207–225 |
| SC-02 | Fail-closed RBAC: restricted prose never served unless a persona-safe redaction is explicitly authored | Implemented | engine.py lines 447–480 |
| SC-03 | Anti-leak validation in smoke tests: literal strings from restricted content asserted absent from unauthorized personas' answers | Tested | simulator/tests/smoke.py lines 88–110 |
| SC-04 | LLM fallback context excludes restricted content at the snippet-generation layer (`_all_snippets` filters by `can_see`) | Implemented | engine.py `_all_snippets` lines 417–460 |
| SC-05 | Real Work IQ auth: OAuth2 scope `WorkIQAgent.Ask`, audience `api://workiq.svc.cloud.microsoft` — **simulator does NOT enforce auth** | By design (local mock) | a2a_server.py `_agent_card` lines 130–145 |
| SC-06 | `AzureCliCredential` used instead of `DefaultAzureCredential` to prevent wrong-tenant token in multi-tenant dev environments | Implemented | workiq_agent.py lines 90–115 |
| SC-07 | **GAP — Hardcoded Foundry endpoint:** `FOUNDRY_ENDPOINT` is a literal string in workiq_agent.py line 76; `AZURE_AI_FOUNDRY_ENDPOINT` env var is declared in docs but never read back | Bug/Gap | workiq_agent.py line 76 |
| SC-08 | **GAP — `update_entity` ACL widening:** patch can set `acl: null` or `acl: ["all"]` on a previously restricted row, silently widening its access for the rest of the session | Gap | engine.py `update_entity` |
| SC-09 | No PHI in synthetic fixtures: email/meeting bodies contain administrative content only; policy enforced by scenario authoring convention | By design | emails.json `_comment`; golden.json |
| SC-10 | Persona resolution precedence in A2A: message metadata > params metadata > HTTP header > server default; blank/whitespace values skipped (cannot accidentally widen scope) | Implemented | a2a_server.py lines 147–185 |
| SC-11 | Governance note is **appended** to the response text (not a separate field), so it cannot be stripped by agents that only read the `response` key | Design | engine.py `GOVERNANCE_NOTE` line 483 |
| SC-12 | `api_version = ""` in workiq_agent.py — empty string not passed to client; may break Foundry endpoints that require a specific API version | Risk | workiq_agent.py line 82 |

---

## Dependencies

### Simulator (simulator/requirements.txt)

| Package | Version | Required? | Role |
|---|---|---|---|
| `mcp` | `>=1.27` | Required | MCP Python SDK — FastMCP stdio server + client |
| `openai` | `>=1.0` | Optional | LLM fallback for non-golden ad-hoc questions |

### A2A Server

| Package | Required? | Role |
|---|---|---|
| Python stdlib only (`http.server`, `json`, `uuid`, `os`, `sys`, `pathlib`) | Required | No additional deps needed |

### Agent (agent/workiq_agent.py)

| Package | Required? | Role |
|---|---|---|
| `agent-framework` | Required | `Agent`, `MCPStdioTool` |
| `agent-framework-foundry` | Required | `OpenAIChatClient` backed by Azure AI Foundry |
| `agent-framework-a2a` | Required | `A2AAgent` for A2A sub-agent wiring |
| `azure-identity[aio]` | Required | `AzureCliCredential`, `get_bearer_token_provider` |
| `openai` | Required | `AsyncOpenAI` client |

### Web UI (agent/web.py)

| Package | Required? | Role |
|---|---|---|
| `fastapi` | Required | HTTP framework |
| `uvicorn` | Required | ASGI server |
| `pydantic` | Required | `BaseModel` for request validation |

### Auth Debug (agent/test.py)

| Package | Notes |
|---|---|
| `PyJWT` (`jwt`) | Not in any requirements file — implicit dependency; must be installed manually |

### Azure Services

| Service | Role |
|---|---|
| Azure AI Foundry | Hosts the LLM deployment (`gpt-4o-mini` default) |
| Azure CLI / Entra ID | Auth for `AzureCliCredential` (requires `az login` in correct tenant) |

### Protocols

| Protocol | Usage |
|---|---|
| MCP stdio (JSON-RPC 2.0 over stdin/stdout) | Tool surface: `ask_work_iq`, `fetch`, `create_entity`, `update_entity` |
| A2A (JSON-RPC 2.0 over HTTP POST) | Chat surface: `SendMessage` / `message/send` |
| OpenAI Chat Completions API | LLM fallback in simulator; agent orchestration |
| OAuth2 (real Work IQ only) | Scope `WorkIQAgent.Ask`, audience `api://workiq.svc.cloud.microsoft` |

---

## Technical Scenarios

### Selected Approach: Dual-transport, scenario-driven simulator with fail-closed RBAC

The WorkIQ system intentionally exposes both MCP (for tool/CRUD operations) and A2A (for chat/grounding)
as separate transports backed by the same `engine.py` core. This mirrors the real Work IQ contract
and lets participants test agent routing logic locally.

**Key rationale:**
- Same engine answers both MCP and A2A queries — single source of truth for answers and RBAC
- Golden answers ensure deterministic behavior without a live model, enabling CI/CD validation
- Fail-closed RBAC prevents test/demo content from leaking restricted facts even in degraded states
- Data-driven scenario design (new scenario = new `scenarios/cN-*/` directory, zero code changes)

#### Considered Alternatives

- Single transport only (MCP or A2A): Rejected — challenge requires agents to use both and route correctly
- Persist mutations to disk: Rejected — `persist=False` keeps scenarios reproducible and avoids state corruption between test runs

---

## Actionable Next Steps

1. **Fix hardcoded Foundry endpoint** (SC-07): Replace literal string in workiq_agent.py line 76 with `os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT", "<default>")` so env var override actually works
2. **Guard `update_entity` against ACL widening** (SC-08): Reject patches that contain an `acl` key on rows whose current `sensitivity` is `"restricted"`
3. **Add `PyJWT` to agent requirements** (or document as optional): agent/test.py line 1 imports `jwt` which is not in any requirements file
4. **Validate `api_version` behavior** (SC-12): Confirm empty string is acceptable for the participant's Foundry endpoint, or read `AZURE_AI_FOUNDRY_API_VERSION` env var
5. **Run `validate_scenario.py` against all 6 scenarios** (c1–c6) to catch ambiguous golden-keyword overlaps before demo
