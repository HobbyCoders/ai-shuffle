"""SkillStore — the persistent half of the continual harness.

Skills are model-authored Python modules that survive the session. They live as
plain ``.py`` files under a skills root (default ``~/.omni/skills``), are
mounted into every code-mode kernel as a virtual ``skills`` package (imported
with ``from skills.<name> import <name>``), and are written from inside the
kernel through the host-gated ``save_skill`` tool — so persistence itself goes
through the same permission engine as any write.

Alongside the skills the store keeps:

- ``journal.jsonl`` — an append-only record of every executed cell (code,
  output preview, error, tools used). This is the raw material the
  :class:`~omni.codemode.continual.Synthesizer` distills skills from.
- ``meta.json``    — per-skill usage counts (bumped when a cell references
  ``skills.<name>``) and the synthesis cursor (how far into the journal the
  synthesizer has already looked). Usage counts exist so curation can favor
  skills that earn their keep and prune ones that never get imported.

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

DEFAULT_SKILLS_ROOT = Path.home() / ".omni" / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    source: str
    description: str  # first line of the module docstring
    uses: int = 0
    updated_at: float = 0.0


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
        return Skill(
            name=name,
            source=source,
            description=_description_of(source) or name,
            uses=int(info.get("uses", 0)),
            updated_at=float(info.get("updated_at", 0.0)),
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

    def note_uses(self, names: list[str]) -> None:
        """Bump usage counters for skills referenced by an executed cell."""
        known = {p.stem for p in self.root.glob("*.py")}
        hits = [n for n in names if n in known]
        if not hits:
            return
        meta = self._load_meta()
        skills = meta.setdefault("skills", {})
        for name in hits:
            skills.setdefault(name, {"uses": 0, "updated_at": 0.0})
            skills[name]["uses"] = int(skills[name].get("uses", 0)) + 1
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

    # ─── synthesis cursor ────────────────────────────────────────────────
    def synth_cursor(self) -> int:
        return int(self._load_meta().get("synth_cursor", 0))

    def set_synth_cursor(self, cursor: int) -> None:
        meta = self._load_meta()
        meta["synth_cursor"] = int(cursor)
        self._save_meta(meta)


# Referenced-skill extraction: ``skills.foo`` attribute use and
# ``from skills.foo import ...`` / ``import skills.foo`` forms.
_USE_RE = re.compile(
    r"(?:\bskills\.([a-z_][a-z0-9_]*))|(?:\bfrom\s+skills\.([a-z_][a-z0-9_]*)\s+import\b)"
)


def referenced_skills(code: str) -> list[str]:
    """Skill names a cell references (for usage counting), deduplicated."""
    seen: dict[str, None] = {}
    for m in _USE_RE.finditer(code or ""):
        name = m.group(1) or m.group(2)
        if name:
            seen.setdefault(name)
    return list(seen)
