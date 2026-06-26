<!-- markdownlint-disable-file -->
# Task Research: WorkIQ Hackathon — Comprehensive Test Plan

Full test coverage artifact for the WorkIQ Hackathon simulator and agent stack.
Covers user stories, acceptance criteria, and 7 test categories with priority, traceability, and Excel-compatible output.

## Task Implementation Requests

* User Stories with Acceptance Criteria
* Positive, Negative, Boundary, Security, Integration, and Performance test cases
* Excel-style table with priority and requirement traceability

## Scope and Success Criteria

* Scope: All testable components — `engine.py`, `server.py` (MCP), `a2a_server.py` (A2A), `agent/workiq_agent.py`, scenario data fixtures, RBAC, citation resolution, Tools surface
* Assumptions: Python 3.10+, `.venv` configured, scenarios c1–c6 present, MCP `mcp` package installed
* Success Criteria:
  * Every user story maps to >=1 acceptance criterion
  * Each test case references a requirement (US or AC id)
  * Priority assigned (P0=blocker, P1=high, P2=medium, P3=low)
  * Negative and security tests explicitly validate fail-closed behavior

## Key Discoveries

### System Components

| Component | File | Role |
|---|---|---|
| Engine | simulator/engine.py | Fixture loading, golden matching, RBAC, citation resolution, Tools ops |
| MCP Server | simulator/server.py | Exposes ask_work_iq + fetch/create_entity/update_entity over stdio |
| A2A Server | simulator/a2a_server.py | JSON-RPC 2.0 chat gateway; method SendMessage / message/send |
| Agent | agent/workiq_agent.py | Orchestrates MCP + A2A via Azure AI Foundry |
| Tests | simulator/tests/ | smoke.py (engine), mcp_e2e.py, a2a_e2e.py, validate_scenario.py |
| Scenarios | simulator/scenarios/c1–c6 | Six challenge datasets with people/emails/meetings/teams/files/tables/golden |

### RBAC Design

* Every fixture carries an `acl` (list of persona ids or `["all"]`)
* `can_see(record, persona_id)` — returns True if `"all"` in acl OR persona_id in acl OR persona_id is None
* `None` persona = admin/dev session, full visibility
* Restricted citations trimmed → governance note injected into response prose
* CRITICAL: restricted facts must NOT appear in response text even if citations are trimmed

### Tools Surface

| Tool | Contract | Idempotency |
|---|---|---|
| ask_work_iq | question: str, fileUrls?: list[str] | Stateless (golden or LLM) |
| fetch | table: str, filter?: dict | Read-only |
| create_entity | table: str, record: dict | Same milestone+owner → returns existing row |
| update_entity | table: str, id: str, patch: dict | Patch-only, no full replace |

### A2A Protocol

* POST JSON-RPC 2.0 to `/a2a/`
* Methods: `SendMessage` (A2A v1.0) and `message/send` (open standard)
* Agent Card: `/.well-known/agent-card.json` and `/.well-known/agent.json`
* Multi-turn: `contextId` from prior response passed into next message
* Persona override: `X-WorkIQ-Persona` header, message metadata `persona`, or `WORKIQ_SIM_PERSONA` env var

### Personas

| Scenario | Persona | Access Level |
|---|---|---|
| c1-northbridge | ops_director | Full (incl. HR, commercial) |
| c1-northbridge | quality_pm | Quality + committees + vendor commercial; NOT HR file |
| c1-northbridge | credentialing_lead | Credentialing + HR file; NOT commercial thread |
| c1-northbridge | vendor_liaison | Least privilege — external vendor only |
| c2-contoso | new_pm | Full (incl. customer escalations, commercial) |
| c2-contoso | quality_engineer | Internal engineering/quality; NOT customer-restricted |
| c2-contoso | contractor | Least privilege — no restricted/commercial |
| c2-contoso | director | Same as new_pm |

### Golden Matching

* `match_golden()` uses stemmed token subset match
* Threshold: 0.5 fraction of keyword phrases matched
* Ranking: (absolute hits, fraction) — longer specific entries beat short generic ones
* LLM fallback activates only for off-script (non-golden) questions

## Research Executed

### File Analysis

* simulator/engine.py — Full fixture loading, RBAC, citation, golden match, Tools (fetch/create_entity/update_entity), LLM fallback
* simulator/server.py — MCP FastMCP tool registration; env-var-driven scenario/persona selection
* simulator/a2a_server.py — JSON-RPC 2.0 server; persona precedence: metadata > header > env > default
* simulator/tests/smoke.py — 8 C2 golden Qs, persona trim, Tools surface validation
* simulator/tests/validate_scenario.py — scenario-agnostic data integrity gate
* simulator/scenarios/c1-northbridge/golden.json — 8 C1 questions with keywords, citations, restricted_citations, tool field
* simulator/scenarios/c1-northbridge/personas.json — 4 C1 personas with ACL descriptions
* simulator/scenarios/c2-contoso/personas.json — 4 C2 personas

### Project Conventions

* Test files live in simulator/tests/ and exit 0 on all-pass, 1 on failure
* validate_scenario.py is the Layer 1 data-integrity gate
* No PHI in any scenario — healthcare scenarios use administrative data only
* LLM-fallback is optional; golden answers are model-free
