"""
Tests for the deploy-time bundled-CLI interrupt patch
(app/core/cli_interrupt_patch.py).

The patch neutralizes ONE cancel-broadcast call site in the ~257MB bundled
``claude`` binary — anchored dynamically by the regex
``remote-cancel"));<3-char ident>({taskRegistry:`` — with a SAME-LENGTH 3-byte
edit (``<ident>`` → ``0&&``) so a user "Stop" no longer kills in-flight
background sub-agents. It only edits when there is EXACTLY ONE unpatched anchor
and ZERO patched ones; anything else is refused. The minified identifier
rotates between CLI bundles AND, since CLI 2.1.218, differs per platform
(``jVr`` in 2.1.205, ``sYr`` in 2.1.214, ``Ppr`` on Windows / ``Opr`` on Linux
in 2.1.218). The stable ``({taskRegistry:`` tail is what makes discovery
platform-independent; unrelated bare ``Ppr(``/``Opr(`` sites elsewhere in the
binary, the second bare ``remote-cancel"));`` prefix (followed by ``continue``),
and old-token sites lacking the tail must never be matched.

These tests use SMALL synthetic tmp-file buffers, never the real binary.
"""

import json

import pytest

import app.core.cli_interrupt_patch as patch_mod
from app.core.cli_interrupt_patch import (
    _MARKER_NAME,
    _PATCHED,
    _PREFIX,
    _TOKEN_OFFSET,
    is_patch_active,
    patch_cli_preserve_background,
)

# The real 2.1.218 tokens (Windows / Linux). The module default used when a test
# doesn't parametrize; kept as a module-level for the decoy/revert helpers.
_DEFAULT_TOKEN = b"Ppr"

# The stable tail that distinguishes the true cancel-broadcast site.
_TAIL = b"({taskRegistry:"


def _anchor(token: bytes = _DEFAULT_TOKEN) -> bytes:
    """The full unpatched anchor for a given 3-char token."""
    return _PREFIX + token + _TAIL


# Decoys that MUST NOT be matched or touched by the patch:
#   * bare token sites without the prefix (``Ppr(``/``Opr(`` elsewhere)
#   * the REAL second prefix site, followed by ``continue`` (no taskRegistry tail)
#   * an OLD-token site ``remote-cancel"));sYr(`` — 3-char ident but WRONG tail
#     (no ``({taskRegistry:``), so the regex must not match it
_DECOY = (
    b"....Ppr(x)....Opr(y)...."
    + b'remote-cancel"));continue}else if(...'
    + b'remote-cancel"));sYr(oldSite)...'
)


def _buffer(*, anchor: bytes = b"", count: int = 1) -> bytes:
    """Filler + `count` copies of `anchor` + decoys, as a binary-ish blob."""
    parts = [b"\x00\x01filler-head" + _DECOY]
    for i in range(count):
        parts.append(b"padding%d" % i)
        parts.append(anchor)
    parts.append(_DECOY + b"filler-tail\xff\xfe")
    return b"".join(parts)


def _write(tmp_path, data: bytes):
    binary = tmp_path / "claude"
    binary.write_bytes(data)
    return binary


@pytest.fixture(autouse=True)
def _reset_active():
    """The patched-active flag is module-global; reset around every test."""
    patch_mod._PATCH_ACTIVE = False
    yield
    patch_mod._PATCH_ACTIVE = False


@pytest.mark.parametrize("token", [b"Ppr", b"Opr"])
def test_single_unpatched_anchor_is_patched(tmp_path, token):
    original = _buffer(anchor=_anchor(token), count=1)
    binary = _write(tmp_path, original)

    result = patch_cli_preserve_background(binary)

    assert result == "patched"
    after = binary.read_bytes()
    # Same length — only the 3-byte token changed.
    assert len(after) == len(original)
    assert after.count(_anchor(token)) == 0
    assert after.count(_PATCHED) == 1
    # The anchor now reads as the patched form.
    assert _PATCHED in after
    # After patching, the anchor no longer contains either bare token, so only
    # the decoy bare-token sites remain (one Ppr( + one Opr( per decoy blob,
    # head + tail = 2 each) regardless of which token was the anchor. The
    # old-token / ``continue`` prefix sites (2 each) are likewise untouched.
    assert after.count(b"Ppr(") == 2
    assert after.count(b"Opr(") == 2
    assert after.count(b'remote-cancel"));continue') == 2
    assert after.count(b'remote-cancel"));sYr(') == 2
    assert is_patch_active() is True
    # A marker recording sdk_version + size was written.
    marker = json.loads((tmp_path / _MARKER_NAME).read_text())
    assert marker["size"] == len(after)
    # The patched token sits exactly where the marker says.
    token_offset = marker["token_offset"]
    assert after[token_offset : token_offset + 3] == b"0&&"


@pytest.mark.parametrize("token", [b"Ppr", b"Opr"])
def test_patch_then_repatch_roundtrip_is_idempotent(tmp_path, token):
    """Patch a fresh buffer, then re-run: second run sees the patched form and
    reports already-patched without changing bytes (platform-independent)."""
    original = _buffer(anchor=_anchor(token), count=1)
    binary = _write(tmp_path, original)

    assert patch_cli_preserve_background(binary) == "patched"
    patched_bytes = binary.read_bytes()

    # Drop the marker so the second run must confirm via a full scan.
    (tmp_path / _MARKER_NAME).unlink()
    patch_mod._PATCH_ACTIVE = False

    assert patch_cli_preserve_background(binary) == "already-patched"
    assert binary.read_bytes() == patched_bytes  # untouched
    assert is_patch_active() is True


def test_already_patched_binary_is_reported_unchanged(tmp_path):
    original = _buffer(anchor=_PATCHED, count=1)
    binary = _write(tmp_path, original)

    result = patch_cli_preserve_background(binary)

    assert result == "already-patched"
    assert binary.read_bytes() == original  # untouched
    assert is_patch_active() is True


def test_zero_anchors_is_pattern_shifted(tmp_path):
    original = _buffer(anchor=b"", count=0)  # decoys only, no real anchor
    binary = _write(tmp_path, original)

    result = patch_cli_preserve_background(binary)

    assert result == "pattern-shifted"
    assert binary.read_bytes() == original  # refused → untouched
    assert is_patch_active() is False
    assert not (tmp_path / _MARKER_NAME).exists()


def test_two_unpatched_anchors_are_refused(tmp_path):
    original = _buffer(anchor=_anchor(), count=2)
    binary = _write(tmp_path, original)

    result = patch_cli_preserve_background(binary)

    assert result == "pattern-shifted"
    assert binary.read_bytes() == original  # never edits an ambiguous binary
    assert is_patch_active() is False


def test_prefix_with_token_but_no_taskregistry_tail_is_not_matched(tmp_path):
    """prefix + 3-char ident + ``(`` but WITHOUT ``{taskRegistry:`` must NOT be
    matched → zero anchors → pattern-shifted (no false patch)."""
    near_miss = _PREFIX + b"Ppr(" + b"notTaskRegistry:...)"
    original = _buffer(anchor=near_miss, count=1)
    binary = _write(tmp_path, original)

    result = patch_cli_preserve_background(binary)

    assert result == "pattern-shifted"
    assert binary.read_bytes() == original  # untouched
    assert is_patch_active() is False


def test_marker_skips_rescan_when_bytes_verify(tmp_path):
    original = _buffer(anchor=_anchor(), count=1)
    binary = _write(tmp_path, original)

    assert patch_cli_preserve_background(binary) == "patched"
    marker = json.loads((tmp_path / _MARKER_NAME).read_text())
    token_offset = marker["token_offset"]
    patched_size = binary.stat().st_size

    # Same-SIZE buffer with NO anchor anywhere, but the patched token ``0&&``
    # still sitting at the recorded offset. A full scan would see zero anchors →
    # "pattern-shifted"; the marker's byte re-verification must short-circuit to
    # "already-patched" WITHOUT scanning (proving the warm path both matched the
    # marker AND confirmed the bytes still carry the patch).
    garbage = bytearray(b"Z" * patched_size)
    garbage[token_offset : token_offset + 3] = b"0&&"
    binary.write_bytes(bytes(garbage))
    patch_mod._PATCH_ACTIVE = False  # simulate a fresh boot

    result = patch_cli_preserve_background(binary)

    assert result == "already-patched"
    assert is_patch_active() is True


def test_reverted_bytes_are_repatched_not_trusted(tmp_path):
    """Marker survives over REVERTED bytes (same-length edit → same size). The
    warm path must NOT trust the stale marker: byte re-verification fails, the
    scan runs, and the lone unpatched anchor is re-patched."""
    token = _DEFAULT_TOKEN
    original = _buffer(anchor=_anchor(token), count=1)
    binary = _write(tmp_path, original)

    assert patch_cli_preserve_background(binary) == "patched"
    marker = json.loads((tmp_path / _MARKER_NAME).read_text())
    token_offset = marker["token_offset"]

    # Revert the 3 patched bytes back to the original token, leaving the marker
    # (matching sdk version + size) in place.
    data = bytearray(binary.read_bytes())
    data[token_offset : token_offset + 3] = token
    binary.write_bytes(bytes(data))
    assert binary.read_bytes().count(_anchor(token)) == 1  # anchor restored
    patch_mod._PATCH_ACTIVE = False  # simulate a fresh boot

    result = patch_cli_preserve_background(binary)

    # Marker matched size/version but the bytes reverted → NOT trusted on the
    # warm path; the scan ran, found one unpatched anchor, and re-patched it.
    assert result == "patched"
    after = binary.read_bytes()
    assert after.count(_PATCHED) == 1
    assert after.count(_anchor(token)) == 0
    assert is_patch_active() is True


def test_old_format_marker_without_token_offset_falls_through(tmp_path):
    """A pre-existing marker with no ``token_offset`` (old format) can't be
    byte-verified → it must fall through to the scan, never a false trust."""
    original = _buffer(anchor=_anchor(), count=1)
    binary = _write(tmp_path, original)

    # Old-format marker: matches sdk version + size but carries NO token_offset.
    (tmp_path / _MARKER_NAME).write_text(
        json.dumps(
            {
                "sdk_version": patch_mod.claude_agent_sdk.__version__,
                "size": binary.stat().st_size,
            }
        ),
        encoding="utf-8",
    )

    result = patch_cli_preserve_background(binary)

    # No offset to verify → don't trust the marker; the scan runs and patches the
    # lone unpatched anchor (no false "already-patched").
    assert result == "patched"
    assert binary.read_bytes().count(_PATCHED) == 1
    assert is_patch_active() is True


def test_read_only_file_returns_read_only_without_raising(tmp_path):
    original = _buffer(anchor=_anchor(), count=1)
    binary = _write(tmp_path, original)
    binary.chmod(0o444)  # readable but not writable → open("r+b") fails

    try:
        result = patch_cli_preserve_background(binary)
    finally:
        binary.chmod(0o644)

    assert result == "read-only"
    # Read succeeded but the write was refused — bytes unchanged, no raise.
    assert binary.read_bytes() == original
    assert is_patch_active() is False


def test_missing_binary_resolver_returns_none(monkeypatch, tmp_path):
    """resolve_bundled_cli_path returns None when the file is absent."""
    from app.core import cli_interrupt_patch as m

    fake_sdk_mod = tmp_path / "sdk" / "_internal" / "transport"
    fake_sdk_mod.mkdir(parents=True)
    fake_file = fake_sdk_mod / "subprocess_cli.py"
    fake_file.write_text("# stub")

    import types

    stub = types.SimpleNamespace(__file__=str(fake_file))
    monkeypatch.setattr(
        "claude_agent_sdk._internal.transport.subprocess_cli", stub, raising=False
    )
    # No _bundled/claude(.exe) created under the fake tree → None.
    assert m.resolve_bundled_cli_path() is None
