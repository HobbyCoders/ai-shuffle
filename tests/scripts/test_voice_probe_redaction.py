"""The voice probe prints provider errors and exceptions; none may carry the bearer."""
import asyncio
from types import SimpleNamespace

from scripts import voice_probe

BEARER = "xai-probe-test-bearer-0123456789abcdef"


def test_a_provider_error_echoing_the_bearer_is_redacted(monkeypatch, capsys):
    monkeypatch.setenv(voice_probe.TOKEN_ENV, BEARER)
    out = {"errors": [{"type": "error", "code": "unauthorized", "param": None,
                       "message": f"Invalid Authorization header: Bearer {BEARER}"}],
           "added": [], "function_calls": [], "status": "failed", "ended": "error",
           "audio_bytes": 0, "transcript": f"echo {BEARER}"}
    voice_probe.report("user", "handoff", out)
    printed = capsys.readouterr().out
    assert BEARER not in printed and voice_probe.REDACTED in printed


def test_an_exception_carrying_the_bearer_is_redacted_before_it_is_cut(monkeypatch, capsys):
    monkeypatch.setenv(voice_probe.TOKEN_ENV, BEARER)

    async def fails(_args, _shape):
        # The bearer straddles the 200-character cut: a prefix must not survive.
        raise RuntimeError("x" * 190 + f"Bearer {BEARER}")

    monkeypatch.setattr(voice_probe, "run_shape", fails)
    assert asyncio.run(voice_probe.main_async(SimpleNamespace(shapes=["user"]))) == 1
    printed = capsys.readouterr().out
    assert BEARER[:8] not in printed


def test_stderr_lines_are_redacted_too(monkeypatch, capsys):
    monkeypatch.setenv(voice_probe.TOKEN_ENV, BEARER)
    import sys
    voice_probe.print("auth failed for", BEARER, file=sys.stderr)
    assert capsys.readouterr().err == f"auth failed for {voice_probe.REDACTED}\n"
