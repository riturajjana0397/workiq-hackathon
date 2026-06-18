# Local Work IQ Simulator

> **Metadata** — Created: 14-JUN-2026 · Component: `simulator/README.md` ·
> Role: run guide + drop-in MCP config for the local Work IQ simulator.

A local **MCP stdio server** that imitates the real Microsoft Work IQ server so hackathon
teams can build and demo their agents **without a real M365 / Microsoft Graph tenant**.

It exposes the **exact real `ask_work_iq` contract** plus the **Tools** surface the
challenges need (`fetch` / `create_entity` / `update_entity`), backed by synthetic
fixtures with retrieval, golden-answer + optional LLM synthesis, persona-based
permission trimming, and citations.

**The contract is identical to the real server** — going from simulator to the real
Work IQ MCP server is a *config change* (swap `command`/`args`), not a code rewrite.

---

## What's in here

```
simulator/
  engine.py                 # load fixtures, retrieve, golden-match, persona-trim, citations, Tools
  server.py                 # MCP stdio server: ask_work_iq + fetch/create_entity/update_entity
  a2a_server.py             # A2A (Agent-to-Agent) JSON-RPC server: ask over the Chat domain
  requirements.txt          # mcp (+ optional openai); a2a_server is stdlib-only
  tests/
    smoke.py                # engine-level (C2): 8 golden Qs, persona trim, Tools surface (no server)
    mcp_e2e.py              # end-to-end (C2): launches server.py as a real stdio subprocess
    a2a_e2e.py              # end-to-end: drives a2a_server.py over real HTTP/JSON-RPC (all 6 scenarios)
    validate_scenario.py    # scenario-agnostic acceptance gate — run against ANY scenario dir
  scenarios/
    c1-northbridge/         # Challenge 1 — Northbridge Health Network (update_entity hero, no PHI)
      people.json           # committee membership across two governance bodies
      emails.json           # Lumina vendor go-live thread + (restricted) leadership commercial thread
      meetings.json         # Quality Steering + Credentialing committees + Westgate EHR rollout review
      teams.json            # per-clinic Westgate rollout channel + staff-only Quality Program channel
      files.json            # CAPA tracker summary, med-rec policy draft, (restricted) HR personnel file
      personas.json         # ops_director | quality_pm | credentialing_lead | vendor_liaison
      golden.json           # the 8 C1 compound questions -> cited answers
      tables/
        capa_tracker.json        # corrective-action tracker (the update_entity target)
    c2-contoso/             # Challenge 2 — Contoso Precision Parts / Part 45621-B (create_entity hero)
      people.json           # org chart / people graph
      emails.json           # program + customer-escalation + supplier-risk threads
      meetings.json         # design-review recaps + transcripts + action items
      teams.json            # Inconel 718 material channel posts
      files.json            # PPAP QTP rev D, supplier risk register, (restricted) supplier agreement
      personas.json         # new_pm | quality_engineer | contractor | director
      golden.json           # the 8 C2 compound questions -> cited answers
      tables/
        milestone_tracker.json   # Dataverse-style tracker (the create/update/fetch target)
```

> **Tables are auto-discovered.** Any `*.json` under a scenario's `tables/` folder becomes a Tools-backed
> table keyed by its file stem (`milestone_tracker`, `capa_tracker`, …). The engine derives the citation
> `kind` and id-prefix from the data, so **adding Challenges 3–6 is data-only — no engine changes.**

## Setup

```powershell
# from the repo root
.\.venv\Scripts\python.exe -m pip install -r simulator\requirements.txt
```

`openai` is optional — the 8 scripted (golden) questions answer deterministically with
**no model**. A model only powers ad-hoc, off-script questions (see *LLM fallback*).

## Test

```powershell
# engine-level smoke (fast, no subprocess) — C2
.\.venv\Scripts\python.exe simulator\tests\smoke.py

# full end-to-end over real MCP stdio — C2
.\.venv\Scripts\python.exe simulator\tests\mcp_e2e.py

# full end-to-end over real A2A (JSON-RPC over HTTP) — all 6 scenarios
.\.venv\Scripts\python.exe simulator\tests\a2a_e2e.py

# scenario-agnostic acceptance gate — run for EACH scenario you ship
.\.venv\Scripts\python.exe simulator\tests\validate_scenario.py scenarios\c1-northbridge
.\.venv\Scripts\python.exe simulator\tests\validate_scenario.py scenarios\c2-contoso
```

All should print `ALL ... CHECKS PASSED` and exit 0. `validate_scenario.py` is the **Layer 1**
data-integrity gate (golden self-match, no dangling citations, RBAC actually trims, Tools round-trip) and
works against any scenario folder with no per-scenario code.

## Run the server

```powershell
.\.venv\Scripts\python.exe simulator\server.py
```

It speaks MCP over stdio — you normally don't run it by hand; you register it in an MCP
client (below) which launches it for you.

## Run the A2A server

The simulator also exposes the same engine over the **A2A protocol** (agent-to-agent), for
callers that reach Work IQ as a *peer agent* rather than as an MCP tool. It speaks
**JSON-RPC 2.0 over HTTP** — the method name (`SendMessage`, or the open-standard alias
`message/send`) goes in the request **body**, POSTed to `/a2a/`. This mirrors the real Work
IQ A2A gateway contract.

```powershell
.\.venv\Scripts\python.exe simulator\a2a_server.py
# default: http://127.0.0.1:8920/a2a/  (card at /.well-known/agent-card.json)
```

A2A maps to the Work IQ **Chat** domain (a cited answer) — it exposes `ask` only. The
**Tools** surface (`fetch` / `create_entity` / `update_entity`) is intentionally MCP-only
(`server.py`). The server is **stdlib-only** (no extra installs). It honours the same
`WORKIQ_SIM_SCENARIO` / `WORKIQ_SIM_PERSONA` variables; persona can also be overridden
per-request via the message `metadata.persona` field or the `X-WorkIQ-Persona` header. Bind
host/port via `WORKIQ_A2A_HOST` / `WORKIQ_A2A_PORT` (port `0` = ephemeral). Auth is **not
enforced** (local mock); persona scoping stands in for identity.

Send a message:

```powershell
$body = '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"role":"user","parts":[{"kind":"text","text":"Who owns the qualification test plan?"}],"metadata":{"persona":"all"}}}}'
Invoke-RestMethod -Uri http://127.0.0.1:8920/a2a/ -Method Post -ContentType application/json -Body $body
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `WORKIQ_SIM_SCENARIO` | `scenarios/c2-contoso` | Scenario folder to load (absolute or relative to `server.py`). Ships with `c1-northbridge` and `c2-contoso`. |
| `WORKIQ_SIM_PERSONA` | `new_pm` | Active persona for permission trimming. **Persona ids are scenario-specific:** C2 (Contoso) = `new_pm` / `quality_engineer` / `contractor` / `director`; C1 (Northbridge) = `ops_director` / `quality_pm` / `credentialing_lead` / `vendor_liaison`. Set to `all` (or unset) for full visibility. |
| `OPENAI_API_KEY` | _(unset)_ | Optional. Enables LLM fallback for non-golden questions. |
| `OPENAI_BASE_URL` | _(unset)_ | Optional. OpenAI-compatible endpoint (Azure OpenAI, local, etc.). |
| `MODEL` | `gpt-4o-mini` | Optional. Model name for the fallback. |

---

## Drop-in MCP config

The simulator registers exactly like the real `workiq` MCP server — same tool name
(`ask_work_iq`), so your agent code is unchanged.

**Simulator (local, no tenant):**
```json
{
  "mcpServers": {
    "workiq": {
      "command": "C:\\path\\to\\workiq-hackathon\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\workiq-hackathon\\simulator\\server.py"],
      "env": { "WORKIQ_SIM_PERSONA": "new_pm" }
    }
  }
}
```

**Real Work IQ (when you have a tenant)** — swap `command`/`args`, keep everything else:
```json
{
  "mcpServers": {
    "workiq": {
      "command": "npx",
      "args": ["-y", "@microsoft/workiq@preview", "mcp", "start"]
    }
  }
}
```

To switch the demo persona for the RBAC governance story, change `WORKIQ_SIM_PERSONA`
and restart the MCP client.

---

## The tool contract

### `ask_work_iq(question: string, fileUrls?: string[] | null) -> JSON`
Identical shape to the real server. Returns:
```json
{
  "response": "…the cited answer…",
  "conversationId": "sim-…",
  "citations": [
    { "citation_id": "MTG-001", "source_index": 1, "title": "Meeting: TR-7 Weekly Design Review (2026-06-11)", "kind": "meeting", "sensitivity": "internal" }
  ]
}
```

### Tools surface (superset the challenges need)
- `fetch(table, filter?)` — read rows from any auto-discovered Tools table (e.g. `milestone_tracker`, `capa_tracker`).
- `create_entity(table, record)` — append a row (idempotent on `id`; id-prefix derived from existing rows, e.g. `MS-005`, `CAPA-006`). C2's hero action.
- `update_entity(table, id, patch)` — patch fields on a row (e.g. flip a CAPA `status` and set `past_due`). C1's hero action.

> The real **stable** Work IQ MCP server exposes only `ask_work_iq` (Chat/Context); the
> Entity/Tools surface is platform/preview-only. The simulator adds it so the
> "Agent with MCP / Tools" challenge tier is demonstrable locally. Writes are **in-memory
> by default** (set `persist=True` in `engine.create_entity`/`update_entity` to write the
> JSON back).

---

## Governance demo (RBAC / permission trimming)

Every fixture carries an `acl` (list of persona ids, or `["all"]`). A persona sees a
fixture only if its id is in the acl (or the acl is `["all"]`). The customer-escalation
emails (`EML-001/002`) and the supplier agreement (`FILE-003`) are `restricted` to
program leadership.

- Run as `new_pm` → the handover brief **includes** the customer escalation.
- Run as `contractor` → the same question is answered, but the restricted sources are
  **withheld from both the citations *and* the answer text** (a persona-safe redacted
  variant is returned), and the response appends a `[Governance]` note naming what was
  trimmed. Redaction is fail-closed: if no redacted variant is authored, the answer is
  suppressed entirely rather than risk leaking restricted prose.

This is the same answer pipeline trimming on identity — the RBAC narrative the rubric rewards.

> **C1 (Northbridge) RBAC matrix** shows cross-cutting trimming: the leadership/vendor *commercial* thread
> (`EML-003/004`) is visible to `ops_director` + `quality_pm` but withheld from `credentialing_lead` and
> `vendor_liaison`; the HR-sensitive *credentialing* file (`FILE-003`) is visible to `credentialing_lead` +
> `ops_director` but withheld from `quality_pm` — so no single non-director persona sees everything.

> **Robustness notes:** citation objects include a placeholder `url` so UI link-rendering
> doesn't break; unknown table names return a structured `{error, available_tables}` JSON
> the agent can self-correct from; and an unknown/misspelled `WORKIQ_SIM_PERSONA` logs a
> startup warning to stderr (and fails closed to public-only visibility).

---

## Adding Challenges 3–6 (data-only)

Copy a scenario folder, replace the seven fixture files + `tables/*.json`, and run
`.\.venv\Scripts\python.exe simulator\tests\validate_scenario.py scenarios\<new>` from the repo root. No `engine.py` / `server.py` changes are needed — the engine
discovers personas, tables, citation kinds, and id-prefixes from the data.
