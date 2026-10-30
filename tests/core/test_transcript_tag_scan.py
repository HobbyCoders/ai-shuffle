"""Literal transcript tags keep lazy-first-pair semantics without prefix retries."""

import os
import random
import re
import subprocess
import sys
from pathlib import Path

from app.core.jsonl_parser import (
    _extract_local_command_output, _extract_task_notification_field, _parse_slash_command,
)


def test_tag_consumers_match_legacy_on_bounded_grammar():
    rng = random.Random(911)
    tags = ("task-id", "tool-use-id", "status", "summary", "result", "local-command-stdout",
            "command-message", "command-name", "command-args")
    pieces = ["", "literal", "\n", "\r\n", "\t", " ", "<", ">", "</bad>"]
    pieces += [f"<{tag}>" for tag in tags] + [f"</{tag}>" for tag in tags]
    corpus = ["", "<command-name></command-name>"]
    for _ in range(10_000):
        tag = rng.choice(tags)
        inner = "".join(rng.choices(pieces, k=rng.randrange(6)))
        block = f"<{tag}>{inner}</{tag}>"
        corpus.append("".join(rng.choices([block, *pieces], k=rng.randrange(1, 8))))
    for text in corpus:
        expected = {}
        for field, tag in (("command_id", "command-message"), ("command_name", "command-name"),
                           ("command_args", "command-args")):
            match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
            if match:
                expected[field] = match.group(1).strip()
        slash = expected if "command_name" in expected and text.strip().startswith("<command-") else None
        assert _parse_slash_command(text) == slash
        for tag in tags:
            match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
            expected_field = match.group(1).strip() if match else None
            assert _extract_task_notification_field(text, tag) == expected_field
            if tag == "local-command-stdout":
                assert _extract_local_command_output(text) == expected_field


def test_tag_scans_have_an_os_deadline(tmp_path):
    root = Path(__file__).resolve().parents[2]
    environment = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP")
                   if key in os.environ}
    environment.update(PYTHONPATH=os.pathsep.join((str(root), str(root / "fluid"))),
                       TESTING="1", AI_SHUFFLE_ENV_FILE=os.devnull,
                       DATA_DIR=str(tmp_path), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                       CLAUDE_CONFIG_DIR=str(tmp_path / "claude"), CODEX_HOME=str(tmp_path / "codex"))
    subprocess.run([sys.executable, "-c", r'''
from app.core.jsonl_parser import _extract_local_command_output, _extract_task_notification_field, _parse_slash_command
assert _extract_local_command_output("<local-command-stdout>" * 100_000) is None
assert _extract_task_notification_field("<summary>" * 100_000, "summary") is None
assert _parse_slash_command("<command-name>" * 100_000) is None
'''], cwd=root, env=environment, capture_output=True, check=True, timeout=5)
