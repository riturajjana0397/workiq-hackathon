"""
Local Work IQ Simulator — retrieval + synthesis engine (scenario C2: Contoso Precision Parts / 45621-B).

Metadata
--------
Created:        14-JUN-2026 (authoring date)
Component:      engine.py
Role:           Loads synthetic fixtures, answers compound questions via golden-answer
                keyword matching (works with NO model) with an optional LLM fallback,
                applies persona-based permission trimming, resolves citations, and backs
                the Tools surface (fetch / create_entity / update_entity) against the
                Dataverse-style milestone tracker.

Design
------
- The MCP *contract* is identical to the real Work IQ server; only the backend differs.
- Golden answers guarantee deterministic, citable responses for the 8 scripted C1
  questions even when no model is configured. LLM fallback (OpenAI-compatible env vars)
  handles ad-hoc questions.
- Permission model: every fixture carries an `acl` (list of persona ids, or ["all"]).
  A persona sees a fixture iff its id is in the acl OR the acl contains "all".
  Restricted citations that get trimmed produce a governance note (the RBAC demo).
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


# --------------------------------------------------------------------------- #
# Fixture loading
# --------------------------------------------------------------------------- #

# Maps a fixture file (relative to the scenario dir) to the top-level list key inside it.
# Tools-backed tables are NOT listed here — they are discovered dynamically from the
# scenario's `tables/` folder so new scenarios (C2..C6) need zero engine changes.
FIXTURE_FILES: dict[str, str] = {
    "people.json": "people",
    "emails.json": "emails",
    "meetings.json": "meetings",
    "teams.json": "teams_messages",
    "files.json": "files",
    "personas.json": "personas",
    "golden.json": "golden",
}

# Sub-folder (relative to a scenario root) holding the editable Tools tables.
TABLES_DIR = "tables"


@dataclass
class Scenario:
    """In-memory representation of a loaded scenario."""

    root: Path
    people: list[dict] = field(default_factory=list)
    emails: list[dict] = field(default_factory=list)
    meetings: list[dict] = field(default_factory=list)
    teams_messages: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    personas: list[dict] = field(default_factory=list)
    golden: list[dict] = field(default_factory=list)
    # Tools-backed tables, keyed by file stem (e.g. "milestone_tracker", "capa_tracker").
    tables: dict[str, list[dict]] = field(default_factory=dict)
    # Original on-disk shape per table ("dict" or "list") so persistence round-trips faithfully.
    table_formats: dict[str, str] = field(default_factory=dict)

    # id -> (kind, record) for every citable entity (including action items).
    index: dict[str, tuple[str, dict]] = field(default_factory=dict)

    def persona_ids(self) -> list[str]:
        return [p["id"] for p in self.personas]

    def table_names(self) -> list[str]:
        return list(self.tables.keys())

    def get_persona(self, persona_id: str | None) -> dict | None:
        if persona_id is None:
            return None
        for p in self.personas:
            if p["id"] == persona_id:
                return p
        return None


def _kind_for_table(table: str) -> str:
    """Singular citation 'kind' for a table (milestone_tracker -> milestone)."""
    for suffix in ("_tracker", "_table", "_log", "_pipeline"):
        if table.endswith(suffix):
            return table[: -len(suffix)]
    return table


def _prefix_for_table(rows: list[dict], table: str) -> str:
    """Derive an id prefix from existing row ids (e.g. 'MS-001' -> 'MS', 'CAPA-007' ->
    'CAPA'); fall back to the uppercased table initials."""
    for row in rows:
        rid = row.get("id")
        if isinstance(rid, str) and "-" in rid:
            head = rid.rsplit("-", 1)[0]
            if head:
                return head
    return "".join(w[0] for w in table.split("_") if w).upper() or "ROW"


def load_scenario(scenario_dir: str | Path) -> Scenario:
    """Load every fixture file and build the citation index."""
    root = Path(scenario_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Scenario directory not found: {root}")

    sc = Scenario(root=root)
    for rel, key in FIXTURE_FILES.items():
        path = root / rel
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        setattr(sc, key, data.get(key, []))

    # Discover Tools tables: every JSON file under tables/, keyed by its stem. Each file
    # is either {"<stem>": [...]} or a bare list.
    tables_path = root / TABLES_DIR
    if tables_path.is_dir():
        for tf in sorted(tables_path.glob("*.json")):
            with open(tf, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            stem = tf.stem
            if isinstance(data, dict):
                sc.table_formats[stem] = "dict"
                rows = data.get(stem)
                if rows is None:
                    # Author used a different inner key than the file stem. Fall back to
                    # the first list-valued key so the data isn't silently dropped, and warn.
                    list_keys = [k for k, v in data.items()
                                 if k != "_comment" and isinstance(v, list)]
                    if list_keys:
                        sys.stderr.write(
                            f"[workiq-sim] WARNING: table '{tf.name}' has no '{stem}' key; "
                            f"using '{list_keys[0]}' instead.\n"
                        )
                        rows = data[list_keys[0]]
                    else:
                        sys.stderr.write(
                            f"[workiq-sim] WARNING: table '{tf.name}' has no list rows; "
                            f"registering it empty.\n"
                        )
                        rows = []
            else:
                sc.table_formats[stem] = "list"
                rows = data
            sc.tables[stem] = rows or []

    _build_index(sc)
    return sc


def _build_index(sc: Scenario) -> None:
    """Index every citable entity by its stable id. Action items are indexed too."""
    idx: dict[str, tuple[str, dict]] = {}

    for person in sc.people:
        idx[person["id"]] = ("person", person)
    for email in sc.emails:
        idx[email["id"]] = ("email", email)
    for msg in sc.teams_messages:
        idx[msg["id"]] = ("teams_message", msg)
    for f in sc.files:
        idx[f["id"]] = ("file", f)
    for table, rows in sc.tables.items():
        kind = _kind_for_table(table)
        for row in rows:
            rid = row.get("id")
            if not rid:
                sys.stderr.write(
                    f"[workiq-sim] WARNING: row in table '{table}' is missing an 'id'; "
                    f"skipping it (not citable): {row}\n"
                )
                continue
            idx[rid] = (kind, row)
    for mtg in sc.meetings:
        idx[mtg["id"]] = ("meeting", mtg)
        for ai in mtg.get("action_items", []):
            # action items inherit the parent meeting's acl for trimming
            ai_record = dict(ai)
            ai_record.setdefault("acl", mtg.get("acl", ["all"]))
            ai_record["_meeting_id"] = mtg["id"]
            idx[ai["id"]] = ("action_item", ai_record)

    sc.index = idx


# --------------------------------------------------------------------------- #
# Permission trimming
# --------------------------------------------------------------------------- #

def _acl_of(record: dict) -> list[str]:
    acl = record.get("acl")
    if not acl:
        return ["all"]
    return acl


def can_see(record: dict, persona_id: str | None) -> bool:
    """A record is visible if its acl contains 'all', or contains the persona id.

    When persona_id is None (no persona selected) the simulator grants full
    visibility — mirroring an unscoped admin/dev session.
    """
    acl = _acl_of(record)
    if "all" in acl:
        return True
    if persona_id is None:
        return True
    return persona_id in acl


# --------------------------------------------------------------------------- #
# Citation resolution
# --------------------------------------------------------------------------- #

def _title_for(kind: str, record: dict) -> str:
    if kind == "person":
        return f"{record.get('name')} — {record.get('title')}"
    if kind == "email":
        return f"Email: {record.get('subject')}"
    if kind == "meeting":
        return f"Meeting: {record.get('title')} ({record.get('date', '')[:10]})"
    if kind == "teams_message":
        return f"Teams ({record.get('channel')}): {record.get('author')}"
    if kind == "file":
        return f"File: {record.get('name')}"
    if kind == "action_item":
        return f"Action item: {record.get('text', '')[:60]}"
    # Generic table row (milestone, capa, engagement, deal, …): pick the best label field.
    label = (
        record.get("milestone")
        or record.get("action")
        or record.get("title")
        or record.get("name")
        or record.get("description")
        or record.get("id")
    )
    return f"{kind.replace('_', ' ').title()}: {label}"


def resolve_citations(
    sc: Scenario, citation_ids: list[str], persona_id: str | None
) -> tuple[list[dict], list[str]]:
    """Resolve citation ids to {citation_id, source_index, title, kind}, trimming any
    the persona may not see. Returns (visible_citations, trimmed_ids)."""
    visible: list[dict] = []
    trimmed: list[str] = []
    source_index = 1
    for cid in citation_ids:
        entry = sc.index.get(cid)
        if entry is None:
            continue
        kind, record = entry
        if not can_see(record, persona_id):
            trimmed.append(cid)
            continue
        visible.append(
            {
                "citation_id": cid,
                "source_index": source_index,
                "title": _title_for(kind, record),
                "kind": kind,
                "sensitivity": record.get("sensitivity", "internal"),
                # Placeholder so UI layers that render clickable citation links don't
                # break; the real Work IQ server returns a Graph webUrl here.
                "url": record.get("url", f"https://simulator.local/{kind}/{cid}"),
            }
        )
        source_index += 1
    return visible, trimmed


# --------------------------------------------------------------------------- #
# Golden-answer matching
# --------------------------------------------------------------------------- #

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


_SUFFIXES = ("ing", "ers", "er", "ed", "es", "s")


def _stem(token: str) -> str:
    """Light, symmetric suffix stripper so 'blocking'/'blockers' both reduce to 'block'.
    Applied to BOTH keywords and the question, so over-stemming stays consistent."""
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if len(token) > len(suf) + 2 and token.endswith(suf):
                token = token[: -len(suf)]
                changed = True
                break
    return token


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in _normalize(text).split() if t}


def _match_stats(question: str, golden: dict) -> tuple[int, float]:
    """(absolute keyword hits, fraction of keywords matched) for ranking golden entries.

    A keyword phrase matches if every (stemmed) word in it appears in the (stemmed)
    question token set — order-independent and morphology-tolerant, so realistic
    paraphrases ('what are the blockers?') still match 'blocking'."""
    qtokens = _tokens(question)
    keywords = golden.get("keywords", [])
    if not keywords:
        return (0, 0.0)
    hits = 0
    for kw in keywords:
        kwt = _tokens(kw)
        if kwt and kwt <= qtokens:
            hits += 1
    return (hits, hits / len(keywords))


def _score_golden(question: str, golden: dict) -> float:
    """Fraction of the golden entry's keyword phrases present in the question.
    Retained for callers (e.g. validate_scenario) that only need the fraction."""
    return _match_stats(question, golden)[1]


def match_golden(sc: Scenario, question: str, threshold: float = 0.5) -> dict | None:
    """Return the best-matching golden entry at/above the fraction `threshold`.

    Ranking is by (absolute hits, fraction): a question that matches more keyword
    phrases outranks one that matches a higher fraction of fewer phrases, so a short
    generic entry can't hijack a longer, more specific one. On exact ties the FIRST
    entry in declaration order wins (deterministic)."""
    best: dict | None = None
    best_key: tuple[int, float] = (-1, -1.0)
    for g in sc.golden:
        hits, frac = _match_stats(question, g)
        if frac >= threshold and (hits, frac) > best_key:
            best_key = (hits, frac)
            best = g
    return best


# --------------------------------------------------------------------------- #
# LLM fallback (optional, OpenAI-compatible)
# --------------------------------------------------------------------------- #

def _llm_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _llm_answer(question: str, context_snippets: list[str]) -> str | None:
    """Best-effort ad-hoc synthesis. Returns None if no model is configured or the
    call fails — the caller then degrades gracefully."""
    if not _llm_available():
        return None
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            api_key=os.environ["OPENAI_API_KEY"],
        )
        model = os.environ.get("MODEL", "gpt-4o-mini")
        context = "\n\n".join(context_snippets[:12])
        prompt = (
            "You are a Work IQ simulator answering from the provided work-context "
            "snippets only. Cite the bracketed ids you use. If the snippets do not "
            "contain the answer, say so.\n\nSNIPPETS:\n"
            f"{context}\n\nQUESTION: {question}\n\nANSWER:"
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content
    except Exception:
        return None


def _all_snippets(sc: Scenario, persona_id: str | None) -> list[dict]:
    """Flatten visible fixtures into (id, text) snippets for retrieval/fallback.
    Every snippet is permission-checked so restricted content never reaches the LLM
    fallback context or the retrieval-only response for an unauthorized persona."""
    snippets: list[dict] = []
    for email in sc.emails:
        if can_see(email, persona_id):
            snippets.append({"id": email["id"], "text": f"{email.get('subject')} :: {email.get('body')}"})
    for mtg in sc.meetings:
        if can_see(mtg, persona_id):
            snippets.append({"id": mtg["id"], "text": f"{mtg.get('title')} :: {mtg.get('recap')}"})
            for ai in mtg.get("action_items", []):
                ai_rec = sc.index.get(ai["id"], (None, ai))[1]
                if can_see(ai_rec, persona_id):
                    snippets.append({"id": ai["id"], "text": f"Action item ({mtg.get('title')}): {ai.get('text')} (owner {ai.get('owner')}, due {ai.get('due')}, {ai.get('status')})"})
    for msg in sc.teams_messages:
        if can_see(msg, persona_id):
            snippets.append({"id": msg["id"], "text": f"{msg.get('channel')} :: {msg.get('text')}"})
    for f in sc.files:
        if can_see(f, persona_id):
            snippets.append({"id": f["id"], "text": f"{f.get('name')} :: {f.get('summary')}"})
    for table, rows in sc.tables.items():
        for row in rows:
            if can_see(row, persona_id):
                fields = ", ".join(f"{k} {v}" for k, v in row.items() if k != "acl")
                snippets.append({"id": row["id"], "text": f"{_kind_for_table(table).title()} record :: {fields}"})
    for person in sc.people:
        if can_see(person, persona_id):
            snippets.append({"id": person["id"], "text": f"{person.get('name')} :: {person.get('title')} :: {', '.join(person.get('expertise', []))}"})
    return snippets


def _retrieve(snippets: list[dict], question: str, k: int = 6) -> list[dict]:
    """Retrieve top-k snippets using cosine similarity on embeddings if available,
    falling back to term-overlap if no embedding endpoint is configured."""
    embeddings = _get_embeddings([s["text"] for s in snippets] + [question])
    if embeddings is not None:
        snippet_vecs = embeddings[:-1]
        query_vec = embeddings[-1]
        # Cosine similarity
        norms = np.linalg.norm(snippet_vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1
        snippet_normed = snippet_vecs / norms
        query_norm = query_vec / (np.linalg.norm(query_vec) or 1)
        scores = snippet_normed @ query_norm
        top_indices = np.argsort(scores)[::-1][:k]
        return [snippets[i] for i in top_indices if scores[i] > 0]

    # Fallback: term overlap
    q_terms = set(_normalize(question).split())
    scored = []
    for s in snippets:
        terms = set(_normalize(s["text"]).split())
        overlap = len(q_terms & terms)
        if overlap:
            scored.append((overlap, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:k]]


def _get_embeddings(texts: list[str]):
    """Get embeddings from Azure OpenAI. Returns numpy array or None if unavailable."""
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return None
    # Strip /openai/v1 suffix if present to get the base endpoint
    base = re.sub(r"/openai/v1$", "", endpoint)
    deploy = os.environ.get("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
    api_version = "2024-02-01"
    url = f"{base}/openai/deployments/{deploy}/embeddings?api-version={api_version}"

    try:
        import httpx
        from azure.identity import AzureCliCredential
        credential = AzureCliCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default").token
        resp = httpx.post(
            url,
            json={"input": texts},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            print(f"[engine] embedding request failed ({resp.status_code}): {resp.text[:200]}", file=sys.stderr)
            return None
        data = resp.json()
        vecs = [item["embedding"] for item in data["data"]]
        return np.array(vecs, dtype=np.float32)
    except Exception as exc:
        print(f"[engine] embedding fallback to term-overlap: {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# Public API: ask
# --------------------------------------------------------------------------- #

GOVERNANCE_NOTE = (
    "\n\n[Governance] {n} source(s) were withheld from this answer because the active "
    "persona ('{persona}') does not have access to restricted/customer-confidential "
    "material: {ids}. Switch to a leadership persona to see them."
)


def ask(sc: Scenario, question: str, persona_id: str | None = None) -> dict:
    """Answer a question. Returns {response, conversationId, citations, trimmed}."""
    conversation_id = f"sim-{uuid.uuid4().hex[:12]}"
    golden = match_golden(sc, question)

    if golden is not None:
        visible, trimmed = resolve_citations(sc, golden.get("citations", []), persona_id)
        if trimmed:
            # RBAC: do NOT return the full canned answer — its prose can contain the
            # restricted facts even though the citations were stripped. Use the authored
            # redaction ONLY when the trimmed set is fully anticipated by the golden's
            # `restricted_citations`; otherwise this persona is blocked from MORE sources
            # than the redaction was written for, so the redaction itself may narrate
            # facts it shouldn't. In that case fail closed with a generic message.
            persona = sc.get_persona(persona_id)
            label = persona["label"] if persona else (persona_id or "unscoped")
            restricted = set(golden.get("restricted_citations", []))
            authored = golden.get("trimmed_answer")
            if authored and set(trimmed) <= restricted:
                response = authored
            else:
                response = (
                    "A complete answer to this question draws on sources the active "
                    "persona is not authorized to see, and no persona-safe redaction is "
                    "available for this set of restrictions. Switch to a persona with "
                    "broader access to view it."
                )
            response += GOVERNANCE_NOTE.format(
                n=len(trimmed), persona=label, ids=", ".join(trimmed)
            )
        else:
            response = golden["answer"]
        return {
            "response": response,
            "conversationId": conversation_id,
            "citations": visible,
            "trimmed": trimmed,
            "source": "golden",
            "matched": golden["id"],
            "tool": golden.get("tool"),
        }

    # No golden match — retrieve, then optionally synthesise with a model.
    snippets = _all_snippets(sc, persona_id)
    top = _retrieve(snippets, question)
    llm = _llm_answer(question, [f"[{s['id']}] {s['text']}" for s in top])
    if llm is not None:
        cited_ids = [s["id"] for s in top]
        visible, _ = resolve_citations(sc, cited_ids, persona_id)
        return {
            "response": llm,
            "conversationId": conversation_id,
            "citations": visible,
            "trimmed": [],
            "source": "llm",
            "matched": None,
            "tool": None,
        }

    # Graceful degradation: no golden, no model.
    if top:
        bullets = "\n".join(f"- [{s['id']}] {s['text'][:140]}" for s in top)
        response = (
            "No scripted answer matched this question, and no model is configured "
            "(set OPENAI_API_KEY for ad-hoc synthesis). Closest work-context signals:\n"
            f"{bullets}"
        )
    else:
        response = (
            "No scripted answer matched and no relevant work-context signals were found "
            "for the active persona."
        )
    visible, _ = resolve_citations(sc, [s["id"] for s in top], persona_id)
    return {
        "response": response,
        "conversationId": conversation_id,
        "citations": visible,
        "trimmed": [],
        "source": "retrieval-only",
        "matched": None,
        "tool": None,
    }


# --------------------------------------------------------------------------- #
# Public API: Tools surface (fetch / create_entity / update_entity)
# --------------------------------------------------------------------------- #

def _persist_table(sc: Scenario, table: str) -> None:
    rows = sc.tables.get(table)
    if rows is None:
        return
    path = sc.root / "tables" / f"{table}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Round-trip in the file's original shape (bare list vs {stem: rows}) so persisting
    # never silently rewrites the on-disk format.
    payload: Any = rows if sc.table_formats.get(table) == "list" else {table: rows}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _default_table_acl(rows: list[dict]) -> list[str] | None:
    """ACL to apply to a new row that omits one: inherit from the first existing row that
    declares an `acl` (least-privilege — a new row on a restricted table must not default
    to world-readable). Returns None when no row declares an acl (table is public)."""
    for row in rows:
        acl = row.get("acl")
        if acl:
            return list(acl)
    return None


def _next_id(rows: list[dict], prefix: str) -> str:
    """Generate a unique id of the form PREFIX-NNN based on the max existing numeric
    suffix (not len(rows), which collides when rows were deleted or ids are sparse)."""
    max_n = 0
    existing = {row.get("id") for row in rows}
    for rid in existing:
        if isinstance(rid, str) and rid.startswith(prefix + "-"):
            try:
                max_n = max(max_n, int(rid.rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                continue
    n = max_n + 1
    while f"{prefix}-{n:03d}" in existing:
        n += 1
    return f"{prefix}-{n:03d}"


def fetch(sc: Scenario, table: str, filter: dict | None = None) -> list[dict]:
    """Read rows from a Tools-backed table, optionally filtered by exact field match."""
    rows = sc.tables.get(table)
    if rows is None:
        raise ValueError(f"Unknown table: {table}")
    if not filter:
        return list(rows)
    out = []
    for row in rows:
        if all(row.get(k) == v for k, v in filter.items()):
            out.append(row)
    return out


def create_entity(
    sc: Scenario, table: str, record: dict, persist: bool = False
) -> dict:
    """Append a row to a Tools-backed table. Idempotent on `id` and on a
    `dedupe_key` of (milestone, owner) for the milestone tracker — re-issuing the
    same logical create returns the existing row instead of duplicating."""
    rows = sc.tables.get(table)
    if rows is None:
        raise ValueError(f"Unknown table: {table}")

    record = dict(record)  # never mutate the caller's dict
    new_id = record.get("id")
    if new_id:
        existing = sc.index.get(new_id)
        if existing is not None:
            _, existing_row = existing
            if any(r is existing_row for r in rows):
                return {"created": False, "reason": "id_exists", "row": existing_row}
            # id is already used by a DIFFERENT entity (another table, an email, a
            # person...) — appending would clobber its index entry. Reject.
            return {
                "created": False,
                "reason": "id_collision",
                "detail": f"id '{new_id}' is already used by another entity",
            }

    # logical dedupe for milestone tracker
    if table == "milestone_tracker":
        m = record.get("milestone")
        o = record.get("owner")
        if m and o:
            for row in rows:
                if row.get("milestone") == m and row.get("owner") == o:
                    return {"created": False, "reason": "duplicate_milestone_owner", "row": row}

    if not new_id:
        record["id"] = _next_id(rows, _prefix_for_table(rows, table))

    # least-privilege: a new row that omits `acl` inherits the table's existing acl rather
    # than defaulting to world-readable (can_see treats missing acl as ["all"]).
    if "acl" not in record:
        inherited = _default_table_acl(rows)
        if inherited is not None:
            record["acl"] = inherited

    rows.append(record)
    sc.index[record["id"]] = (_kind_for_table(table), record)
    if persist:
        _persist_table(sc, table)
    return {"created": True, "row": record}


def update_entity(
    sc: Scenario, table: str, id: str, patch: dict, persist: bool = False
) -> dict:
    """Patch fields on an existing row by id. If the patch changes `id`, the citation
    index is atomically rekeyed so lookups stay consistent (and id collisions are
    rejected)."""
    rows = sc.tables.get(table)
    if rows is None:
        raise ValueError(f"Unknown table: {table}")
    for row in rows:
        if row.get("id") == id:
            new_id = patch.get("id", id)
            if new_id != id and new_id in sc.index:
                return {"updated": False, "reason": "id_collision"}
            row.update(patch)
            if new_id != id:
                kind = sc.index.get(id, (None,))[0]
                sc.index.pop(id, None)
                if kind is not None:
                    sc.index[new_id] = (kind, row)
            if persist:
                _persist_table(sc, table)
            return {"updated": True, "row": row}
    return {"updated": False, "reason": "not_found"}
