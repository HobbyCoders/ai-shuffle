"""Bounded declarative memory — durable facts injected as a frozen snapshot.

fluid's continual harness stores *procedural* memory (executable skills); this
module is the *declarative* sibling: a small, char-capped, human-editable file
of facts that follow the user across sessions — stated preferences, environment
conventions, corrections that should never need repeating.

Design contract (each line is load-bearing):

* **Bounded.** One file, hard char cap on total content. A memory that can
  grow without bound decays into noise the model pays for every turn; the cap
  plus a visible fill percentage make the model curate instead of hoard.
* **Frozen snapshot.** The file is read once per prompt compilation (session
  start). Mid-session writes are durable on disk but never touch the compiled
  prompt — the prefix cache survives; the snapshot refreshes next session.
* **Batch, atomic, cap-on-final.** One Memory tool call applies a whole
  operations array and checks the cap only on the final result — so a single
  call can remove stale entries and add new ones even when the add alone would
  overflow. On overflow nothing is written and the current entries come back
  in the error, so the model can reissue one consolidating batch.
* **Screened at injection.** Entries pass the same conservative injection
  screen as instruction files when the snapshot is built; a poisoned entry is
  replaced in the snapshot (never silently deleted from disk — the user
  removes it).

Entries are separated by ``---`` lines (a markdown thematic break), so the
file reads and edits naturally in any editor.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

DEFAULT_MEMORY_FILE = Path.home() / ".fluid" / "MEMORY.md"
DEFAULT_CHAR_LIMIT = 3_000

_SEPARATOR_RE = re.compile(r"^\s*---\s*$", re.MULTILINE)


def _split_entries(content: str) -> list[str]:
    return [e.strip() for e in _SEPARATOR_RE.split(content) if e.strip()]


def _join_entries(entries: list[str]) -> str:
    return "\n---\n".join(entries) + ("\n" if entries else "")


class MemoryStore:
    """The file-backed store. Stateless between calls — every operation
    re-reads the file, so concurrent sessions see each other's writes."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        char_limit: int = DEFAULT_CHAR_LIMIT,
    ) -> None:
        # The default resolves at call time, not import time, so a changed
        # HOME (tests, sandboxes) is honored.
        self.path = (
            Path(path) if path is not None else Path.home() / ".fluid" / "MEMORY.md"
        )
        self.char_limit = max(200, int(char_limit))

    def entries(self) -> list[str]:
        try:
            return _split_entries(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return []

    def content_chars(self, entries: list[str] | None = None) -> int:
        return len(_join_entries(entries if entries is not None else self.entries()))

    def apply(self, operations: list[dict[str, Any]]) -> tuple[bool, str]:
        """Apply one batched operation list atomically.

        Each operation: ``{"action": "add"|"remove"|"replace", "content"?,
        "old_text"?}``. ``remove``/``replace`` match the single entry that
        contains ``old_text``; an ambiguous or missing match fails the whole
        batch (nothing is written). Returns ``(ok, message)``.
        """
        from fluid.context.screening import looks_like_secret

        entries = self.entries()
        for i, op in enumerate(operations):
            action = str(op.get("action", "")).strip().lower()
            content_field = str(op.get("content", "")).strip()
            if action in ("add", "replace") and content_field:
                secret_kind = looks_like_secret(content_field)
                if secret_kind is not None:
                    return False, (
                        f"operation {i}: the entry looks like it contains a "
                        f"credential ({secret_kind}). Memory is injected into "
                        "every future session's prompt — never store secrets "
                        "in it. Store WHERE the credential lives (the env var "
                        "name, the file) instead of its value."
                    )
            if action == "add":
                content = content_field
                if not content:
                    return False, f"operation {i}: add needs non-empty content"
                if _SEPARATOR_RE.search(content):
                    return False, (
                        f"operation {i}: an entry cannot contain a '---' line "
                        "(that is the entry separator)"
                    )
                entries.append(content)
                continue
            if action in ("remove", "replace"):
                old = str(op.get("old_text", "")).strip()
                if not old:
                    return False, f"operation {i}: {action} needs old_text"
                hits = [j for j, e in enumerate(entries) if old in e]
                if not hits:
                    return False, (
                        f"operation {i}: no entry contains {old!r}. "
                        f"Current entries:\n{self.render_entries(entries)}"
                    )
                if len(hits) > 1:
                    return False, (
                        f"operation {i}: {old!r} matches {len(hits)} entries — "
                        "use a longer unique fragment"
                    )
                if action == "remove":
                    entries.pop(hits[0])
                else:
                    content = str(op.get("content", "")).strip()
                    if not content:
                        return False, f"operation {i}: replace needs content"
                    entries[hits[0]] = content
                continue
            return False, (
                f"operation {i}: unknown action {action!r} "
                "(expected add | remove | replace)"
            )
        final = _join_entries(entries)
        if len(final) > self.char_limit:
            return False, (
                f"memory is over its {self.char_limit}-char limit "
                f"({len(final)} chars after this batch). Nothing was written. "
                "Reissue as ONE batch that removes or shortens stale entries "
                f"and adds the new ones together.\nCurrent entries:\n"
                f"{self.render_entries(self.entries())}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(final, encoding="utf-8")
        os.replace(tmp, self.path)
        pct = min(100, int(len(final) / self.char_limit * 100))
        return True, (
            f"Memory updated ({len(entries)} entries, {pct}% of "
            f"{self.char_limit} chars). This update is complete — do not "
            "repeat it. The prompt snapshot refreshes next session."
        )

    @staticmethod
    def render_entries(entries: list[str]) -> str:
        if not entries:
            return "(memory is empty)"
        return "\n---\n".join(entries)

    def render_section(self) -> str | None:
        """The compiled prompt's ``# Memory`` section (frozen snapshot).

        ``None`` when the store is empty, so empty installs pay nothing.
        Entries are screened individually; a finding replaces that entry with
        a note naming the file so the user can clean it up.
        """
        from fluid.context.screening import screen_findings

        entries = self.entries()
        if not entries:
            return None
        shown: list[str] = []
        for entry in entries:
            findings = screen_findings(entry)
            if findings:
                shown.append(
                    f"[an entry was withheld from the prompt — suspected "
                    f"injection pattern(s): {', '.join(findings)}. It is "
                    f"still in {self.path}; remove it with the Memory tool.]"
                )
            else:
                shown.append(entry)
        chars = self.content_chars(entries)
        pct = min(100, int(chars / self.char_limit * 100))
        return (
            "# Memory\n"
            f"Durable facts saved in earlier sessions "
            f"({pct}% full — {chars:,}/{self.char_limit:,} chars). Snapshot "
            "from session start; curate with the Memory tool — saves land on "
            "disk now and in the prompt next session.\n\n"
            + "\n---\n".join(shown)
        )
