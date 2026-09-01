"""Anchor sets — the retention half of guarded harness evolution (HCL).

An anchor is a compact replayable case: a task prompt plus success checks,
proving one behavior the harness currently gets right. The commit gate
(:mod:`fluid.codemode.evolution`) replays the set against a proposed
harness-state change and refuses to commit past a regression budget — the
mechanism that lets learned guidance land without silently breaking behavior
that used to work (HCL's "harness-level forgetting").

Anchors live in ``anchors.json`` beside ``memories.json`` in the skills root,
same ``{next_id, entries}`` document shape, ids ``a<n>``. The set is capped
small: replay cost is linear in it, and every entry is load-bearing — adding
past the cap is an explicit error, never a silent eviction.

Checks are outcome-shaped, not transcript-shaped. The task prompt never names
a tool (the testing doctrine: build the situation, don't instruct the flow);
the assertion may check that the flow *fired*:

- ``{"kind": "contains", "pattern": …}``      — regex over the final reply
- ``{"kind": "not_contains", "pattern": …}``  — regex must NOT match it
- ``{"kind": "tool_called", "name": …}``      — the named tool ran
- ``{"kind": "file", "path": …[, "pattern": …]}`` — workspace file assertion

Everything here is stdlib-only and synchronous, mirroring ``skills.py``; the
replay runner (CLI layer) and the gate are the callers.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from fluid.codemode.skills import atomic_write_text, load_json_document

MAX_ANCHORS = 20
MAX_PROMPT_CHARS = 4_000
MAX_SETUP_FILES = 8
MAX_SETUP_CHARS = 20_000
CHECK_KINDS = ("contains", "not_contains", "tool_called", "file")
BASELINES = ("pass", "fail", "unknown")
DEFAULT_MAX_MODEL_CALLS = 8


def _escapes_workspace(path: str) -> bool:
    """True when ``path`` is not a plain workspace-relative path.

    ``Path("/x").is_absolute()`` is False on Windows (no drive), so rooted
    paths are checked explicitly on top of the drive/UNC forms.
    """
    return (
        Path(path).is_absolute()
        or path.startswith(("/", "\\"))
        or ".." in Path(path).parts
    )


@dataclass(frozen=True, kw_only=True)
class Anchor:
    """One replayable case. ``baseline`` is the status of its last plain
    replay — retention compares candidate replays against this recorded value
    instead of paying a fresh control run per gate evaluation."""

    id: str
    title: str
    prompt: str
    checks: tuple[dict, ...]
    setup: dict[str, str] = field(default_factory=dict)  # workspace seed files
    tools: tuple[str, ...] | None = None  # None → the runner's default surface
    model: str | None = None  # None → the session's configured model
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    source: str = "manual"  # "manual" | "harvest" | "session:<id>"
    baseline: str = "unknown"
    created_at: float = 0.0
    last_run_at: float = 0.0
    last_result: str = ""


@dataclass(frozen=True, kw_only=True)
class ReplayOutcome:
    """What one replay produced, in exactly the terms checks are written in.

    ``status`` is ``ok`` (turn completed), ``error`` (provider/engine
    failure), ``timeout``, or ``unchanged`` — the compile-only short-circuit:
    the candidate did not alter this replay's compiled prompt, so the anchor
    cannot have regressed and no model call was spent.
    """

    status: str
    final_text: str = ""
    tool_calls: tuple[str, ...] = ()
    workspace: Path | None = None
    error: str = ""
    # Check verdicts, filled by the runner via :func:`evaluate_checks` while
    # the throwaway workspace still exists (file checks need it); empty means
    # the anchor passed. Consumers read this, not the raw fields.
    failures: tuple[str, ...] = ()


def validate_check(check: object) -> str | None:
    """Return an error message, or None when ``check`` is well-formed."""
    if not isinstance(check, dict):
        return f"check must be an object, got {type(check).__name__}"
    kind = check.get("kind")
    if kind not in CHECK_KINDS:
        return f"unknown check kind {kind!r} (one of {', '.join(CHECK_KINDS)})"
    if kind in ("contains", "not_contains"):
        pattern = check.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return f"{kind} check needs a non-empty 'pattern'"
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"{kind} pattern does not compile: {exc}"
    elif kind == "tool_called":
        if not isinstance(check.get("name"), str) or not check["name"]:
            return "tool_called check needs a tool 'name'"
    elif kind == "file":
        path = check.get("path")
        if not isinstance(path, str) or not path:
            return "file check needs a relative 'path'"
        if _escapes_workspace(path):
            return "file check path must stay inside the workspace"
        pattern = check.get("pattern")
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"file pattern does not compile: {exc}"
    return None


def validate_anchor(
    prompt: str, checks: list | tuple, setup: dict | None = None
) -> str | None:
    """Return an error message, or None when the anchor fields are valid."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "anchor prompt is empty"
    if len(prompt) > MAX_PROMPT_CHARS:
        return f"anchor prompt exceeds {MAX_PROMPT_CHARS} chars"
    if not checks:
        return "anchor needs at least one check"
    for check in checks:
        error = validate_check(check)
        if error is not None:
            return error
    setup = setup or {}
    if len(setup) > MAX_SETUP_FILES:
        return f"anchor setup exceeds {MAX_SETUP_FILES} files"
    for path, content in setup.items():
        if not isinstance(path, str) or not path or _escapes_workspace(path):
            return f"setup path {path!r} must stay inside the workspace"
        if not isinstance(content, str) or len(content) > MAX_SETUP_CHARS:
            return f"setup file {path!r} missing or over {MAX_SETUP_CHARS} chars"
    return None


def evaluate_checks(
    checks: tuple[dict, ...] | list[dict], outcome: ReplayOutcome
) -> list[str]:
    """Failure descriptions for ``outcome`` against ``checks`` (empty = pass).

    An ``unchanged`` outcome passes vacuously (the candidate could not have
    affected it); ``error``/``timeout`` outcomes fail every check.
    """
    if outcome.status == "unchanged":
        return []
    if outcome.status != "ok":
        return [f"replay {outcome.status}: {outcome.error or 'no turn completed'}"]
    failures: list[str] = []
    for check in checks:
        kind = check.get("kind")
        if kind == "contains":
            if not re.search(check["pattern"], outcome.final_text, re.IGNORECASE):
                failures.append(f"final text does not match /{check['pattern']}/")
        elif kind == "not_contains":
            if re.search(check["pattern"], outcome.final_text, re.IGNORECASE):
                failures.append(f"final text matches forbidden /{check['pattern']}/")
        elif kind == "tool_called":
            if check["name"] not in outcome.tool_calls:
                failures.append(f"tool {check['name']} was never called")
        elif kind == "file":
            root = outcome.workspace
            target = (root / check["path"]) if root is not None else None
            if target is None or not target.is_file():
                failures.append(f"workspace file {check['path']} missing")
            elif check.get("pattern"):
                try:
                    content = target.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    failures.append(f"workspace file {check['path']} unreadable: {exc}")
                    continue
                if not re.search(check["pattern"], content):
                    failures.append(
                        f"file {check['path']} does not match /{check['pattern']}/"
                    )
    return failures


class AnchorStore:
    """CRUD over ``anchors.json`` — the same document idiom as memories."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "anchors.json"

    def _load(self) -> dict:
        # A file that does not parse is set aside and logged, never read as
        # empty and then overwritten — same policy as memories.json.
        doc = load_json_document(self.path, {"next_id": 1, "entries": []})
        doc.setdefault("next_id", 1)
        entries = doc.get("entries")
        doc["entries"] = entries if isinstance(entries, list) else []
        return doc

    def _save(self, doc: dict) -> None:
        atomic_write_text(
            self.path,
            json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _to_anchor(entry: dict) -> Anchor:
        return Anchor(
            id=str(entry.get("id", "")),
            title=str(entry.get("title", "")),
            prompt=str(entry.get("prompt", "")),
            checks=tuple(c for c in entry.get("checks", []) if isinstance(c, dict)),
            setup={
                str(k): str(v) for k, v in (entry.get("setup") or {}).items()
            },
            tools=(
                tuple(str(t) for t in entry["tools"])
                if entry.get("tools") is not None
                else None
            ),
            model=entry.get("model") or None,
            max_model_calls=int(
                entry.get("max_model_calls", DEFAULT_MAX_MODEL_CALLS)
            ),
            source=str(entry.get("source", "manual")),
            baseline=(
                str(entry["baseline"])
                if entry.get("baseline") in BASELINES
                else "unknown"
            ),
            created_at=float(entry.get("created_at", 0.0)),
            last_run_at=float(entry.get("last_run_at", 0.0)),
            last_result=str(entry.get("last_result", "")),
        )

    def list(self) -> list[Anchor]:
        return [self._to_anchor(e) for e in self._load()["entries"]]

    def get(self, anchor_id: str) -> Anchor | None:
        for entry in self._load()["entries"]:
            if entry.get("id") == anchor_id:
                return self._to_anchor(entry)
        return None

    def add(
        self,
        *,
        title: str,
        prompt: str,
        checks: list[dict] | tuple[dict, ...],
        setup: dict[str, str] | None = None,
        tools: list[str] | tuple[str, ...] | None = None,
        model: str | None = None,
        max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
        source: str = "manual",
    ) -> Anchor:
        """Validate and persist one anchor (raises ValueError when invalid)."""
        error = validate_anchor(prompt, checks, setup)
        if error is not None:
            raise ValueError(error)
        doc = self._load()
        if len(doc["entries"]) >= MAX_ANCHORS:
            raise ValueError(
                f"anchor set is at its cap of {MAX_ANCHORS}; retire one first "
                "(fluid anchors rm <id>) — every anchor is load-bearing"
            )
        anchor_id = f"a{doc['next_id']}"
        doc["next_id"] = int(doc["next_id"]) + 1
        entry = {
            "id": anchor_id,
            "title": title.strip() or anchor_id,
            "prompt": prompt,
            "checks": list(checks),
            "setup": dict(setup or {}),
            "tools": list(tools) if tools is not None else None,
            "model": model,
            "max_model_calls": int(max_model_calls),
            "source": source,
            "baseline": "unknown",
            "created_at": time.time(),
            "last_run_at": 0.0,
            "last_result": "",
        }
        doc["entries"].append(entry)
        self._save(doc)
        return self._to_anchor(entry)

    def delete(self, anchor_id: str) -> bool:
        doc = self._load()
        before = len(doc["entries"])
        doc["entries"] = [e for e in doc["entries"] if e.get("id") != anchor_id]
        if len(doc["entries"]) == before:
            return False
        self._save(doc)
        return True

    def record_result(self, anchor_id: str, *, passed: bool, detail: str = "") -> None:
        """Record a plain (no-candidate) replay verdict as the new baseline."""
        doc = self._load()
        for entry in doc["entries"]:
            if entry.get("id") == anchor_id:
                entry["baseline"] = "pass" if passed else "fail"
                entry["last_run_at"] = time.time()
                entry["last_result"] = (
                    "pass" if passed else (detail or "fail")[:400]
                )
                self._save(doc)
                return
