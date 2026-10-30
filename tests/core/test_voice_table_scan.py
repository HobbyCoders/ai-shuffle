"""Regression for overlapping trailing whitespace in voice table detection."""

import os
import random
import re
import subprocess
import sys
from pathlib import Path

from app.core.voice_progress import speech_text


LEGACY_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$|^\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def test_table_detection_legacy_parity():
    # Import the recognizer under test once the production parser is replaced.
    from app.core.voice_progress import _is_table_row

    rng = random.Random(911)
    pieces = ["", " ", "\t", "\r", "\n", "\u2000", "|", ":", "-", "--", "---", "----", "x", ":---:"]
    corpus = ["|", "||", "|text|", "|text\n|", "---|---", "---|---|", "---|---||"]
    corpus += ["".join(rng.choices(pieces, k=rng.randrange(1, 18))) for _ in range(30_000)]
    for text in corpus:
        assert _is_table_row(text) == bool(LEGACY_TABLE_ROW.match(text)), repr(text)


def test_voice_table_scan_has_an_os_deadline(tmp_path):
    root = Path(__file__).resolve().parents[2]
    environment = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP")
                   if key in os.environ}
    environment.update(PYTHONPATH=os.pathsep.join((str(root), str(root / "fluid"))),
                       TESTING="1", AI_SHUFFLE_ENV_FILE=os.devnull,
                       DATA_DIR=str(tmp_path), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                       CLAUDE_CONFIG_DIR=str(tmp_path / "claude"), CODEX_HOME=str(tmp_path / "codex"))
    subprocess.run([sys.executable, "-c", r'''
from app.core.voice_progress import speech_text
assert speech_text("---|---" + " " * 1_000_000 + "x") == "---|--- x."
'''], cwd=root, env=environment, capture_output=True, check=True, timeout=5)


def test_table_rows_are_omitted_from_speech():
    assert speech_text("| name | value |\n:--- | ---:\n\nDone.") == "Done. Details are in the chat."
