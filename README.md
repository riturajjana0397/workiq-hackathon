<!--
  Metadata
  File:    README.md
  Created: 18-JUN-2026 (time: repo packaging)
  Role:    Top-level getting-started guide for the Work IQ Hackathon repo.
-->

# Microsoft Work IQ Hackathon

Everything a team needs to take on a **Work IQ** hackathon challenge — the challenge
pack, a setup guide, and a **local simulator** so you can build and test **without a
Microsoft 365 tenant**.

> **Work IQ** grounds answers in your *live work context* — email, meetings, chats,
> files, people, calendar and Copilot memory — reached over **MCP** and **A2A**.

---

## What's in this repo

```
workiq-hackathon/
  challenge-pack/     # The PDFs you read first (challenge pack + setup guide)
  simulator/          # Local Work IQ simulator — 6 challenge scenarios, MCP + A2A servers, tests
  starter-kit/        # MCP connection smoke-tests and a reference MCP config
  README.md           # You are here
```

| Folder | Start here |
|---|---|
| `challenge-pack/WorkIQ-Hackathon-Challenge-Pack_14-JUN-2026.pdf` | The 6 challenges, judging criteria, capability tiers. **Read first.** |
| `challenge-pack/WorkIQ-Hackathon-Participant-Setup-Guide_14-JUN-2026.pdf` | Step-by-step environment setup — only needed for **real** Work IQ (Path B). |

---

## Pick your path

| | Path A — Local simulator | Path B — Real Work IQ |
|---|---|---|
| **Needs a tenant?** | ❌ No | ✅ Yes (M365 + Copilot, admin consent) |
| **Best for** | Building & testing logic fast, offline | The final, production-grade demo |
| **Setup** | 3 commands (below) | Follow the **Setup Guide PDF** |

You can build your whole solution against **Path A**, then swap the MCP endpoint to the
real server for **Path B** — your agent code doesn't change.

---

## Quick start — Path A (local simulator)

**Prerequisite:** Python 3.10+ on your PATH.

From the repo root (`workiq-hackathon/`):

```powershell
# 1. Create an isolated environment
python -m venv .venv

# 2. Install the simulator's only dependency (mcp)
.\.venv\Scripts\python.exe -m pip install -r simulator\requirements.txt

# 3. Confirm everything works (each prints "ALL ... PASSED")
.\.venv\Scripts\python.exe simulator\tests\smoke.py
.\.venv\Scripts\python.exe simulator\tests\mcp_e2e.py
.\.venv\Scripts\python.exe simulator\tests\a2a_e2e.py
```

> macOS / Linux: use `python3 -m venv .venv` then `.venv/bin/python` instead of
> `.\.venv\Scripts\python.exe`.

### Ask the simulator a question

```powershell
# Default challenge (c2-contoso), default persona
.\.venv\Scripts\python.exe simulator\demo.py --ask "What is blocking qualification?"

# Try the RBAC governance demo — same question, different persona = redacted answer
.\.venv\Scripts\python.exe simulator\demo.py --persona contractor --ask "Give me the 45621-B handover brief."
```

### Validate any of the 6 challenge scenarios

```powershell
.\.venv\Scripts\python.exe simulator\tests\validate_scenario.py scenarios\c1-northbridge
.\.venv\Scripts\python.exe simulator\tests\validate_scenario.py scenarios\c2-contoso
# ... c3-meridian, c4-arundel, c5-westbrook, c6-edkh
```

### Plug it into your agent (MCP)

Register the simulator like the real Work IQ MCP server — same tool name
(`ask_work_iq`), so your agent code is unchanged. See
[`simulator/README.md`](simulator/README.md) for the full MCP + A2A config and wire
contracts.

---

## Build your agent

The simulator is your Work IQ stand-in. It exposes the **same surface as the real thing**, so
whatever you build here works unchanged against production Work IQ later:

- **MCP** — `simulator/server.py` exposes the `ask_work_iq` tool (plus the Tools actions
  `fetch` / `create_entity` / `update_entity`). Register it like any MCP server.
- **A2A** — `simulator/a2a_server.py` serves Work IQ as a peer agent (JSON-RPC over HTTP,
  agent card at `/.well-known/agent-card.json`).

How you turn that into an agent is the challenge. Pick your own LLM, framework, and transport,
let the model decide when to call Work IQ, and make sure every answer carries the citations the
tool returns. The tool contract, environment variables, and persona/RBAC behaviour are in
[`simulator/README.md`](simulator/README.md) — start there.

> **Persona = identity.** Set `WORKIQ_SIM_PERSONA` to demo governance: an under-privileged
> persona gets restricted sources withheld with a note, while the rest of the answer still returns.

---

## Quick start — Path B (real Work IQ)

Open **`challenge-pack/WorkIQ-Hackathon-Participant-Setup-Guide_14-JUN-2026.pdf`** and
follow it end to end: tenant prerequisites, admin consent for `WorkIQAgent.Ask`, the
service principal, Copilot licensing, and registering the real MCP endpoint. The
`starter-kit/` scripts get you to a first call quickly.

---

## Starter kit

Connection helpers in `starter-kit/` to confirm your MCP wiring before you build (rename /
repath as needed):

| File | What it does |
|---|---|
| `workiq-mcp-smoke_14-JUN-2026.mjs` | Confirm your MCP connection + tool list. |
| `workiq-smoke-test_14-JUN-2026.ps1` | PowerShell smoke test. |
| `workiq-mcp-config_14-JUN-2026.json` | Reference MCP server config. |

> These verify connectivity only — building the agent itself is up to you.

---

## Need more detail?

- **The challenges** → `challenge-pack/WorkIQ-Hackathon-Challenge-Pack_14-JUN-2026.pdf`
- **Real Work IQ setup** → `challenge-pack/WorkIQ-Hackathon-Participant-Setup-Guide_14-JUN-2026.pdf`
- **Simulator internals, MCP/A2A config, tool contract** → [`simulator/README.md`](simulator/README.md)

Happy hacking. 🛠️
