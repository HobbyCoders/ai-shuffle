"""SkillStore — the persistent half of the continual harness.

Skills are model-authored Python modules that survive the session. They live as
plain ``.py`` files under a skills root (default ``~/.fluid/skills``), are
mounted into every code-mode kernel as a virtual ``skills`` package (imported
with ``from skills import <name>``), and are written from inside the
kernel through the host-gated ``save_skill`` tool — so persistence itself goes
through the same permission engine as any write.

Alongside the skills the store keeps:

- ``journal.jsonl`` — an append-only record of every executed cell (code,
  output preview, error, tools used). This is the raw material the
  :class:`~fluid.codemode.continual.Synthesizer` distills skills from.
- ``meta.json``    — per-skill usage counts (bumped when a cell references
  ``skills.<name>``) and the synthesis cursor (how far into the journal the
  synthesizer has already looked). Usage counts exist so curation can favor
  skills that earn their keep and prune ones that never get imported.
- ``memories.json`` — the learner's declarative half: short remembered facts
  (stated preferences, environment conventions, corrections) written ONLY by
  the synthesizer and rendered into every session's prompt as a frozen
  snapshot (``render_memory_section``). No model-facing tool writes them, so
  there is no file for the agent to curate — the learner adds, rewrites, and
  retires entries itself as evidence accumulates, and the store caps the
  count, archiving the oldest instead of growing without bound.

Everything is stdlib-only and synchronous; callers are the code-mode session
(host side) and the synthesizer.
"""

from __future__ import annotations

import ast
import json
import keyword
import re
import time
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
MAX_SKILL_BYTES = 65536
MAX_MEMORIES = 40
MAX_MEMORY_CHARS = 500  # one memory is a fact, not an essay

DEFAULT_SKILLS_ROOT = Path.home() / ".fluid" / "skills"


@dataclass(frozen=True)
class MemoryEntry:
    """One remembered fact — the declarative half of the continual store."""

    id: str  # "m<n>", monotonically assigned, stable across rewrites
    text: str
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class Skill:
    name: str
    source: str
    description: str  # first line of the module docstring
    uses: int = 0
    updated_at: float = 0.0
    broken: str | None = None  # last verification failure (None = ok/unverified)


def _description_of(source: str) -> str | None:
    """First line of the module docstring, or None if there is no docstring."""
    try:
        doc = ast.get_docstring(ast.parse(source))
    except SyntaxError:
        return None
    if not doc:
        return None
    for line in doc.strip().splitlines():
        if line.strip():
            return line.strip()
    return None


def validate_skill(name: str, source: str) -> str | None:
    """Return an error message, or None when (name, source) is a valid skill."""
    if not isinstance(name, str) or not _NAME_RE.match(name) or keyword.iskeyword(name):
        return (
            f"invalid skill name {name!r}: use a short lowercase identifier "
            "(letters, digits, underscores; not a Python keyword)"
        )
    if not isinstance(source, str) or not source.strip():
        return "skill source is empty"
    if len(source.encode("utf-8", "replace")) > MAX_SKILL_BYTES:
        return f"skill source exceeds {MAX_SKILL_BYTES} bytes"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"skill source does not parse: {exc}"
    if not ast.get_docstring(tree):
        return (
            "skill module needs a docstring (first line = one-sentence "
            "description shown in list_tools)"
        )
    return None


class SkillStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_SKILLS_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._meta_path = self.root / "meta.json"
        self._journal_path = self.root / "journal.jsonl"

    # ─── meta ────────────────────────────────────────────────────────────
    def _load_meta(self) -> dict:
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_meta(self, meta: dict) -> None:
        self._meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )

    # ─── skills CRUD ─────────────────────────────────────────────────────
    def _path(self, name: str) -> Path:
        return self.root / f"{name}.py"

    def save(self, name: str, source: str) -> Skill:
        """Validate and persist one skill (raises ValueError when invalid)."""
        error = validate_skill(name, source)
        if error is not None:
            raise ValueError(error)
        if not source.endswith("\n"):
            source += "\n"
        self._path(name).write_text(source, encoding="utf-8")
        meta = self._load_meta()
        entry = meta.setdefault("skills", {}).setdefault(name, {"uses": 0})
        entry["updated_at"] = time.time()
        self._save_meta(meta)
        skill = self.get(name)
        assert skill is not None
        return skill

    def delete(self, name: str) -> bool:
        path = self._path(name) if _NAME_RE.match(name or "") else None
        existed = bool(path and path.is_file())
        if path and existed:
            path.unlink()
        meta = self._load_meta()
        if meta.get("skills", {}).pop(name, None) is not None:
            self._save_meta(meta)
        return existed

    def get(self, name: str) -> Skill | None:
        if not _NAME_RE.match(name or ""):
            return None
        path = self._path(name)
        if not path.is_file():
            return None
        source = path.read_text(encoding="utf-8")
        info = self._load_meta().get("skills", {}).get(name, {})
        broken = info.get("broken")
        # A broken verdict only holds for the exact source it was issued on.
        if broken is not None and info.get("verified_mtime") != self._mtime_ns(name):
            broken = None
        return Skill(
            name=name,
            source=source,
            description=_description_of(source) or name,
            uses=int(info.get("uses", 0)),
            updated_at=float(info.get("updated_at", 0.0)),
            broken=broken,
        )

    def list(self) -> list[Skill]:
        out = []
        for path in sorted(self.root.glob("*.py")):
            skill = self.get(path.stem)
            if skill is not None:
                out.append(skill)
        return out

    def sources(self) -> dict[str, str]:
        """``{name: source}`` for the kernel's ``skills`` VFS mount."""
        return {s.name: s.source for s in self.list()}

    def signature(self) -> tuple:
        """Change-detection key for the kernel VFS (name + mtime per skill)."""
        out = []
        for path in sorted(self.root.glob("*.py")):
            try:
                out.append((path.stem, path.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(out)

    # ─── verification bookkeeping (see fluid.codemode.verify) ─────────────
    def _mtime_ns(self, name: str) -> int | None:
        try:
            return self._path(name).stat().st_mtime_ns
        except OSError:
            return None

    def needs_verification(self, name: str) -> bool:
        """True when the skill was never verified for its current source."""
        info = self._load_meta().get("skills", {}).get(name, {})
        mtime = self._mtime_ns(name)
        return mtime is None or info.get("verified_mtime") != mtime

    def mark_verified(self, name: str, error: str | None = None) -> None:
        """Record a verification verdict for the skill's current source."""
        meta = self._load_meta()
        entry = meta.setdefault("skills", {}).setdefault(name, {"uses": 0})
        entry["verified_mtime"] = self._mtime_ns(name)
        if error is None:
            entry.pop("broken", None)
        else:
            entry["broken"] = error
        self._save_meta(meta)

    def note_uses(self, names: list[str]) -> None:
        """Bump usage counters for skills referenced by an executed cell."""
        known = {p.stem for p in self.root.glob("*.py")}
        hits = [n for n in names if n in known]
        if not hits:
            return
        meta = self._load_meta()
        skills = meta.setdefault("skills", {})
        now = time.time()
        for name in hits:
            skills.setdefault(name, {"uses": 0, "updated_at": 0.0})
            skills[name]["uses"] = int(skills[name].get("uses", 0)) + 1
            skills[name]["last_used_at"] = now
        self._save_meta(meta)

    # ─── journal ─────────────────────────────────────────────────────────
    def journal_append(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        with self._journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def journal_count(self) -> int:
        try:
            with self._journal_path.open("r", encoding="utf-8") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    def journal_entries(self, start: int = 0, limit: int | None = None) -> list[dict]:
        """Entries [start, start+limit) by line index; malformed lines skipped."""
        out: list[dict] = []
        try:
            with self._journal_path.open("r", encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i < start:
                        continue
                    if limit is not None and len(out) >= limit:
                        break
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    # ─── memories (the declarative half — synthesizer-written only) ──────
    @property
    def memories_path(self) -> Path:
        return self.root / "memories.json"

    def _load_memories(self) -> dict:
        try:
            doc = json.loads(self.memories_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"next_id": 1, "entries": []}
        if not isinstance(doc, dict):
            return {"next_id": 1, "entries": []}
        doc.setdefault("next_id", 1)
        entries = doc.get("entries")
        doc["entries"] = entries if isinstance(entries, list) else []
        return doc

    def _save_memories(self, doc: dict) -> None:
        self.memories_path.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def memories(self) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for e in self._load_memories()["entries"]:
            if isinstance(e, dict) and e.get("id") and str(e.get("text", "")).strip():
                out.append(MemoryEntry(
                    id=str(e["id"]),
                    text=str(e["text"]).strip(),
                    created_at=float(e.get("created_at", 0.0)),
                    updated_at=float(e.get("updated_at", 0.0)),
                ))
        return out

    def save_memory(self, text: str, memory_id: str | None = None) -> MemoryEntry:
        """Add (``memory_id=None``) or rewrite one remembered fact.

        Raises ``ValueError`` for empty/oversized text and for anything that
        looks like a credential — memories land in every future session's
        prompt, so a secret here would leak forever.
        """
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("memory text is empty")
        if len(text) > MAX_MEMORY_CHARS:
            raise ValueError(f"memory text exceeds {MAX_MEMORY_CHARS} chars")
        from fluid.context.screening import looks_like_secret

        secret_kind = looks_like_secret(text)
        if secret_kind is not None:
            raise ValueError(
                f"memory looks like it contains a credential ({secret_kind}); "
                "store where the credential lives, never its value"
            )
        doc = self._load_memories()
        now = time.time()
        if memory_id is not None:
            for e in doc["entries"]:
                if isinstance(e, dict) and e.get("id") == memory_id:
                    e["text"] = text
                    e["updated_at"] = now
                    self._save_memories(doc)
                    return MemoryEntry(
                        id=str(memory_id), text=text,
                        created_at=float(e.get("created_at", now)), updated_at=now,
                    )
        entry = {
            "id": f"m{int(doc.get('next_id', 1))}", "text": text,
            "created_at": now, "updated_at": now,
        }
        doc["next_id"] = int(doc.get("next_id", 1)) + 1
        doc["entries"].append(entry)
        # Bounded: past the cap the least-recently-touched entries move to the
        # archive (never deleted), the same posture as skill curation.
        if len(doc["entries"]) > MAX_MEMORIES:
            doc["entries"].sort(
                key=lambda e: float(e.get("updated_at", 0.0)) if isinstance(e, dict) else 0.0
            )
            overflow = doc["entries"][: len(doc["entries"]) - MAX_MEMORIES]
            doc["entries"] = doc["entries"][len(overflow):]
            try:
                archive = self.root / ".archive"
                archive.mkdir(exist_ok=True)
                with (archive / "memories-archive.jsonl").open(
                    "a", encoding="utf-8"
                ) as fh:
                    for old in overflow:
                        fh.write(json.dumps({"archived_at": now, **old},
                                            ensure_ascii=False) + "\n")
            except OSError:
                pass
        self._save_memories(doc)
        return MemoryEntry(id=str(entry["id"]), text=text, created_at=now, updated_at=now)

    def delete_memory(self, memory_id: str) -> bool:
        doc = self._load_memories()
        before = len(doc["entries"])
        doc["entries"] = [
            e for e in doc["entries"]
            if not (isinstance(e, dict) and e.get("id") == memory_id)
        ]
        if len(doc["entries"]) == before:
            return False
        self._save_memories(doc)
        return True

    def render_memory_section(self) -> str | None:
        """The compiled prompt's ``# Memory`` section (frozen snapshot).

        ``None`` when there are no memories, so empty installs pay nothing.
        Entries pass the injection screen; a finding replaces that entry with
        a note naming the file, never silently deleting it.
        """
        entries = self.memories()
        if not entries:
            return None
        from fluid.context.screening import screen_findings

        shown: list[str] = []
        for entry in entries:
            findings = screen_findings(entry.text)
            if findings:
                shown.append(
                    f"- [an entry was withheld — suspected injection "
                    f"pattern(s): {', '.join(findings)}. It is still in "
                    f"{self.memories_path}.]"
                )
            else:
                shown.append(f"- {entry.text}")
        return (
            "# Memory\n"
            "Facts your continual learner distilled from earlier sessions. "
            "Trust them, and correct course when the user contradicts one — "
            "the learner curates this list itself from what happens in "
            "sessions; you never manage it.\n\n"
            + "\n".join(shown)
        )

    # ─── lifecycle curation ──────────────────────────────────────────────
    #
    # Deterministic, no LLM, and touching ONLY store-managed skills (the ones
    # with a meta entry — synthesized or saved through save_skill). Archive is
    # the maximum action: the file moves to ``.archive/`` and comes back by
    # moving it back; nothing is ever deleted. Pinned skills
    # (``meta["skills"][name]["pinned"] = true``) are exempt from every
    # transition. Interval-gated so per-engine construction stays cheap.

    CURATE_INTERVAL_DAYS = 7.0
    STALE_AFTER_DAYS = 30.0
    ARCHIVE_AFTER_DAYS = 90.0

    def curate(self, *, now: float | None = None, force: bool = False) -> list[str]:
        """Run one lifecycle pass when due. Returns action lines (may be [])."""
        now = time.time() if now is None else now
        meta = self._load_meta()
        last = float(meta.get("last_curated_at", 0.0))
        if not force and now - last < self.CURATE_INTERVAL_DAYS * 86_400:
            return []
        meta["last_curated_at"] = now
        actions: list[str] = []
        archive_dir = self.root / ".archive"
        for name, info in sorted((meta.get("skills") or {}).items()):
            if not isinstance(info, dict) or info.get("pinned"):
                continue
            path = self._path(name)
            if not path.is_file():
                continue
            last_activity = max(
                float(info.get("last_used_at", 0.0)),
                float(info.get("updated_at", 0.0)),
            )
            if last_activity <= 0:
                continue  # no evidence either way — leave it alone
            idle_days = (now - last_activity) / 86_400
            if idle_days > self.ARCHIVE_AFTER_DAYS:
                try:
                    archive_dir.mkdir(exist_ok=True)
                    path.rename(archive_dir / path.name)
                except OSError:
                    continue
                info["state"] = "archived"
                actions.append(
                    f"archived {name} (unused {idle_days:.0f}d) -> "
                    f"{archive_dir / path.name} — move it back to restore"
                )
            elif idle_days > self.STALE_AFTER_DAYS:
                if info.get("state") != "stale":
                    info["state"] = "stale"
                    actions.append(f"marked {name} stale (unused {idle_days:.0f}d)")
            elif info.get("state") == "stale":
                info.pop("state", None)
                actions.append(f"{name} active again")
        self._save_meta(meta)
        if actions:
            try:
                with (self.root / ".curation-log.jsonl").open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(json.dumps({"ts": now, "actions": actions}) + "\n")
            except OSError:
                pass
        return actions

    # ─── synthesis cursor ────────────────────────────────────────────────
    def synth_cursor(self) -> int:
        return int(self._load_meta().get("synth_cursor", 0))

    def set_synth_cursor(self, cursor: int) -> None:
        meta = self._load_meta()
        meta["synth_cursor"] = int(cursor)
        self._save_meta(meta)


# Referenced-skill extraction: ``skills.foo`` attribute use, the
# ``from skills.foo import ...`` / ``import skills.foo`` forms, and the package
# form ``from skills import foo, bar``.
_USE_RE = re.compile(
    r"(?:\bskills\.([a-z_][a-z0-9_]*))"
    r"|(?:\bfrom\s+skills\.([a-z_][a-z0-9_]*)\s+import\b)"
    r"|(?:\bfrom\s+skills\s+import\s+(?:\(([^)]*)\)|([^\n]+)))"
)
_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*")


def referenced_skills(code: str) -> list[str]:
    """Skill names a cell references (for usage counting), deduplicated."""
    seen: dict[str, None] = {}
    for m in _USE_RE.finditer(code or ""):
        name = m.group(1) or m.group(2)
        if name:
            seen.setdefault(name)
        elif m.group(3) or m.group(4):
            # ``from skills import a, b as c`` — the imported names, not aliases.
            for item in (m.group(3) or m.group(4)).split(","):
                head = _NAME_RE.match(item.strip())
                if head:
                    seen.setdefault(head.group(0))
    return list(seen)
