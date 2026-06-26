# Work IQ Orchestrator Agent

A small Microsoft Agent Framework app that:

1. Talks to your **Azure AI Foundry** deployment via the OpenAI-compatible
   `/openai/v1` endpoint, authenticated with `DefaultAzureCredential` (no
   API keys — uses your `az login` / Managed Identity / VS Code session).
2. Wires the local **Work IQ simulator** in as a tool surface over **both**
   transports:
   - **MCP (stdio)** — spawns `simulator/server.py` as a child process.
     Exposes the low-level tools `ask_work_iq`, `fetch`, `create_entity`,
     `update_entity`.
   - **A2A (HTTP)** — talks to `simulator/a2a_server.py` as a remote sub-agent
     for chat-style grounded answers.
3. Lets the model decide per turn which transport to use.

The same wiring will work against the **real** Work IQ later — only the
endpoint changes.

---

## Setup

```powershell
# 1) Python packages (already installed if you've followed along)
.\.venv\Scripts\python.exe -m pip install `
    agent-framework agent-framework-foundry agent-framework-a2a `
    azure-identity openai

# 2) Sign in for Entra ID auth
az login
```

## Configure

```powershell
$env:AZURE_AI_FOUNDRY_ENDPOINT   = "https://<your-resource>.services.ai.azure.com/openai/v1"
$env:AZURE_AI_FOUNDRY_DEPLOYMENT = "gpt-4o-mini"     # your deployment name
$env:WORKIQ_SIM_PERSONA          = "new_pm"           # or quality_engineer | contractor | director
```

## Run

In **terminal A** — start the A2A side of the simulator (the MCP side is
launched automatically by the agent):
```powershell
.\.venv\Scripts\python.exe simulator\a2a_server.py
```

In **terminal B** — run the agent:
```powershell
# one-shot
.\.venv\Scripts\python.exe agent\workiq_agent.py --ask "what is blocking PPAP qualification?"

# interactive REPL
.\.venv\Scripts\python.exe agent\workiq_agent.py
```

## Things to try

| Prompt | Watch for |
|---|---|
| `what did we decide in the last design review?` | A2A surface used; citations like `MTG-001` |
| `fetch every milestone tracker row whose status is At Risk` | MCP `fetch` called |
| `summarise the open qualification blockers and open a tracked risk item for each one` | A2A then MCP `create_entity` (idempotent) |
| Re-run the same question after `$env:WORKIQ_SIM_PERSONA="contractor"` | RBAC kicks in — restricted facts redacted, governance note surfaced |

## Notes / troubleshooting

- **Auth fails** — make sure `az login` was successful and your account has
  Cognitive Services User on the Foundry resource.
- **A2AAgent / OpenAIChatClient import errors** — the exact import path
  depends on your installed `agent-framework` version. On older builds try
  `from agent_framework_a2a import A2AAgent` and check the changelog.
- **MCP subprocess won't start** — the script uses `.\.venv\Scripts\python.exe`
  by default; edit `VENV_PY` in `workiq_agent.py` if your venv lives elsewhere.
