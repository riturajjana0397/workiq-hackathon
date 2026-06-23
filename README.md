<!--
  Metadata
  File:    README.md
  Created: 18-JUN-2026 (time: repo packaging)
  Role:    Top-level getting-started guide for the Work IQ Hackathon repo.
-->

# Microsoft Work IQ Hackathon

Everything a team needs to take on a **Work IQ** hackathon challenge — the challenge
pack and a **local simulator** so you can build and test **without a
Microsoft 365 tenant**.

> **Work IQ** grounds answers in your *live work context* — email, meetings, chats,
> files, people, calendar and Copilot memory — reached over **MCP** and **A2A**.

**Your build target:** an agent built with the **Microsoft Agent Framework** on an **Azure AI
Foundry** model, connected to Work IQ over **both MCP and A2A**. See
[**Build your agent**](#build-your-agent) below.

---

## What's in this repo

```
workiq-hackathon/
  challenge-pack/     # The challenge pack PDF you read first
  simulator/          # Local Work IQ simulator — 6 challenge scenarios, MCP + A2A servers, tests
  starter-kit/        # MCP connection smoke-tests and a reference MCP config
  README.md           # You are here
```

| Folder | Start here |
|---|---|
| `challenge-pack/WorkIQ-Hackathon-Challenge-Pack_14-JUN-2026.pdf` | The 6 challenges, judging criteria, capability tiers. **Read first.** |

> **Setup Guide:** the participant setup guide for **real** Work IQ (Path B) is **not
> included in this repo** — request it from the hackathon organizers.

---

## Pick your path

| | Path A — Local simulator | Path B — Real Work IQ |
|---|---|---|
| **Needs a tenant?** | ❌ No | ✅ Yes (M365 + Copilot, admin consent) |
| **Best for** | Building & testing logic fast, offline | The final, production-grade demo |
| **Setup** | 3 commands (below) | Follow the **Setup Guide** (from organizers) |

You can build your whole solution against **Path A**, then swap the MCP endpoint to the
real server for **Path B** — your agent code doesn't change.

---

## Quick start — Path A (local simulator)

### ⚡ Get the simulator running in 60 seconds

**Need:** Python 3.10+ on your PATH. Run from the repo root (`workiq-hackathon/`):

```powershell
# 1. Clone (skip if you already have the repo)
git clone https://github.com/kanhaiyasingh/workiq-hackathon.git
cd workiq-hackathon

# 2. Create a venv + install the one dependency
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r simulator\requirements.txt

# 3. Verify it works (prints "ALL ... PASSED")
.\.venv\Scripts\python.exe simulator\tests\smoke.py

# 4. Ask it a question 🎉
.\.venv\Scripts\python.exe simulator\demo.py --ask "What is blocking qualification?"
```

> macOS / Linux: use `python3 -m venv .venv` and `.venv/bin/python` instead of
> `.\.venv\Scripts\python.exe`. If you see "python not found", install Python 3.10+ first.

Done — you're ready to build. The full walkthrough is below.

---

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

### Plug it into your agent (MCP)

Register the simulator like the real Work IQ MCP server — same tool name
(`ask_work_iq`), so your agent code is unchanged. See
[`simulator/README.md`](simulator/README.md) for the full MCP + A2A config and wire
contracts.

---

## Build your agent

Your deliverable is an **agentic app**: a reasoning model that calls Work IQ, decides when to
retrieve, and returns cited answers. The challenge is built and judged around a specific stack —
**use it**:

1. **Reasoning model — Azure AI Foundry.** Deploy a chat model in an **Azure AI Foundry** project
   (e.g. `gpt-4o-mini`) and drive your agent with it.
2. **Agent runtime — Microsoft Agent Framework.** Build the agent with the **Microsoft Agent
   Framework** (Python). It hosts the model, runs the tool-calling loop, and connects to MCP/A2A.
3. **Connect to Work IQ over BOTH transports.** Wire the **local simulator** into your agent over
   **MCP _and_ A2A**. The same wiring works against real Work IQ later — you only swap the endpoint.

### 1. Install

```powershell
.\.venv\Scripts\python.exe -m pip install agent-framework agent-framework-foundry agent-framework-a2a azure-identity
```

### 2. Configure your Foundry model (+ Entra auth)

```powershell
az login
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<your-foundry-project>.services.ai.azure.com/api/projects/<project>"
$env:FOUNDRY_MODEL            = "gpt-4o-mini"   # your chat deployment name
```

### 3. Wire it up (the building blocks — assembling them is the challenge)

- **Model client** — `FoundryChatClient` (from `agent_framework_foundry`): reads the `FOUNDRY_*`
  env vars above and authenticates with `AzureCliCredential` (from `azure.identity`).
- **Agent** — `Agent` (from `agent_framework`): give it the model client, your instructions, and
  the Work IQ tool(s). Let the **model** decide when to call Work IQ — don't hard-code retrieval.
- **MCP transport** — `MCPStdioTool` (from `agent_framework`): launch `simulator/server.py` as the
  tool process so the model can call `ask_work_iq` (plus `fetch` / `create_entity` /
  `update_entity` for the write tier). Set `WORKIQ_SIM_SCENARIO` and `WORKIQ_SIM_PERSONA` in its env.
- **A2A transport** — run `simulator/a2a_server.py`, then reach it with `A2AAgent` (from
  `agent_framework_a2a`) pointed at `http://127.0.0.1:8920`, and expose it to your orchestrating
  `Agent` via `.as_tool(...)`.

**Your agent must:** (1) run on a **Foundry** model via the **Microsoft Agent Framework**,
(2) reach Work IQ over **both MCP and A2A**, (3) let the model decide when to retrieve, and
(4) surface the **citations** Work IQ returns. The tool contract, env vars, and per-transport wire
details are in [`simulator/README.md`](simulator/README.md) — start there.

> **Persona = identity.** Set `WORKIQ_SIM_PERSONA` to demo governance: an under-privileged
> persona gets restricted sources withheld with a note, while the rest of the answer still returns.

---

## Quick start — Path B (real Work IQ)

Request the **Participant Setup Guide** from the hackathon organizers (it is not included
in this repo) and follow it end to end: tenant prerequisites, admin consent for
`WorkIQAgent.Ask`, the service principal, Copilot licensing, and registering the real MCP
endpoint. The `starter-kit/` scripts get you to a first call quickly.

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

## ⭐ Bonus: Use Work IQ from GitHub Copilot CLI (Windows)

Want to chat with **real Work IQ** straight from your terminal? Wire the **Work IQ MCP
server** into **GitHub Copilot CLI** and ask questions in plain English. This is **optional**
— only useful for **Path B** (real Work IQ), and you'll still need the tenant prerequisites
from the Setup Guide.

> **Heads-up:** the Work IQ CLI talks to a **live Microsoft 365 tenant**. For offline
> building, stick with **Path A** (the local simulator) — no install needed.

**1. Prerequisites (Windows Terminal → PowerShell)**

```powershell
# Node.js 18+ (provides npx) and GitHub Copilot CLI
winget install OpenJS.NodeJS.LTS
npm install -g @github/copilot

# Verify
node --version
copilot --version
```

**2. Install the Work IQ CLI**

```powershell
# Installs the Work IQ CLI/MCP package globally
npm install -g @microsoft/workiq@preview

# Sign in to your tenant (opens a browser)
workiq auth login

# Smoke-test the CLI
workiq --version
```

> Prefer not to install globally? You can skip this step — the MCP config below uses
> `npx`, which fetches the package on demand.

**3. Register the Work IQ MCP server with Copilot CLI**

The fastest way is the interactive command — launch `copilot`, then run:

```text
/mcp add
```

…and provide: name `workiq`, command `npx`, args `-y @microsoft/workiq@preview mcp start`.

Or add it manually to `~/.copilot/mcp-config.json` (`%USERPROFILE%\.copilot\mcp-config.json`):

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

**4. Ask away** — restart Copilot CLI and try:

```text
What activities am I doing for the AGCO project today?
```

Run `/mcp` inside Copilot CLI any time to confirm the `workiq` server is connected. 🎉

---

## Need more detail?

- **The challenges** → `challenge-pack/WorkIQ-Hackathon-Challenge-Pack_14-JUN-2026.pdf`
- **Real Work IQ setup** → Participant Setup Guide (request from organizers — not in this repo)
- **Simulator internals, MCP/A2A config, tool contract** → [`simulator/README.md`](simulator/README.md)

Happy hacking. 🛠️
