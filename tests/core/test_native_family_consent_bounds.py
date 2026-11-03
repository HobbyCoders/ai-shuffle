"""Full native-consent paths have a process-level wall limit, not a GIL-bound timer."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import model_override as mo
from tests.core.test_model_override import ARGS, _start, _turn, chats, world  # noqa: F401
from tests.core.test_model_override_memory import _save


PASTES = [
    "=" * 29 + " 615 passed in 10.88s " + "=" * 29,
    "Opus " + "-" * 4096 + " for reviews",
    "Opus " + " " * 4096 + "for reviews",
    "download 50%|" + "█" * 4096 + "| done",
]


def test_native_consent_has_a_process_deadline(tmp_path):
    # re holds the GIL: an asyncio/thread timeout cannot kill a bad expression.
    # Select only the private workers below, so this subprocess cannot recurse.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(Path(__file__).resolve()),
             "-o", "python_functions=_bounded_*", "--basetemp", str(tmp_path / "child"), "-q", "-s"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"Native consent exceeded its 30s process deadline: {exc.stdout!r}")
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("paste", PASTES, ids=["summary", "dashes", "spaces", "progress"])
@pytest.mark.parametrize("source", ["chat", "memory"])
async def _bounded_admission(chats, source, paste):
    print(f"BEGIN {source} {paste[:30]!r}", flush=True)
    if source == "chat":
        _save("Risky/cross-cutting code and all UI work: Opus.")
        _turn("caller", "user", paste, profile="agent")
    else:
        # Owner facts cap persisted text at 2000 characters; chat and the raw
        # memory-consumer assertion below still exercise the full 4096 runs.
        memory = _save(paste[:1900] + "\nReviews: Opus.")
        assert memory.text  # exercise the real whitespace-normalizing writer
        _turn("caller", "user", "start the review", profile="agent")
    started = time.perf_counter()
    result = await _start({**ARGS, "model": "opus"})
    assert not result.is_error and not chats[2], result.text
    # Also bound the memory consumer on raw/legacy whitespace, not only the
    # normalized string the real producer persists and StartSession just read.
    assert mo.memory_names("opus", paste + "\nReviews: Opus.")
    assert time.perf_counter() - started < 6
    print(f"END {source}: {time.perf_counter() - started:.4f}s", flush=True)


@pytest.mark.parametrize("padding", [0, 64000])
async def _bounded_long_history(chats, monkeypatch, padding):
    _save("Risky/cross-cutting code and all UI work: Opus.")
    _turn("caller", "user", "Use Opus for reviews", profile="agent")
    paste = PASTES[0] + "\ndownload 50%|" + "█" * 128 + "| done\n" + "-" * (256 + padding)
    for _ in range(1600):
        _turn("caller", "user", paste, profile="agent")
    scan = mo._find_named
    measured = []

    def timed(*args, **kwargs):
        started = time.perf_counter()
        try:
            return scan(*args, **kwargs)
        finally:
            measured.append(time.perf_counter() - started)

    monkeypatch.setattr(mo, "_find_named", timed)
    result = await _start({**ARGS, "model": "opus"})
    if measured[0] >= 5:
        assert result.is_error and len(chats[2]) == 1 and not chats[1], result.text
        assert "history" in result.text
    else:
        assert not result.is_error and not chats[2], result.text
    assert len(measured) == 1 and measured[0] < 5.5
    print(f"1601-turn history ({len(paste)} chars/row): scan={measured[0]:.4f}s "
          f"(5s budget plus one row); receipt={result.text}", flush=True)

@pytest.mark.parametrize("text", [
    ("a-" * 32000),
    ("3f2a9c1e-7b1d-4e8a-9f00-1a2b3c4d5e6f-" * 1800)[:64000],
    ("1." * 32000),
], ids=["hyphenated", "joined-uuids", "dotted-numbers"])
async def _bounded_quadratic_shapes(chats, text):
    _save("Risky/cross-cutting code and all UI work: Opus.")
    _turn("caller", "user", text, profile="agent")
    started = time.perf_counter()
    result = await _start({**ARGS, "model": "opus"})
    elapsed = time.perf_counter() - started
    assert not result.is_error and not chats[2], result.text
    assert elapsed < 1, f"64k structured token took {elapsed:.4f}s"
    print(f"64k structured token: {elapsed:.4f}s", flush=True)
