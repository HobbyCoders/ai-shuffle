"""
Deploy-time patch: make a user "Stop"/interrupt spare in-flight background
sub-agents by neutralizing ONE call site in the bundled Claude CLI binary.

Why this exists
---------------
The app runs the SDK's bundled ``claude`` CLI (a ~257MB Bun binary shipped
inside ``claude_agent_sdk``; we never set ``cli_path``). The CLI's interrupt is
QUERY-scoped, not task-scoped: a single ``interrupt()`` — what a user "Stop"
triggers — stops EVERY in-flight ``Task`` child, so stopping the main turn also
kills any background sub-agents the user wanted to keep running. There is no
task-filtered interrupt to call; the only real fix lives in the CLI itself.

The one call site responsible is the cancel-broadcast call
``remote-cancel"));<tok>({taskRegistry:``, where ``<tok>`` is a minified
identifier. Neutralizing it is a SAME-LENGTH, three-byte edit: replace the
3-char ``<tok>`` with ``0&&`` so the anchor becomes
``remote-cancel"));0&&({taskRegistry:`` — turning the cancel-broadcast call into
a dead ``0 && (...)`` short-circuit. Equal length means the edit is done in
place (a single ``seek``+``write`` of three bytes), never a full-file rewrite,
and Bun does not integrity-check its embedded payload, so the patched binary
runs clean.

Safety
------
The token is a MINIFIED identifier that ROTATES between CLI bundles AND, since
CLI 2.1.218, DIFFERS PER PLATFORM. Token history:

  * ``jVr`` — CLI 2.1.205 / SDK 0.2.114
  * ``sYr`` — CLI 2.1.214 / SDK 0.2.122
  * ``Ppr`` (Windows ``claude.exe``) / ``Opr`` (Linux ``claude``) —
    CLI 2.1.218 / SDK 0.2.126

Because the identifier is no longer stable, the anchor is discovered DYNAMICALLY
by regex (``_ANCHOR_RE``) rather than by a hardcoded token: the tail
``({taskRegistry:`` is a property access, and minifiers do NOT mangle property
names, so that tail stays stable across platforms and builds even as the
identifier rotates. The regex requires EXACTLY THREE identifier chars between
the prefix and ``({taskRegistry:`` — that constraint is load-bearing: the
replacement ``0&&`` is exactly 3 bytes, so a same-length in-place edit is only
valid for a 3-char identifier. A future build minifying to a 2- or 4-char
identifier therefore yields ZERO regex matches → ``"pattern-shifted"`` refusal
(correct — a different length needs a different, same-length neutralization).

Several bare ``Ppr(``/``Opr(`` sites exist elsewhere in the binary and MUST NOT
be touched — they are unrelated. The bare prefix ``remote-cancel"));`` itself
appears MULTIPLE times (as of 2.1.218, twice: this cancel-broadcast site and a
second site immediately followed by ``continue``), so the prefix alone is NOT
unique — only ONE occurrence carries the ``({taskRegistry:`` tail. This module
only ever edits when there is EXACTLY ONE unpatched anchor (full regex match,
prefix + 3-char token + ``({taskRegistry:`` together) and ZERO already-patched
anchors; any other count means the CLI shifted between SDK versions and we
refuse to edit (``"pattern-shifted"``, logged loudly with the discovered
tokens). The FULL anchor is what makes the match unique, so neither the repeated
bare prefix nor bare token occurrences elsewhere are ever in scope.

A sibling marker file (``.claude_interrupt_patch``) records the SDK version,
binary size, and the absolute ``token_offset`` of the 3 patched bytes at patch
time so a subsequent boot can skip the 257MB scan. Because the edit is
same-length (patched and unpatched binaries are byte-for-byte the same size),
size alone can't prove the bytes still carry the patch — a warm boot therefore
re-verifies by seeking to ``token_offset`` and reading the 3 bytes. Only when
they read as ``0&&`` is the scan skipped; a reverted binary (or an old-format
marker with no ``token_offset``) falls through to the full scan.

This is opt-in: it only runs when ``settings.preserve_background_on_interrupt``
is set (see ``app/main.py``). No binary is ever touched by default.
"""

import json
import logging
import platform
import re
from pathlib import Path

import claude_agent_sdk

logger = logging.getLogger(__name__)

# The unique cancel-broadcast call site. Same-length replacement neutralizes it
# by turning the call into a dead ``0 && (...)`` short-circuit.
_PREFIX = b'remote-cancel"));'
# The cancel-broadcast call site: prefix + EXACTLY-3-char minified identifier +
# ``({taskRegistry:``. ``taskRegistry`` is a property name (minifiers don't
# mangle props), so the tail is stable across platforms/builds even though the
# identifier rotates (and, since CLI 2.1.218, differs per platform: ``Ppr`` on
# Windows, ``Opr`` on Linux). The exactly-3 constraint is load-bearing: the
# replacement ``0&&`` only fits a 3-char identifier as a same-length edit.
_ANCHOR_RE = re.compile(rb'remote-cancel"\)\);([A-Za-z0-9_$]{3})\(\{taskRegistry:')
_PATCHED = _PREFIX + b"0&&("  # patched-form detector (must stay exact)
_NEW_TOKEN = b"0&&"
# Offset of the 3-byte token within the anchor (computed, not hardcoded — the
# prefix ``remote-cancel"));`` is 17 bytes, so the token starts at index 17).
_TOKEN_OFFSET = len(_PREFIX)

# Sibling of the binary; lets a warm boot skip the full-file scan.
_MARKER_NAME = ".claude_interrupt_patch"

# Set True the moment we confirm the running binary carries the neutralized
# anchor (either freshly patched this boot or already-patched from a prior one).
_PATCH_ACTIVE = False


def is_patch_active() -> bool:
    """True iff the bundled CLI binary is confirmed to carry the patch.

    Distinct from the config flag: the flag is the operator's INTENT, this is
    whether the deploy-time edit actually landed. Callers that surface the
    effective behavior (e.g. the ``/live`` endpoint) should AND the two.
    """
    return _PATCH_ACTIVE


def resolve_bundled_cli_path() -> Path | None:
    """Locate the SDK's bundled ``claude`` binary, or None if it is absent.

    Mirrors the SDK's own ``_find_bundled_cli`` resolver: the ``_bundled``
    directory sits three levels up from ``subprocess_cli.py`` inside the
    installed ``claude_agent_sdk`` package. The SDK names the binary
    ``claude.exe`` on Windows and ``claude`` elsewhere, so mirror that too. We
    never set ``cli_path``, so this is the exact binary every spawn runs.
    """
    from claude_agent_sdk._internal.transport import subprocess_cli

    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = (
        Path(subprocess_cli.__file__).parent.parent.parent / "_bundled" / cli_name
    )
    if bundled.exists() and bundled.is_file():
        return bundled
    return None


def _marker_path(binary: Path) -> Path:
    return binary.parent / _MARKER_NAME


def _write_marker(binary: Path, token_offset: int) -> None:
    """Record the SDK version, binary size, and patched-token offset.

    ``token_offset`` is the absolute byte position of the 3 patched bytes; a warm
    boot re-reads it to prove the bytes still carry the patch before skipping the
    scan (the edit is same-length, so size alone can't).
    """
    try:
        _marker_path(binary).write_text(
            json.dumps(
                {
                    "sdk_version": claude_agent_sdk.__version__,
                    "size": binary.stat().st_size,
                    "token_offset": token_offset,
                }
            ),
            encoding="utf-8",
        )
    except OSError as e:  # non-fatal: worst case we re-scan next boot
        logger.warning("[CLI patch] Could not write marker %s: %s", _marker_path(binary), e)


def _read_marker(binary: Path) -> dict | None:
    """Parse the sibling marker, or None on any read/parse error."""
    try:
        raw = _marker_path(binary).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _marker_matches_binary(data: dict | None, binary: Path) -> bool:
    """True iff the marker matches the current SDK version + binary size.

    A match means this exact binary was patched by a prior boot — but see
    ``_patched_bytes_present``: because the edit is same-length, the bytes are
    also re-verified before the scan is actually skipped.
    """
    if not data:
        return False
    try:
        return (
            data.get("sdk_version") == claude_agent_sdk.__version__
            and data.get("size") == binary.stat().st_size
        )
    except OSError:
        return False


def _patched_bytes_present(binary: Path, token_offset: int) -> bool:
    """True iff the 3 bytes at ``token_offset`` read as the patched token ``0&&``.

    Cheap warm-path verification: a single ``seek`` + 3-byte read, never the
    257MB scan. Never raises — any error → False so the caller falls through to
    the full scan (safe: the scan re-counts anchors and re-patches or refuses).
    """
    try:
        with open(binary, "rb") as f:
            f.seek(token_offset)
            return f.read(len(_NEW_TOKEN)) == _NEW_TOKEN
    except OSError:
        return False


def patch_cli_preserve_background(binary: Path) -> str:
    """Neutralize the interrupt's kill-all-tasks call site in ``binary``.

    Returns one of:
      * ``"already-patched"`` — the binary already carries the patch (marker hit
        whose recorded ``token_offset`` byte-verifies as ``0&&`` so no scan is
        needed, or a scan found the patched anchor and no unpatched one).
      * ``"patched"``         — exactly one unpatched anchor was found and edited
        in place (3-byte ``seek``+``write``, no full-file rewrite).
      * ``"pattern-shifted"`` — the anchor count was anything other than the two
        safe cases; refuses to edit and logs loudly (SDK likely changed).
      * ``"read-only"``       — the binary/dir could not be opened for writing.
      * ``"unreadable"``      — some other OSError reading the binary.

    Never raises — any unexpected exception is caught, logged, and reported as
    ``"unreadable"`` with ``_PATCH_ACTIVE`` left False. Sets the module
    ``_PATCH_ACTIVE`` flag on any confirmed-patched outcome.
    """
    try:
        return _patch_cli_preserve_background_impl(binary)
    except Exception as e:  # noqa: BLE001 — module contract: NEVER raises
        logger.error(
            "[CLI patch] Unexpected error patching %s: %s", binary, e, exc_info=True
        )
        return "unreadable"


def _patch_cli_preserve_background_impl(binary: Path) -> str:
    global _PATCH_ACTIVE

    # 1) Warm boot: marker matches AND the bytes still carry the patch → skip the
    #    scan. The size-only marker match isn't enough (same-length edit), so
    #    re-verify the 3 token bytes at the recorded offset. A reverted binary or
    #    an old-format marker (no token_offset) falls through to the full scan.
    marker = _read_marker(binary)
    if _marker_matches_binary(marker, binary):
        token_offset = marker.get("token_offset")
        if isinstance(token_offset, int) and _patched_bytes_present(binary, token_offset):
            _PATCH_ACTIVE = True
            logger.info(
                "[CLI patch] Marker current + bytes verified for %s (sdk %s) — "
                "already patched, scan skipped",
                binary,
                claude_agent_sdk.__version__,
            )
            return "already-patched"
        logger.info(
            "[CLI patch] Marker matches size/version for %s but byte "
            "re-verification failed (offset=%r) — falling through to full scan",
            binary,
            token_offset,
        )

    # 2) Read the whole binary and count both anchors.
    try:
        data = binary.read_bytes()
    except (PermissionError,) as e:
        logger.error("[CLI patch] Cannot read %s (permission): %s", binary, e)
        return "read-only"
    except OSError as e:
        logger.error("[CLI patch] Cannot read %s: %s", binary, e)
        return "unreadable"

    matches = list(_ANCHOR_RE.finditer(data))
    n_unpatched = len(matches)
    n_patched = data.count(_PATCHED)

    # 3) Already patched (scan confirms) → refresh the marker and record active.
    if n_patched >= 1 and n_unpatched == 0:
        _PATCH_ACTIVE = True
        # Record where the patched token lives so warm boots can byte-verify.
        _write_marker(binary, data.find(_PATCHED) + _TOKEN_OFFSET)
        logger.info(
            "[CLI patch] %s already carries the patch (%d patched anchor(s)) — "
            "refreshed marker",
            binary,
            n_patched,
        )
        return "already-patched"

    # 4) Exactly one unpatched anchor, none already patched → edit in place.
    if n_unpatched == 1 and n_patched == 0:
        # The regex guarantees the identifier is exactly 3 bytes, so writing the
        # 3-byte ``_NEW_TOKEN`` at token_idx is a same-length, in-place edit.
        token_idx = matches[0].start() + _TOKEN_OFFSET
        try:
            with open(binary, "r+b") as f:
                f.seek(token_idx)
                f.write(_NEW_TOKEN)  # 3 bytes; no full-file rewrite
                f.flush()
                import os

                os.fsync(f.fileno())
        except PermissionError as e:
            logger.error("[CLI patch] Cannot write %s (read-only): %s", binary, e)
            return "read-only"
        except OSError as e:
            logger.error("[CLI patch] Write failed for %s: %s", binary, e)
            return "unreadable"

        _PATCH_ACTIVE = True
        _write_marker(binary, token_idx)
        logger.info(
            "[CLI patch] Patched %s at offset %d (interrupt no longer kills "
            "background sub-agents)",
            binary,
            token_idx,
        )
        return "patched"

    # 5) Anything else: the anchor shifted between SDK versions. NEVER edit —
    #    guessing could corrupt an unrelated bare-token site.
    logger.error(
        "[CLI patch] Anchor pattern shifted in %s (unpatched=%d, patched=%d, "
        "tokens=%r, sdk %s) — refusing to edit. Interrupt will still kill "
        "background sub-agents until the anchor is re-verified.",
        binary,
        n_unpatched,
        n_patched,
        [m.group(1) for m in matches],
        claude_agent_sdk.__version__,
    )
    return "pattern-shifted"
