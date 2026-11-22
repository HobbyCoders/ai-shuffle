"""Per-model guidance files: location and caps.

``~/.fluid/guidance/<stem>.md`` holds hand-written guidance for model families
whose id contains ``stem`` (case-insensitive substring: ``glm.md`` matches
``z-ai/glm-5.3-flash``). The prompt composer
(``fluid.cli.prompt.load_guidance_files``) renders matching files into the
base prompt, screened and capped. Nothing writes these files automatically:
learned working habits are behaviour rules in the memory store
(:mod:`fluid.codemode.behavior`).
"""

from __future__ import annotations

from pathlib import Path

GUIDANCE_DIR_MAX_FILES = 8
GUIDANCE_FILE_MAX_CHARS = 4_000


def guidance_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".fluid" / "guidance"
