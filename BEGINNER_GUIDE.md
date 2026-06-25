# Work IQ Beginner Guide

This project is a local practice environment for a Work IQ hackathon. It lets you
build and test an AI agent without needing a Microsoft 365 tenant.

## What this project is

The repo contains three main pieces:

1. The challenge pack, which explains the problems you are supposed to solve.
2. A local simulator, which acts like Work IQ using fake but realistic company data.
3. An agent app, which asks questions and uses the simulator to get grounded answers.

You do not need to understand everything at once. The simplest way to think about it
is:

- The simulator is the fake work system.
- The agent is the thing you run and talk to.
- MCP and A2A are the two ways the agent talks to the simulator.

## What NorthBridge Health Network is

NorthBridge Health Network is the c1 scenario in the simulator. It is a made-up health
organization used for practice. The data in this scenario includes:

- committee membership and governance discussions
- emails about vendor go-live and leadership topics
- meetings about quality and credentialing
- Teams-style messages for rollout coordination
- files such as policy drafts and a CAPA tracker

The important point is that this is not real patient data. It is simulated business
context for the agent to answer questions from.

## What the NorthBridge stream is doing

The NorthBridge stream is the storyline you work inside for the c1 challenge. Your job
is to use the agent to answer questions based on the available work context.

In plain English, you are trying to do things like:

- find what is blocking a rollout or qualification
- summarize what was decided in meetings
- identify who owns an action item
- update the tracked CAPA item when the scenario asks for it

The simulator will only show you what your selected persona is allowed to see. That is
why the persona matters.

## What you are supposed to do

For c1-northbridge, your goal is usually to:

1. Start the simulator.
2. Ask questions through the agent.
3. Read the grounded answer and citations.
4. Use the right persona so you see the right data.

If you are just learning, start with these questions:

- What is blocking qualification?
- What did the quality team decide?
- Who owns the action item?

## The simplest setup

Use one terminal for the A2A simulator and one terminal for the agent.

Terminal 1:

```powershell
$env:WORKIQ_SIM_SCENARIO = "scenarios/c1-northbridge"
$env:WORKIQ_SIM_PERSONA = "ops_director"
.\.venv\Scripts\python.exe simulator\a2a_server.py
```

Terminal 2:

```powershell
$env:AZURE_AI_FOUNDRY_ENDPOINT = "https://iqs-ai-nqgxoe2zunc6q.openai.azure.com/openai/v1"
$env:AZURE_AI_FOUNDRY_DEPLOYMENT = "gpt-4o-mini"
.\.venv\Scripts\python.exe agent\workiq_agent.py --ask "What is blocking qualification?"
```

If you want a browser UI instead of the terminal, run `agent/web.py` and open
`http://127.0.0.1:8000`.

## What MCP and A2A mean

You do not need to master these to get started, but here is the short version:

- MCP is the tool interface. The agent uses it for Work IQ-style tools and data access.
- A2A is the chat interface. The agent uses it to send a natural-language question to
  the simulator and get a cited answer back.

## What success looks like

You are done with the basic setup when:

- the simulator starts without errors
- the agent runs without import errors
- you can ask a question and get a cited answer
- the answer matches the c1-northbridge scenario

## If you get stuck

Common issues are:

- wrong scenario selected
- wrong persona selected
- the A2A server is not running
- the Foundry endpoint is missing or incorrect
- the Azure login is not available

If that happens, go back to the three things above: scenario, persona, and terminal
setup.

## Short glossary

- Agent: the app that thinks and asks tools for help.
- Simulator: the local fake Work IQ system.
- Foundry: the Azure AI model endpoint your agent uses.
- MCP: the tool channel to the simulator.
- A2A: the chat channel to the simulator.
- Persona: the role that controls what data is visible.

If you want, read this guide first, then use the main README when you are ready to
follow the exact setup commands.