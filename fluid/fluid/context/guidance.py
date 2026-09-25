"""Per-model guidance files — shared mechanics.

``~/.fluid/guidance/<stem>.md`` holds guidance for model families whose id
contains ``stem`` (case-insensitive substring: ``glm.md`` matches
``z-ai/glm-5.3-flash``). Three consumers share these mechanics and must
agree on them:

- the CLI prompt composer (``fluid.cli.prompt.load_guidance_files``) renders
  matching files into the base prompt, screened and capped;
- the continual synthesizer receives the raw matching sources so it can
  propose evidence-based edits (queued, never applied directly);
- the anchor-replay runner stages a copy of the directory with a candidate
  edit applied when gating a proposal.

This module owns the directory location, the caps, and the raw
``{stem: content}`` reader; rendering concerns (screening, truncation
markers) stay with the prompt composer.
"""

from __future__ import annotations

from pathlib import Path

GUIDANCE_DIR_MAX_FILES = 8
GUIDANCE_FILE_MAX_CHARS = 4_000


def guidance_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".fluid" / "guidance"


def guidance_sources(model: str, home: Path | None = None) -> dict[str, str]:
    """Raw ``{stem: content}`` for guidance files matching ``model``.

    Same matching and ordering rules as the prompt composer (stem must be a
    non-empty case-insensitive substring of the model id; sorted by name;
    at most ``GUIDANCE_DIR_MAX_FILES`` files considered), but contents are
    returned unscreened and untruncated — callers that render into a prompt
    apply their own screening.
    """
    lowered = (model or "").lower()
    if not lowered:
        return {}
    out: dict[str, str] = {}
    try:
        paths = sorted(guidance_dir(home).glob("*.md"))
    except OSError:
        return {}
    for path in paths[:GUIDANCE_DIR_MAX_FILES]:
        stem = path.stem.strip().lower()
        if not stem or stem not in lowered:
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if content:
            out[stem] = content
    return out
