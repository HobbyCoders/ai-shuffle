"""Compatibility and OS-deadline regressions for rewind's suffix-only parser."""

import os
import random
import re
import subprocess
import sys
from pathlib import Path

from app.core.jsonl_rewind import checkpoints_from_entries, extract_entry_text


# Bounded compatibility oracle, never run on the adversarial deadline inputs.
LEGACY = re.compile(
    r'(?:\s*<system[•-]reminder>(?:(?!</system[•-]reminder>).)*'
    r'</system[•-]reminder>)+\s*$', re.DOTALL,
)


def test_rewind_suffix_scan_matches_legacy_grammar():
    opens = [f"<system{separator}reminder>" for separator in ("•", "-")]
    closes = [f"</system{separator}reminder>" for separator in ("•", "-")]
    whitespace = [chr(c) for c in range(0x3100) if chr(c).isspace()]
    pieces = ["", "prompt", "literal", "\n", "\r\n", " ", "\t", "<", ">",
              "<system-reminder", "</system-reminder", "<SYSTEM-reminder>",
              "</system-reminder>inline", *opens, *closes, *whitespace]
    corpus = []
    for opener in opens:
        for closer in closes:
            for gap in whitespace:
                note = opener + "body" + closer
                corpus.extend((
                    gap + note + gap,
                    "prompt" + gap + note + gap + note + gap,
                    "prompt" + note + "literal" + gap + note,
                    "prompt" + gap + opener + note + gap,
                    "prompt" + note + closer + gap,
                    "prompt" + note + gap + opener + "unclosed",
                ))
    rng = random.Random(911)
    for _ in range(30_000):
        # Compose valid/nested blocks as well as malformed tag fragments; a
        # uniform character fuzzer almost never reaches the suffix grammar.
        body = "".join(rng.choices(pieces, k=rng.randrange(9)))
        note = rng.choice(opens) + body + rng.choice(closes)
        corpus.append("".join(rng.choices([note, *pieces], k=rng.randrange(1, 10))))
    for text in corpus:
        expected = LEGACY.sub("", text)
        for role in ("user", "assistant"):
            actual = extract_entry_text({"message": {"role": role, "content": text}})
            assert actual == (expected if role == "user" else text), repr(text)
            if actual == text:
                assert actual is text
        assert extract_entry_text({"message": {"role": "user", "content": [
            {"type": "text", "text": text}, {"type": "image"},
        ]}}) == expected


def test_checkpoint_path_retains_only_the_real_prompt():
    text = "prompt\n<system-reminder>facts</system•reminder>\n"
    checkpoints = checkpoints_from_entries([
        {"uuid": "user", "type": "user", "message": {"role": "user", "content": text}},
        {"uuid": "assistant", "type": "assistant", "message": {"role": "assistant", "content": text}},
    ])
    assert [(cp.uuid, cp.full_message) for cp in checkpoints] == [
        ("user", "prompt"), ("assistant", text),
    ]


def test_rewind_reminders_are_bounded_by_an_os_deadline(tmp_path):
    root = Path(__file__).resolve().parents[2]
    environment = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP")
                   if key in os.environ}
    environment.update(PYTHONPATH=os.pathsep.join((str(root), str(root / "fluid"))),
                       TESTING="1", AI_SHUFFLE_ENV_FILE=os.devnull,
                       DATA_DIR=str(tmp_path), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                       CLAUDE_CONFIG_DIR=str(tmp_path / "claude"), CODEX_HOME=str(tmp_path / "codex"))
    # A thread/async timeout cannot interrupt a C regex that holds the GIL.
    subprocess.run([sys.executable, "-c", r'''
from app.core.jsonl_rewind import checkpoints_from_entries, extract_entry_text
for separator in ("•", "-"):
    opener = f"<system{separator}reminder>"
    closer = f"</system{separator}reminder>"
    for body, stripped in (("\n" * 1_000_000 + opener + "unclosed", False),
                           (opener * 50_000, False), (closer * 50_000, False),
                           (("\n" + opener + "body" + closer) * 20_000, True)):
        text = "prompt" + body
        entry = {"uuid": "u", "type": "user", "message": {"role": "user", "content": text}}
        expected = "prompt" if stripped else text
        assert extract_entry_text(entry) == expected
        checkpoints = checkpoints_from_entries([entry])
        assert len(checkpoints) == 1
        assert checkpoints[0].full_message == expected
'''], cwd=root, env=environment, capture_output=True, check=True, timeout=5)
