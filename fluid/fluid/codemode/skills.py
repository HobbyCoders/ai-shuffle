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
  (stated preferences, environment conventions, corrections) rendered into
  every session's prompt as a frozen snapshot (``render_memory_section``).
  Only the synthesizer writes the file itself; the model *proposes* through
  the ``Remember`` builtin, which journals a high-salience entry the
  synthesizer consolidates on its next pass (propose-then-curate: the model
  flags what seemed durable while context was hot, the learner with global
  view decides what lands). The store caps the count, archiving the least
  recalled instead of growing without bound; the ``Recall`` builtin searches
  live and archived entries and feeds the usage counters that drive that
  eviction.

Every one of those files is BOUNDED and the harness owns the cleanup. The
model has no reaping control at all — its only write path stays the
propose-only ``Remember`` flow. Reaping is deterministic, best-effort (a
failure never breaks journaling or synthesis) and rides machinery that
already exists: :meth:`SkillStore.reap`, called from the interval-gated
lifecycle pass (:meth:`SkillStore.curate`) and from the synthesizer's
consolidation pass. The caps live in module constants next to
``MAX_MEMORIES`` so every bound is visible in one place.

Everything is stdlib-only and synchronous; callers are the code-mode session
(host side) and the synthesizer.

Concurrency posture. Within one process journaling and reaping are
synchronous and the synthesizer's background pass runs on the same event
loop, so they cannot interleave. Across processes sharing a skills root:

- Every JSON document rewrite (``meta.json``, ``memories.json``,
  ``anchors.json``) and every log trim goes through a temp file in the same
  directory plus ``os.replace`` (:func:`atomic_write_text`), so a crash or a
  lost race leaves the previous complete file, never a truncated one. A
  document that fails to parse on load is renamed aside as
  ``<name>.corrupt-<timestamp>`` and logged, then the store starts from
  empty; it is never overwritten in place.
- Journal reaping holds an advisory lock file (``journal.jsonl.lock``) while
  it reads and replaces the journal, and appenders take the same lock for
  the duration of their single write. A reap that finds the lock held skips
  and tries again next pass. As a backstop against a writer that does not
  take the lock (an older build), reap re-checks the journal's size right
  before the replace and aborts if anything landed in between. Residual
  race: an appender that gave up waiting for the lock and wrote anyway,
  after the size check and before the replace, loses that line.
- ``meta.json`` counters (usage, cursor) are last-writer-wins between
  processes; the write itself is still atomic.
"""

from __future__ import annotations

import ast
import json
import keyword
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fluid._filelock import file_lock

_log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
MAX_SKILL_BYTES = 65536
# Live skills past this many are archived, least useful first (the memory cap
# has the same posture): a library the synthesizer must re-read every pass
# cannot grow without bound.
MAX_LIVE_SKILLS = 40
MAX_MEMORIES = 40
MAX_MEMORY_CHARS = 500  # one memory is a fact, not an essay

# ─── storage bounds (the harness reaps; the model never does) ────────────────
#
# journal.jsonl: entries the synthesizer has already consumed have no reader
# left, so they are droppable — but a recent tail is what makes a store
# debuggable after the fact, so a prefix is only dropped once the file grows
# past the tail budget. Both caps are ceilings, not targets: a quiet store
# never reaches either and is never rewritten.
JOURNAL_KEEP_ENTRIES = 500  # recent lines kept even though they are consumed
JOURNAL_MAX_BYTES = 1_048_576  # past this, every consumed line goes
# memories-archive.jsonl: overflow past MAX_MEMORIES lands here and Recall
# searches it, so it is real storage, not a graveyard — but it is capped.
MAX_ARCHIVED_MEMORIES = 200
# .archive/*.py: skills curation archives rather than deletes; the oldest
# archived modules are dropped for good past this count.
MAX_ARCHIVED_SKILLS = 100
# .curation-log.jsonl / .synthesis-log.jsonl: audit trails, newest wins.
MAX_LOG_ENTRIES = 500
MAX_LOG_BYTES = 262_144  # only look at a log once it grows past this

DEFAULT_SKILLS_ROOT = Path.home() / ".fluid" / "skills"


def trim_jsonl(path: Path, keep: int) -> int:
    """Keep the newest ``keep`` lines of an append-only log file.

    Returns the number of lines dropped (0 when the file is absent, short
    enough, or unreadable). The rewrite is atomic — temp file plus
    ``os.replace`` — so a crash mid-trim leaves the original intact.
    """
    keep = max(0, int(keep))
    try:
        with path.open("r", encoding="utf-8") as fh:
            total = sum(1 for _ in fh)
    except OSError:
        return 0
    if total <= keep:
        return 0
    drop = total - keep
    # Two streaming passes rather than one slurp: the first trim of a file
    # that grew unbounded before this cap existed must not need the whole
    # thing in memory.
    return drop if _rewrite_dropping_prefix(path, drop) else 0


def _tmp_path(path: Path) -> Path:
    """Sibling temp file for an atomic rewrite of ``path``.

    Same directory, so ``os.replace`` is a rename and never a copy; the pid
    keeps two processes rewriting the same file from clobbering each other's
    temp.
    """
    return path.with_name(f"{path.name}.tmp-{os.getpid()}")


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so a reader only ever sees a complete file.

    Write to a sibling temp file, flush and fsync it, then ``os.replace``
    over the target. A crash at any point leaves either the old file or the
    new one, never a truncated mix. Raises ``OSError`` when the write or the
    replace fails; the temp file is removed first.
    """
    tmp = _tmp_path(path)
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        _discard(tmp)
        raise


def _set_aside_corrupt(path: Path) -> Path | None:
    """Rename a document that failed to parse to ``<name>.corrupt-<timestamp>``.

    Returns the new path, or None when the rename itself failed. The file is
    kept so the bytes can be inspected and possibly recovered by hand; the
    next save writes a fresh document beside it instead of over it.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    try:
        os.replace(path, aside)
    except OSError:
        return None
    return aside


def load_json_document(path: Path, default: dict) -> dict:
    """Parse a JSON object at ``path``, or return a copy of ``default``.

    An absent file is the normal empty case. A file that exists but does not
    parse, or parses to something other than an object, is set aside with a
    warning rather than silently treated as empty — a truncated
    ``memories.json`` used to make every memory vanish and the next save
    overwrite the evidence.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return dict(default)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        aside = _set_aside_corrupt(path)
        _log.warning(
            "%s does not parse (%s); moved aside to %s and starting from empty",
            path, exc, aside.name if aside is not None else "<rename failed>",
        )
        return dict(default)
    if not isinstance(doc, dict):
        aside = _set_aside_corrupt(path)
        _log.warning(
            "%s holds a %s, not an object; moved aside to %s and starting from empty",
            path, type(doc).__name__,
            aside.name if aside is not None else "<rename failed>",
        )
        return dict(default)
    return doc


def _rewrite_dropping_prefix(
    path: Path, drop: int, *, expect_size: int | None = None
) -> bool:
    """Atomically rewrite ``path`` without its first ``drop`` lines.

    With ``expect_size`` the rewrite is abandoned (returns False, file
    untouched) when the file's size differs from it right before the
    replace: something appended after the caller measured it, and replacing
    would drop that line.
    """
    tmp = _tmp_path(path)
    try:
        with path.open("r", encoding="utf-8") as src, \
                tmp.open("w", encoding="utf-8") as dst:
            for i, line in enumerate(src):
                if i >= drop:
                    dst.write(line)
            dst.flush()
            os.fsync(dst.fileno())
        if expect_size is not None and path.stat().st_size != expect_size:
            _discard(tmp)
            return False
        os.replace(tmp, path)
    except OSError:
        _discard(tmp)
        return False
    return True


# ─── journal lock ────────────────────────────────────────────────────────────
#
# Advisory, cross-process, on a sibling ``.lock`` file rather than the journal
# itself: the journal is replaced by rename during a reap, and a lock on the
# old inode would guard nothing once the new file is in place.


@contextmanager
def journal_lock(path: Path, *, wait: float = 0.0) -> Iterator[bool]:
    """Hold the advisory lock for ``path`` (the journal) inside the block.

    Yields True when the lock was taken, False when another holder kept it
    for the whole ``wait`` window (polled at 10ms) or the lock file could not
    be opened at all. The caller decides what False means: reap skips,
    append goes ahead anyway rather than losing the entry.
    """
    with file_lock(path.with_name(path.name + ".lock"), wait=wait) as held:
        yield held


# How long an appender waits for a reap in another process to finish before
# writing anyway. A reap of a 1 MiB journal takes milliseconds; this only
# bites when the holder is stuck.
JOURNAL_APPEND_LOCK_WAIT = 1.0


@dataclass(frozen=True)
class MemoryEntry:
    """One remembered fact — the declarative half of the continual store."""

    id: str  # "m<n>", monotonically assigned, stable across rewrites
    text: str
    created_at: float = 0.0
    updated_at: float = 0.0
    # Retrieval bookkeeping (the Recall tool bumps these): entries that earn
    # recalls are protected from eviction; entries nothing ever asks for are
    # the first to age out. Mirrors the skill store's ``uses`` counter.
    uses: int = 0
    last_recalled_at: float = 0.0
    # "global" (default) or a project key (see :func:`project_key_for`).
    # Project-scoped entries render only into that project's sessions, so one
    # repo's conventions stop riding into every other repo on the machine.
    scope: str = "global"
    # Where the evidence for this fact came from: ``user`` (a user prompt or a
    # Remember entry said it), ``tool`` (only tool output or cell output said
    # it), ``session`` (neither clearly; the default and what older stores
    # carry), ``manual``. A ``tool`` entry is kept but held out of the prompt:
    # file and web content the agent read is not a source the next session
    # should be told to trust.
    provenance: str = "session"


def project_key_for(cwd: "str | Path | os.PathLike[str] | None") -> str:
    """A stable per-project key for memory scoping.

    The project root is the nearest ancestor with a ``.git`` entry (worktrees
    carry a ``.git`` *file*, which counts), falling back to the directory
    itself. The key is the resolved root path with separators flattened, the
    same shape other tools use for per-project directories.
    """
    path = Path(cwd) if cwd else Path.cwd()
    try:
        path = path.resolve()
    except OSError:
        pass
    root = path
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    return re.sub(r"[^A-Za-z0-9]+", "-", str(root)).strip("-")


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
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or keyword.iskeyword(name):
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
        # O(1) journal length: the count is cached against the file size it
        # was measured at. Any size we did not cause (another process, an
        # external edit) invalidates it and the next count rescans, so the
        # cache is self-healing rather than authoritative. -1 = unknown.
        self._journal_lines = 0
        self._journal_bytes = -1
        # Stamped onto every journal entry (see journal_context).
        self._journal_tag: dict[str, str] = {}

    def journal_context(
        self, *, session_id: str | None = None, agent: str | None = None,
    ) -> None:
        """Stamp the entries this store journals with their session and, for
        a subagent, the agent name. Several engines share one journal (a
        parent and its workers, two shells on one machine); the stamps let
        the synthesizer read the braid one session at a time instead of as a
        single narrative."""
        self._journal_tag = {}
        if session_id:
            self._journal_tag["session"] = session_id
        if agent:
            self._journal_tag["agent"] = agent

    # ─── meta ────────────────────────────────────────────────────────────
    def _load_meta(self) -> dict:
        return load_json_document(self._meta_path, {})

    def _save_meta(self, meta: dict) -> None:
        atomic_write_text(
            self._meta_path, json.dumps(meta, indent=2, sort_keys=True)
        )

    # ─── skills CRUD ─────────────────────────────────────────────────────
    def _path(self, name: str) -> Path:
        # The name becomes a file name under the store root, so it must be a
        # single bare identifier: no separators, no "..", no drive prefix.
        if (
            not isinstance(name, str)
            or not _NAME_RE.fullmatch(name)
            or Path(name).parts != (name,)
        ):
            raise ValueError(f"invalid skill name {name!r}")
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
        self._enforce_live_cap(meta, keep=name)
        self._save_meta(meta)
        skill = self.get(name)
        assert skill is not None
        return skill

    def _enforce_live_cap(self, meta: dict, *, keep: str) -> list[str]:
        """Archive the least useful unpinned skills once the live set is over
        ``MAX_LIVE_SKILLS``: fewest uses first, oldest activity as the
        tiebreak, the skill just saved and pinned skills never. Archived, not
        deleted, like curation. Returns the names moved."""
        live = [p.stem for p in self.root.glob("*.py")]
        if len(live) <= MAX_LIVE_SKILLS:
            return []
        skills_meta = meta.setdefault("skills", {})

        def rank(name: str) -> tuple[int, float]:
            info = skills_meta.get(name) or {}
            return (
                int(info.get("uses", 0)),
                max(float(info.get("last_used_at", 0.0)), float(info.get("updated_at", 0.0))),
            )

        candidates = sorted(
            (
                n for n in live
                if n != keep and not (skills_meta.get(n) or {}).get("pinned")
            ),
            key=rank,
        )
        archived: list[str] = []
        for name in candidates[: len(live) - MAX_LIVE_SKILLS]:
            try:
                self.archive_dir.mkdir(exist_ok=True)
                self._path(name).replace(self.archive_dir / f"{name}.py")
            except (OSError, ValueError):
                continue
            skills_meta.setdefault(name, {})["state"] = "archived"
            archived.append(name)
        return archived

    def delete(self, name: str) -> bool:
        path = self._path(name) if _NAME_RE.fullmatch(name or "") else None
        existed = bool(path and path.is_file())
        if path and existed:
            path.unlink()
        meta = self._load_meta()
        if meta.get("skills", {}).pop(name, None) is not None:
            self._save_meta(meta)
        return existed

    def get(self, name: str) -> Skill | None:
        if not _NAME_RE.fullmatch(name or ""):
            return None
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_SKILL_BYTES:
            # A file written by hand (or by an older fluid) never passed
            # validation; treat it like a failed verdict rather than read
            # and mount it whole.
            return Skill(
                name=name, source="", description=name,
                broken=f"source is {size} bytes, over the {MAX_SKILL_BYTES}-byte cap",
            )
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
        return {s.name: s.source for s in self.list() if s.source}

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
        except (OSError, ValueError):
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
    def _journal_size(self) -> int:
        try:
            return self._journal_path.stat().st_size
        except OSError:
            return -1

    def journal_append(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        for key, value in self._journal_tag.items():
            entry.setdefault(key, value)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # One write under the journal lock: a reap in another process cannot
        # slip its replace between our size check and our line. If the lock
        # never frees, the entry is written anyway — losing it is worse than
        # the residual race documented in the module docstring.
        with journal_lock(self._journal_path, wait=JOURNAL_APPEND_LOCK_WAIT):
            before = self._journal_size()
            with self._journal_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            after = self._journal_size()
        # Only carry the cached count forward when the file was exactly where
        # we last left it; otherwise someone else wrote and the count is a
        # guess, so force a rescan instead of guessing.
        if self._journal_bytes >= 0 and before == self._journal_bytes and after >= 0:
            self._journal_lines += 1
            self._journal_bytes = after
        else:
            self._journal_bytes = -1

    def journal_count(self) -> int:
        """Number of journal lines — O(1) unless the cache went stale."""
        size = self._journal_size()
        if size < 0:
            self._journal_lines, self._journal_bytes = 0, -1
            return 0
        if size == self._journal_bytes:
            return self._journal_lines
        count = 0
        try:
            with self._journal_path.open("r", encoding="utf-8") as fh:
                count = sum(1 for _ in fh)
        except OSError:
            self._journal_bytes = -1
            return 0
        self._journal_lines, self._journal_bytes = count, size
        return count

    def reap_journal(self) -> int:
        """Drop consumed journal lines past the retention tail.

        Only lines *behind* the synthesis cursor are droppable — nothing the
        synthesizer has yet to read is ever removed. Beyond that the tail
        budget (``JOURNAL_KEEP_ENTRIES`` lines, or nothing once the file is
        over ``JOURNAL_MAX_BYTES``) decides how much of the consumed prefix
        survives. The cursor is rebased by exactly the number of lines
        removed, so ``journal_entries(start=synth_cursor())`` keeps meaning
        the same slice. Returns the number of lines dropped; every failure
        path returns 0 and leaves the journal untouched.

        The read-and-replace runs under the journal lock so a cooperating
        appender in another process waits instead of writing into the file
        about to be replaced; a reap that finds the lock already held skips
        this pass. The size measured under the lock is re-checked right
        before the replace, so a line from a writer that ignores the lock
        aborts the reap instead of vanishing.
        """
        with journal_lock(self._journal_path) as held:
            if not held:
                return 0
            size = self._journal_size()
            if size < 0:
                return 0
            cursor = max(0, self.synth_cursor())
            total = self.journal_count()
            drop = min(cursor, total)
            if size <= JOURNAL_MAX_BYTES:
                drop = min(drop, max(0, total - JOURNAL_KEEP_ENTRIES))
            if drop <= 0 or not _rewrite_dropping_prefix(
                self._journal_path, drop, expect_size=size
            ):
                return 0
            meta = self._load_meta()
            meta["synth_cursor"] = max(0, int(meta.get("synth_cursor", 0)) - drop)
            self._save_meta(meta)
            self._journal_lines = max(0, total - drop)
            self._journal_bytes = self._journal_size()
            return drop

    def reap(self) -> dict[str, int]:
        """Bound every append-only file the store owns. Never raises.

        The single cleanup entry point, called from the interval-gated
        lifecycle pass and from the synthesizer's consolidation pass — there
        is no background thread and no model-facing control. Returns
        ``{what: lines_dropped}`` for logging.
        """
        dropped: dict[str, int] = {}
        for what, fn in (
            ("journal", self.reap_journal),
            ("memory_archive", lambda: trim_jsonl(
                self.archive_dir / "memories-archive.jsonl", MAX_ARCHIVED_MEMORIES,
            )),
            ("curation_log", lambda: trim_jsonl(
                self.root / ".curation-log.jsonl", MAX_LOG_ENTRIES,
            )),
            # The evolution gate's files live in the same root: parked
            # proposals carry the full content and evidence of every failed
            # gate, so unbounded they outgrow everything else here.
            ("parked", lambda: trim_jsonl(
                self.root / "parked.jsonl", MAX_LOG_ENTRIES,
            )),
            ("evolution_log", lambda: trim_jsonl(
                self.root / ".evolution-log.jsonl", MAX_LOG_ENTRIES,
            )),
            ("archived_skills", self._reap_archived_skills),
        ):
            try:
                n = fn()
            except Exception:
                n = 0
            if n:
                dropped[what] = n
        return dropped

    def _reap_archived_skills(self) -> int:
        """Delete the oldest archived skill modules past the cap.

        Archiving is the maximum action curation takes, but the archive
        itself cannot grow forever; past ``MAX_ARCHIVED_SKILLS`` the
        least-recently-touched modules go for good, and their meta entries
        with them.
        """
        try:
            files = sorted(
                self.archive_dir.glob("*.py"), key=lambda p: p.stat().st_mtime
            )
        except OSError:
            return 0
        if len(files) <= MAX_ARCHIVED_SKILLS:
            return 0
        doomed = files[: len(files) - MAX_ARCHIVED_SKILLS]
        meta = self._load_meta()
        skills = meta.get("skills") or {}
        removed = 0
        for path in doomed:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            info = skills.get(path.stem)
            if isinstance(info, dict) and info.get("state") == "archived":
                skills.pop(path.stem, None)
        if removed:
            self._save_meta(meta)
        return removed

    def journal_entries(self, start: int = 0, limit: int | None = None) -> list[dict]:
        """Entries [start, start+limit) by line index; malformed lines skipped."""
        return self.journal_slice(start, limit)[0]

    def journal_slice(
        self, start: int = 0, limit: int | None = None
    ) -> tuple[list[dict], int]:
        """Up to ``limit`` entries from line ``start``, and the line index reached.

        Malformed lines are skipped but still consumed: a cursor advanced by
        the number of entries returned would fall short of the lines read
        and hand the next pass a slice it had already synthesized.
        """
        out: list[dict] = []
        reached = start
        try:
            with self._journal_path.open("r", encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i < start:
                        continue
                    if limit is not None and len(out) >= limit:
                        break
                    reached = i + 1
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out, reached

    # ─── memories (the declarative half — synthesizer-written only) ──────
    @property
    def archive_dir(self) -> Path:
        """Where evicted memories and archived skill modules live."""
        return self.root / ".archive"

    @property
    def memories_path(self) -> Path:
        return self.root / "memories.json"

    def _load_memories(self) -> dict:
        doc = load_json_document(self.memories_path, {"next_id": 1, "entries": []})
        doc.setdefault("next_id", 1)
        entries = doc.get("entries")
        doc["entries"] = entries if isinstance(entries, list) else []
        return doc

    def _save_memories(self, doc: dict) -> None:
        atomic_write_text(
            self.memories_path, json.dumps(doc, indent=2, ensure_ascii=False)
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
                    uses=int(e.get("uses", 0)),
                    last_recalled_at=float(e.get("last_recalled_at", 0.0)),
                    scope=str(e.get("scope") or "global"),
                    provenance=str(e.get("provenance") or "session"),
                ))
        return out

    def held_memories(self) -> list[MemoryEntry]:
        """Entries whose only evidence was tool output: stored, never
        rendered into the prompt until a user statement or a Remember
        rewrites them with better provenance."""
        return [m for m in self.memories() if m.provenance == "tool"]

    def save_memory(
        self,
        text: str,
        memory_id: str | None = None,
        scope: str | None = None,
        provenance: str | None = None,
    ) -> MemoryEntry:
        """Add (``memory_id=None``) or rewrite one remembered fact.

        ``provenance`` records where the evidence came from (see
        :class:`MemoryEntry`); a rewrite without one keeps the entry's
        existing provenance, a new entry without one is ``session``.

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
                    if scope is not None:
                        e["scope"] = scope
                    if provenance is not None:
                        e["provenance"] = provenance
                    self._save_memories(doc)
                    return MemoryEntry(
                        id=str(memory_id), text=text,
                        created_at=float(e.get("created_at", now)), updated_at=now,
                        uses=int(e.get("uses", 0)),
                        last_recalled_at=float(e.get("last_recalled_at", 0.0)),
                        scope=str(e.get("scope") or "global"),
                        provenance=str(e.get("provenance") or "session"),
                    )
        entry = {
            "id": f"m{int(doc.get('next_id', 1))}", "text": text,
            "created_at": now, "updated_at": now,
            "scope": scope or "global",
            "provenance": provenance or "session",
        }
        doc["next_id"] = int(doc.get("next_id", 1)) + 1
        doc["entries"].append(entry)
        # Bounded: past the cap the least-useful entries move to the archive
        # (never deleted), the same posture as skill curation. "Least useful"
        # is LFU-informed: fewest recalls first, oldest activity as the
        # tiebreak — an entry retrieval keeps asking for is protected even
        # when it is old.
        if len(doc["entries"]) > MAX_MEMORIES:
            ranked = sorted(
                doc["entries"],
                key=lambda e: (
                    int(e.get("uses", 0)),
                    max(
                        float(e.get("updated_at", 0.0)),
                        float(e.get("last_recalled_at", 0.0)),
                    ),
                ) if isinstance(e, dict) else (0, 0.0)
            )
            overflow = ranked[: len(ranked) - MAX_MEMORIES]
            evicted = {id(e) for e in overflow}
            # The survivors keep their file order: the ranking picks what
            # leaves, and reordering the rest would churn the prompt (and
            # its cache prefix) on every eviction.
            doc["entries"] = [e for e in doc["entries"] if id(e) not in evicted]
            archive_path = self.archive_dir / "memories-archive.jsonl"
            try:
                self.archive_dir.mkdir(exist_ok=True)
                with archive_path.open("a", encoding="utf-8") as fh:
                    for old in overflow:
                        fh.write(json.dumps({"archived_at": now, **old},
                                            ensure_ascii=False) + "\n")
            except OSError:
                pass
            # The archive is bounded too: consolidation trims it, but a store
            # that overflows fast should not wait a week for that pass, so
            # cap it here as well once the file is worth reading.
            try:
                if archive_path.stat().st_size > MAX_LOG_BYTES:
                    trim_jsonl(archive_path, MAX_ARCHIVED_MEMORIES)
            except OSError:
                pass
        self._save_memories(doc)
        return MemoryEntry(
            id=str(entry["id"]), text=text, created_at=now, updated_at=now,
            scope=str(entry["scope"]), provenance=str(entry["provenance"]),
        )

    def note_memories_recalled(self, memory_ids: list[str]) -> None:
        """Bump recall counters for entries a retrieval surfaced (LFU input)."""
        wanted = {str(i) for i in memory_ids if i}
        if not wanted:
            return
        doc = self._load_memories()
        now = time.time()
        hit = False
        for e in doc["entries"]:
            if isinstance(e, dict) and str(e.get("id")) in wanted:
                e["uses"] = int(e.get("uses", 0)) + 1
                e["last_recalled_at"] = now
                hit = True
        if hit:
            self._save_memories(doc)

    def archived_memories(self) -> list[MemoryEntry]:
        """Entries evicted past the cap — still searchable, oldest-first.

        Bounded at ``MAX_ARCHIVED_MEMORIES``: the archive is a second tier of
        memory, not an eternal log, so entries that fall out of it are gone.
        """
        out: list[MemoryEntry] = []
        path = self.archive_dir / "memories-archive.jsonl"
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("id") and str(e.get("text", "")).strip():
                        out.append(MemoryEntry(
                            id=str(e["id"]),
                            text=str(e["text"]).strip(),
                            created_at=float(e.get("created_at", 0.0)),
                            updated_at=float(e.get("updated_at", 0.0)),
                            uses=int(e.get("uses", 0)),
                            last_recalled_at=float(e.get("last_recalled_at", 0.0)),
                            scope=str(e.get("scope") or "global"),
                        ))
        except OSError:
            pass
        return out

    def search_memories(
        self, query: str, *, limit: int = 3
    ) -> list[tuple[MemoryEntry, bool]]:
        """Keyword search over live and archived memories.

        Returns ``(entry, archived)`` pairs, best matches first — scored by how
        many query terms the text contains. Live hits get their recall counters
        bumped (archived ones have already aged out; a bump there is
        meaningless). All scopes are searched: retrieval is explicit, so a
        fact learned in another project is reachable when asked for.
        """
        tokens = [t for t in (query or "").lower().split() if t]
        if not tokens:
            return []
        scored: list[tuple[int, float, MemoryEntry, bool]] = []
        for entry, archived in (
            *((m, False) for m in self.memories()),
            *((m, True) for m in self.archived_memories()),
        ):
            hay = entry.text.lower()
            score = sum(1 for t in tokens if t in hay)
            if score:
                scored.append((score, entry.updated_at, entry, archived))
        scored.sort(key=lambda row: (-row[0], -row[1]))
        hits = [(entry, archived) for _, _, entry, archived in scored[:max(1, limit)]]
        self.note_memories_recalled([e.id for e, archived in hits if not archived])
        return hits

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

    def render_memory_section(self, project: str | None = None) -> str | None:
        """The compiled prompt's ``# Memory`` section (frozen snapshot).

        ``None`` when there are no memories, so empty installs pay nothing.
        With a ``project`` key, only global entries and that project's entries
        render — other projects' facts stay out of this session's prompt (a
        count line says they exist). Entries pass the injection screen; a
        finding replaces that entry with a note naming the file, never
        silently deleting it. Entries whose only evidence was tool output
        (``provenance == "tool"``) are held back the same way: a count line,
        never the text, until a user statement or a Remember confirms them.
        """
        entries = self.memories()
        if not entries:
            return None
        from fluid.context.screening import screen_findings

        shown: list[str] = []
        hidden = 0
        held = 0
        for entry in entries:
            if (
                project is not None
                and entry.scope != "global"
                and entry.scope != project
            ):
                hidden += 1
                continue
            if entry.provenance == "tool":
                held += 1
                continue
            findings = screen_findings(entry.text)
            if findings:
                shown.append(
                    f"- [an entry was withheld — suspected injection "
                    f"pattern(s): {', '.join(findings)}. It is still in "
                    f"{self.memories_path}.]"
                )
            else:
                shown.append(f"- {entry.text}")
        if not shown:
            return None
        footer = (
            f"\n\n({hidden} entr{'y' if hidden == 1 else 'ies'} scoped to "
            "other projects not shown.)"
            if hidden
            else ""
        )
        if held:
            footer += (
                f"\n\n({held} entr{'y' if held == 1 else 'ies'} distilled only "
                "from tool output held back until the user confirms "
                f"{'it' if held == 1 else 'them'}.)"
            )
        return (
            "# Memory\n"
            "Facts your continual learner distilled from earlier sessions. "
            "Trust them, and correct course when the user contradicts one — "
            "the list updates between sessions from what happens in them; "
            "you never edit it directly.\n\n"
            + "\n".join(shown)
            + footer
        )

    # ─── consolidation bookkeeping (see Synthesizer.consolidate) ─────────
    def last_consolidated_at(self) -> float:
        return float(self._load_meta().get("last_consolidated_at", 0.0))

    def set_last_consolidated_at(self, ts: float) -> None:
        meta = self._load_meta()
        meta["last_consolidated_at"] = float(ts)
        self._save_meta(meta)

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
    # Usage counters earn a skill its keep: one that has never been imported
    # since it was written is archived on a tighter horizon than one that has
    # simply gone quiet lately.
    UNUSED_ARCHIVE_AFTER_DAYS = 45.0

    def curate(self, *, now: float | None = None, force: bool = False) -> list[str]:
        """Run one lifecycle pass when due. Returns action lines (may be []).

        Also the store's cleanup tick: every bounded file is reaped here (see
        :meth:`reap`), behind the same interval gate.
        """
        now = time.time() if now is None else now
        meta = self._load_meta()
        last = float(meta.get("last_curated_at", 0.0))
        if not force and now - last < self.CURATE_INTERVAL_DAYS * 86_400:
            return []
        meta["last_curated_at"] = now
        actions: list[str] = []
        archive_dir = self.archive_dir
        for name, info in sorted((meta.get("skills") or {}).items()):
            if not isinstance(info, dict) or info.get("pinned"):
                continue
            try:
                path = self._path(name)
            except ValueError:
                continue
            if not path.is_file():
                continue
            last_activity = max(
                float(info.get("last_used_at", 0.0)),
                float(info.get("updated_at", 0.0)),
            )
            if last_activity <= 0:
                continue  # no evidence either way — leave it alone
            idle_days = (now - last_activity) / 86_400
            never_imported = int(info.get("uses", 0)) <= 0
            horizon = (
                self.UNUSED_ARCHIVE_AFTER_DAYS
                if never_imported
                else self.ARCHIVE_AFTER_DAYS
            )
            if idle_days > horizon:
                try:
                    archive_dir.mkdir(exist_ok=True)
                    path.rename(archive_dir / path.name)
                except OSError:
                    continue
                info["state"] = "archived"
                why = "never imported" if never_imported else "unused"
                actions.append(
                    f"archived {name} ({why}, {idle_days:.0f}d) -> "
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
        # Cleanup rides the same tick — deterministic, silent, never fatal.
        dropped = self.reap()
        if dropped:
            actions.append(
                "reaped " + ", ".join(f"{n} {what}" for what, n in sorted(dropped.items()))
            )
        return actions

    def archived_skills(self) -> list[Skill]:
        """Skill modules curation moved to ``.archive/`` — still recallable."""
        out: list[Skill] = []
        try:
            paths = sorted(self.archive_dir.glob("*.py"))
        except OSError:
            return out
        known = self._load_meta().get("skills", {})
        for path in paths:
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            info = known.get(path.stem) if isinstance(known, dict) else None
            if not isinstance(info, dict):
                info = {}
            out.append(Skill(
                name=path.stem,
                source=source,
                description=_description_of(source) or path.stem,
                uses=int(info.get("uses", 0)),
                updated_at=float(info.get("updated_at", 0.0)),
            ))
        return out

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
_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*")


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
                head = _IDENT_RE.match(item.strip())
                if head:
                    seen.setdefault(head.group(0))
    return list(seen)
