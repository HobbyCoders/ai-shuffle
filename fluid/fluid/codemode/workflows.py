"""Bounded prose workflow proposals. Learning never installs or grants access.

Initial promotion is deliberately local-owner/project-only. The CLI requires
an interactive review; there is no agent-facing approval tool. Explicit user
acceptance receipts ground repeated successful work, but never grant approval.
"""
from __future__ import annotations

from collections.abc import Collection
import hashlib
import json
from typing import Any

from fluid.codemode.workflow_loss import load_loss, session_lost
from pathlib import Path
import re
import time

from fluid.codemode.transactions import file_transaction
from fluid.codemode.skills import project_key_for
from fluid.context.instructions import discover_skills, parse_frontmatter
from fluid.context.screening import looks_like_secret, screen_findings

MAX_PROPOSALS = 32  # includes rejected/promoted records; fail closed, never erase audit
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_CONTENT_CHARS = 4000
MAX_EVIDENCE_ENTRIES = 16
MAX_CONTEXT_WORKFLOWS = 2
_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _screen(text):
    if looks_like_secret(text) or screen_findings(text):
        raise ValueError("Workflow content/evidence failed secret or injection screening")
    # Learned procedures must generalize locations, not retain a worker's or
    # owner's private filesystem layout (including locations in evidence).
    if re.search(r"(?<![\w./])/(?!/)[\w.-]+(?:/[\w.-]+)*|"
                 r"[A-Za-z]:[\\/]|~[/\\]|\\\\|file://", text):
        raise ValueError("Workflow content/evidence contains an absolute/private path")


def _content(body):
    if not isinstance(body, dict) or set(body) != {"name", "description", "workflow", "evidence_sessions"}:
        raise ValueError("Workflow requires name, description, workflow and evidence_sessions only")
    name, description, workflow = (body.get(k) for k in ("name", "description", "workflow"))
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Invalid workflow name")
    if not isinstance(description, str) or not 10 <= len(description) <= 160 or "\n" in description:
        raise ValueError("Workflow needs a short single-line description")
    if not isinstance(workflow, str) or not 30 <= len(workflow) <= MAX_CONTENT_CHARS:
        raise ValueError("Workflow must be 30-4000 characters")
    # Frontmatter cannot declare permissions, hooks, commands or tool grants.
    if "!`" in workflow or re.search(r"(?mi)^---\s*$|allowed-tools:|hooks:", workflow):
        raise ValueError("Executable substitutions and capability metadata are not workflows")
    if not re.match(r"\s*1[.)]\s+", workflow) or "```" in workflow:
        raise ValueError("Workflow must be numbered prose, not executable code")
    # This is a conservative proposal filter, not a permission engine. Even an
    # approved skill confers no access; normal host tool gates still apply.
    if re.search(r"(?i)auto[- ]?approv|bypass.{0,30}(permission|approval|denial)|"
                 r"--no-verify|disable.{0,30}(safety|secret scan)|"
                 r"(ignore|circumvent).{0,30}(denial|permission|approval)", workflow):
        raise ValueError("Workflow cannot weaken tool or approval gates")
    _screen(description + "\n" + workflow)
    return f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n{workflow.strip()}\n"


def _evidence(
    entries: list[dict[str, Any]], sessions: list[str], name: str, project: str,
    *, lost: int = 0,
) -> list[dict[str, Any]]:
    if not isinstance(sessions, list) or len(sessions) != 2 or any(not isinstance(s, str) or not s for s in sessions) or len(set(sessions)) != 2:
        raise ValueError("Cite exactly two distinct successful sessions")
    for entry in entries:
        if entry.get("project") == project and entry.get("kind") == "user":
            text = entry.get("text", "").strip().lower()
            if text in (f"workflow corrected: {name}", f"workflow rejected: {name}"):
                raise ValueError("Corrected or rejected workflow cannot be promoted")
    evidence = []
    for session in sessions:
        if session_lost(lost, project, session):
            raise ValueError("Workflow session has incomplete or unverifiable journal evidence")
        rows = [e for e in entries if e.get("session") == session and e.get("project") == project]
        if not rows or len(rows) > MAX_EVIDENCE_ENTRIES:
            raise ValueError("Workflow evidence is missing or exceeds the review bound")
        if rows[0].get("kind") != "user":
            raise ValueError("Workflow episode has no opening user request (possibly reaped)")
        # Complete bounded episodes are retained, not a model-selected snippet
        # that can hide a subsequent correction. User text is never tool output.
        accepted = f"workflow accepted: {name}"
        tool_at = reply_at = None
        confirmed = False
        for i, row in enumerate(rows):
            raw = json.dumps(row, ensure_ascii=False)
            _screen(raw)
            if len(raw) > 1500 or row.get("error"):
                raise ValueError("Workflow evidence is oversized or includes failed work")
            kind = row.get("kind")
            if kind in ("tool", "cell"):
                tool_at = i
            elif kind == "assistant" and tool_at is not None:
                reply_at = i
            elif kind == "user":
                text = row.get("text", "").strip().lower()
                if text in (f"workflow corrected: {name}", f"workflow rejected: {name}"):
                    raise ValueError("Corrected or rejected workflow cannot be promoted")
                if text == accepted and reply_at is not None and tool_at < reply_at < i:
                    confirmed = True
        if not confirmed:
            raise ValueError("Each workflow session needs successful execution, completion and explicit user acceptance")
        evidence.append({"session": session, "sha256": _hash(rows), "entries": rows})
    return evidence


def repeated_evidence(
    entries: list[dict[str, Any]], current: list[dict[str, Any]], project: str,
    excluded: Collection[str] = (), *, lost: int,
) -> list[dict[str, Any]]:
    """Complete retained episodes for at most two newly accepted workflows.

    Reuse the bounded journal tail, not another persistent copy of session
    content. Reaped/forgotten or incomplete episodes fail closed. Current user
    receipts trigger the lookup; ordinary turns never replay historical text.
    """
    names = []
    for row in current:
        if row.get("kind") != "user" or row.get("project") != project:
            continue
        match = re.fullmatch(r"workflow accepted: ([a-z][a-z0-9-]{0,47})",
                             row.get("text", "").strip().lower())
        if match and match[1] not in excluded and match[1] not in names:
            names.append(match[1])
    selected = []
    for name in names[:MAX_CONTEXT_WORKFLOWS]:
        sessions = []
        for row in entries:
            if (row.get("project") == project and row.get("kind") == "user"
                    and row.get("text", "").strip().lower() == f"workflow accepted: {name}"
                    and row.get("session") and row["session"] not in sessions):
                sessions.append(row["session"])
        try:
            evidence = _evidence(entries, sessions[-2:], name, project, lost=lost)
        except ValueError:
            continue
        for episode in evidence:
            for row in episode["entries"]:
                if row not in selected:
                    selected.append(row)
    return selected


class WorkflowProposals:
    def __init__(self, store):
        self.store = store
        self.path = store.root / ".workflow-proposals.json"
        self.lock = store.root / ".workflow-proposals.lock"
        self.owner = store._facts.owner

    def _read(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("Workflow audit exceeds byte bound")
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if doc.get("owner") != self.owner:
            raise ValueError("Workflow owner mismatch")
        return doc["proposals"]

    def _write(self, proposals):
        text = json.dumps({"owner": self.owner, "proposals": proposals}, ensure_ascii=False, indent=2)
        if len(text.encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("Workflow audit capacity reached")
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.path)

    def list(self):
        with file_transaction(self.lock):
            return self._read()

    def propose(self, body, entries, project, *, cwd=None):
        if not isinstance(project, str) or not project or project == "global":
            raise ValueError("Workflow proposals require explicit project scope")
        content = _content(body)
        if cwd is not None:
            self._deduplicate(cwd, body["name"], body["description"])
        evidence = _evidence(entries, body["evidence_sessions"], body["name"], project,
                             lost=load_loss(self.store.root))
        identity = {"owner": self.owner, "project": project, "content": content}
        proposal_id = _hash(identity)
        with file_transaction(self.lock):
            rows = self._read()
            existing = next((r for r in rows if r["id"] == proposal_id), None)
            if existing:
                return existing  # rejection is sticky; no implicit resurrection
            if any(r["project"] == project and r["name"] == body["name"] for r in rows):
                raise ValueError("A proposal for this workflow already exists; review it before another revision")
            if len(rows) >= MAX_PROPOSALS:
                raise ValueError("Workflow proposal capacity reached; owner review required")
            row = {**identity, "id": proposal_id, "name": body["name"], "description": body["description"],
                   "evidence": evidence, "status": "pending", "created_at": time.time()}
            self._write(rows + [row])
            return row

    def reject(self, proposal_id):
        with file_transaction(self.lock):
            rows = self._read()
            row = next(r for r in rows if r["id"] == proposal_id)
            if row["status"] == "promoted":
                raise ValueError("Already promoted; rejection does not delete installed skills")
            row.update(status="rejected", reviewed_at=time.time())
            self._write(rows)

    @staticmethod
    def _deduplicate(cwd, name, description):
        from fluid.codemode.relevance import query_terms
        terms = set(query_terms(description))
        for skill in discover_skills(cwd, include_plugins=True):
            other = set(query_terms(skill.description))
            overlap = len(terms & other) / max(1, len(terms | other))
            if skill.name.rsplit(":", 1)[-1].lower() == name or (terms and overlap >= 0.8):
                raise ValueError("An installed skill already covers this workflow")

    def promote(self, proposal_id, cwd, *, approved_digest=None):
        """Only the local-owner review CLI calls this. No tool approval surface."""
        if self.owner != "local":
            raise ValueError("API owners require an authenticated scoped approval surface")
        if approved_digest != proposal_id:
            raise ValueError("Explicit owner approval of this exact proposal digest is required")
        with file_transaction(self.lock):
            rows = self._read()
            row = next(r for r in rows if r["id"] == proposal_id)
            if row["status"] != "pending" or row["project"] != project_key_for(cwd):
                raise ValueError("Workflow is not pending in this project")
            if _hash({k: row[k] for k in ("owner", "project", "content")}) != proposal_id:
                raise ValueError("Workflow content changed since proposal")
            # Revalidate original producer evidence, including later corrections.
            evidence = _evidence(
                self.store.journal_entries(), [e["session"] for e in row["evidence"]],
                row["name"], row["project"], lost=load_loss(self.store.root),
            )
            if evidence != row["evidence"]:
                raise ValueError("Workflow evidence changed; do not promote stale proposals")
            fields, workflow = parse_frontmatter(row["content"])
            canonical = _content({"name": row["name"], "description": row["description"],
                                  "workflow": workflow,
                                  "evidence_sessions": [e["session"] for e in evidence]})
            if (
                fields != {"name": row["name"], "description": row["description"]}
                or canonical != row["content"]
            ):
                raise ValueError("Workflow content no longer matches its validated schema")
            self._deduplicate(cwd, row["name"], row["description"])
            root = Path(cwd).resolve() / ".fluid" / "skills"
            # No symlinked scope, no overwrites (including unindexed directories).
            for parent in (root.parent, root, root / row["name"]):
                if parent.is_symlink():
                    raise ValueError("Workflow promotion cannot follow scope symlinks")
            target = root / row["name"]
            path = target / "SKILL.md"
            # Reserve audit capacity before touching discovery. If interrupted,
            # an installing record names the exact owner-approved artifact and
            # cannot be retried as another pending proposal.
            row.update(status="installing", reviewed_at=time.time(), installed_path=str(path))
            self._write(rows)
            try:
                target.mkdir(parents=True, exist_ok=False)
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(row["content"])
            except OSError:
                row["status"] = "installation-failed"
                self._write(rows)
                raise
            row["status"] = "promoted"
            self._write(rows)
            return path
