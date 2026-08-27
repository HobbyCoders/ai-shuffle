"""Monitor completion summaries — round-3 audit items R8 and R16.

The app overrides fluid's Monitor supervision (ruling A13), so fluid's
supervisor-side ANSI strip never runs for these buffers and the app owns what
the parent is woken with. Two audited failures come from that:

* R8 — colour codes reached the model verbatim; a baseline session wrote an
  ANSI-naive regex against them and paid for a full 34s vitest re-run.
* R16 — 8 of 9 monitor notifications said "(no output; exit code 0)" because
  the monitored command redirected everything into a log file.
"""

import os

import pytest

from app.core import monitor_spawn as ms


# ---------------------------------------------------------------------------
# R8 — no escape sequence survives into a summary
# ---------------------------------------------------------------------------

def test_summarize_strips_ansi():
    lines = [
        "\x1b[32mPASS\x1b[0m src/a.test.ts",
        "\x1b[31mFAIL\x1b[0m src/b.test.ts",
    ]

    summary = ms._summarize(lines)

    assert "\x1b[" not in summary
    assert summary == "PASS src/a.test.ts\nFAIL src/b.test.ts"


def test_summarize_strips_ansi_before_slicing():
    """Scrub first, THEN bound: counting escape bytes against the head/tail
    budget evicts real output the parent needed."""
    payload = "\x1b[36m" + ("x" * 40_000) + "\x1b[0m"

    summary = ms._summarize([payload])

    assert "\x1b[" not in summary
    assert "chars truncated" in summary


def test_summarize_handles_osc_and_two_byte_escapes():
    """Beyond plain colour CSI: OSC title strings (the sequence a package
    manager writes to the terminal title) and the lone two-byte escapes."""
    summary = ms._summarize(["\x1b]0;building\x07done\x1bM"])

    assert "\x1b" not in summary
    assert summary == "done"


@pytest.mark.asyncio
async def test_spawn_background_buffers_clean_lines(monkeypatch):
    """The scrub happens at the SOURCE, so TaskOutput/TaskGet and the summary
    all see clean text."""
    import asyncio

    from app.mcp import builtin_tools as bt

    chunks = [b"\x1b[32mok\x1b[0m\n", b"\x1b[1mbold\x1b[22m done\n", b""]

    class _Stdout:
        async def readline(self):
            return chunks.pop(0)

    class _Proc:
        pid = 4242
        stdout = _Stdout()
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    task = await bt._spawn_background("npm test", None, None)
    await task._reader_task

    assert task.lines == ["ok", "bold done"]
    assert not any("\x1b[" in line for line in task.lines)
    bt._background_tasks.pop(task.task_id, None)


# ---------------------------------------------------------------------------
# R16 — a redirected command's log tail, instead of "(no output)"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command, expected",
    [
        ("npm test > build.log 2>&1", ["build.log"]),
        ("make all >>build.log", ["build.log"]),
        ("pytest 2> errors.log", ["errors.log"]),
        ("npm test | tee -a run.log", ["run.log"]),
        ('cmd > "my logs/out.log"', ["my logs/out.log"]),
        ("noisy > /dev/null", []),
        ("plain command with no redirect", []),
        ("a > one.log && b > two.log", ["one.log", "two.log"]),
    ],
)
def test_redirect_targets(command, expected):
    assert ms._redirect_targets(command) == expected


def test_log_tail_reads_the_commands_own_sink(tmp_path):
    log = tmp_path / "build.log"
    log.write_text("compiling\n\x1b[31merror TS2304\x1b[0m\n", encoding="utf-8")

    tail = ms._log_tail("npm run build > build.log 2>&1", str(tmp_path))

    assert "error TS2304" in tail
    assert "\x1b[" not in tail
    assert "build.log" in tail


def test_log_tail_is_bounded(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("y" * (ms._LOG_TAIL_BYTES * 4), encoding="utf-8")

    tail = ms._log_tail("run > big.log", str(tmp_path))

    assert len(tail) < ms._LOG_TAIL_BYTES * 2
    assert "earlier lines omitted" in tail


def test_log_tail_is_empty_when_there_is_nothing_to_read(tmp_path):
    assert ms._log_tail("run > missing.log", str(tmp_path)) == ""
    assert ms._log_tail("run", str(tmp_path)) == ""
    (tmp_path / "empty.log").write_text("", encoding="utf-8")
    assert ms._log_tail("run > empty.log", str(tmp_path)) == ""


def test_log_tail_accepts_an_absolute_target(tmp_path):
    log = tmp_path / "abs.log"
    log.write_text("finished\n", encoding="utf-8")

    tail = ms._log_tail(f"run > {log}", os.getcwd())

    assert "finished" in tail


# ---------------------------------------------------------------------------
# R6 — the receipt commands the park
# ---------------------------------------------------------------------------

def test_receipt_text_commands_the_park():
    text = ms.monitor_receipt_text("t-9")

    assert text.startswith("Monitoring started (task t-9).")
    assert "End the turn now" in text
    assert "woken when it comes to rest" in text
