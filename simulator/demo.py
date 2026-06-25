r"""
Local Work IQ Simulator — interactive demo / manual test runner (default scenario C2: Contoso Precision Parts).

Metadata
--------
Created:   14-JUN-2026
Component: demo.py
Role:      Human-readable way to TEST the simulator the way a hackathon participant will,
           without standing up an MCP client. Prints each question, the grounded answer,
           and the resolved citations, and lets you switch personas to see the RBAC
           (permission-trimming) governance story.

Usage
-----
  # Run all 8 Challenge-1 compound questions as the default persona (new_pm):
  .\.venv\Scripts\python.exe simulator\demo.py

  # Run as a specific persona (ops_director | quality_pm | credentialing_lead | vendor_liaison):
  .\.venv\Scripts\python.exe simulator\demo.py --persona contractor

  # Ask one ad-hoc question:
  .\.venv\Scripts\python.exe simulator\demo.py --ask "what is blocking qualification?"

  # RBAC contrast: ask the SAME question across all personas (great for the demo):
  .\.venv\Scripts\python.exe simulator\demo.py --rbac 2

  # Interactive REPL (type questions, /persona <id> to switch, /quit to exit):
  .\.venv\Scripts\python.exe simulator\demo.py --repl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SIM_DIR))

# Windows consoles default to cp1252 and crash on em-dashes / warning glyphs in the
# fixtures. Force UTF-8 so the demo renders everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

import engine  # noqa: E402

DEFAULT_SCENARIO = SIM_DIR / "scenarios" / "c1-northbridge"

# ANSI helpers (degrade gracefully if the terminal doesn't render them).
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def banner(text: str) -> str:
    return _c("1;36", text)


def render(result: dict, question: str, persona_id: str | None) -> None:
    print(banner("\n" + "=" * 78))
    print(banner(f"Q: {question}"))
    print(_c("2", f"persona={persona_id or 'all'}  source={result['source']}  "
                  f"matched={result.get('matched')}"))
    print("-" * 78)
    print(result["response"])
    cits = result["citations"]
    if cits:
        print(_c("1;33", "\nCitations:"))
        for c in cits:
            print(f"  [{c['source_index']}] {c['citation_id']:<10} "
                  f"{_c('2', c['kind']):<22} {c['title']}")
    if result.get("trimmed"):
        print(_c("1;31", f"\n⚠ Withheld for this persona: {', '.join(result['trimmed'])}"))


def run_all(sc: engine.Scenario, persona_id: str | None) -> None:
    print(banner(f"\n### Running all {len(sc.golden)} Challenge-1 questions as "
                 f"persona='{persona_id or 'all'}' ###"))
    for g in sc.golden:
        result = engine.ask(sc, g["question"], persona_id=persona_id)
        render(result, g["question"], persona_id)


def run_rbac(sc: engine.Scenario, qnum: int) -> None:
    g = next((x for x in sc.golden if x["id"] == f"Q{qnum}"), None)
    if g is None:
        print(f"No question Q{qnum}; valid: {[x['id'] for x in sc.golden]}")
        return
    print(banner(f"\n### RBAC contrast on {g['id']} across personas ###"))
    print(banner(f"Question: {g['question']}\n"))
    for p in sc.personas:
        result = engine.ask(sc, g["question"], persona_id=p["id"])
        print(banner(f"\n----- persona: {p['id']} ({p['label']}) -----"))
        print(result["response"])
        ids = [c["citation_id"] for c in result["citations"]]
        print(_c("1;33", f"Citations: {ids or '(none)'}"))
        if result.get("trimmed"):
            print(_c("1;31", f"⚠ Withheld: {', '.join(result['trimmed'])}"))


def run_repl(sc: engine.Scenario, persona_id: str | None) -> None:
    print(banner("Interactive Work IQ simulator. Commands: /persona <id>, /personas, /quit"))
    while True:
        try:
            line = input(_c("1;32", f"\n[{persona_id or 'all'}] ask> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line == "/personas":
            print("  " + ", ".join(sc.persona_ids()))
            continue
        if line.startswith("/persona"):
            parts = line.split(maxsplit=1)
            persona_id = parts[1].strip() if len(parts) > 1 else None
            if persona_id and persona_id not in sc.persona_ids():
                print(_c("1;31", f"Unknown persona; valid: {sc.persona_ids()}"))
                persona_id = None
            print(_c("2", f"persona set to {persona_id or 'all'}"))
            continue
        result = engine.ask(sc, line, persona_id=persona_id)
        render(result, line, persona_id)


def main() -> int:
    ap = argparse.ArgumentParser(description="Work IQ simulator demo / manual tester")
    ap.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    ap.add_argument("--persona", default="quality_pm",
                    help="ops_director | quality_pm | credentialing_lead | vendor_liaison | all")
    ap.add_argument("--ask", help="ask a single question and exit")
    ap.add_argument("--rbac", type=int, metavar="N",
                    help="show question QN across all personas")
    ap.add_argument("--repl", action="store_true", help="interactive prompt")
    args = ap.parse_args()

    sc = engine.load_scenario(args.scenario)
    persona = None if args.persona.lower() == "all" else args.persona
    if persona and persona not in sc.persona_ids():
        print(_c("1;31", f"Unknown persona '{persona}'. Valid: {sc.persona_ids()}"))
        return 2

    if args.rbac is not None:
        run_rbac(sc, args.rbac)
    elif args.ask:
        render(engine.ask(sc, args.ask, persona_id=persona), args.ask, persona)
    elif args.repl:
        run_repl(sc, persona)
    else:
        run_all(sc, persona)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
