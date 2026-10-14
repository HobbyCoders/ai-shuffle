"""Edge cases for the URL credential scrubbers in app.core.scrub.

Per the WHATWG URL parser (and git, curl and libpq), the host starts after
the LAST ``@`` of the authority, so a raw ``@`` in a password belongs to the
userinfo. A scrubber that stops at the first ``@`` leaves the rest of the
password in the log, the database row or the API response.
"""

from __future__ import annotations

import pytest

from app.core.scrub import (
    redact_url_credentials,
    scrub_url_credentials,
    scrub_url_credentials_text,
    strip_url_userinfo,
)


# ── redact_url_credentials (git argv, stderr, remotes) ───────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ssh://user:pw@host/r.git", "ssh://***@host/r.git"),
        ("ssh://git@host/r.git", "ssh://***@host/r.git"),
        ("https://u:p@[::1]:8443/x", "https://***@[::1]:8443/x"),
        ("https://u:p%40ss%3Aw@host/r", "https://***@host/r"),
        ("https://u:p@ss@host/r", "https://***@host/r"),
        ("https://u:p@s@s@host:8443/r", "https://***@host:8443/r"),
        ("https://tok@host/path@v1", "https://***@host/path@v1"),
        ("https://tok@host?next=a@b", "https://***@host?next=a@b"),
        ("https://tok@host#frag@x", "https://***@host#frag@x"),
        (
            "fetch https://a:1@h1/r then https://b@h2/r and https://h3/r",
            "fetch https://***@h1/r then https://***@h2/r and https://h3/r",
        ),
        ("'https://u:p@ss@h/r'", "'https://***@h/r'"),
    ],
)
def test_redact_replaces_the_whole_userinfo(raw, expected):
    assert redact_url_credentials(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "git@github.com:o/r.git",
        "deploy@git.example.com:/srv/r.git",
        "https://host/path@v1",
        "https://host/r?next=a@b",
        "https://[::1]:8443/x",
        "mail ops@example.com about https://example.com/x",
        "user@host and more text",
        "",
    ],
)
def test_redact_leaves_non_credentials_alone(raw):
    assert redact_url_credentials(raw) == raw


@pytest.mark.parametrize("value", [None, 5, b"https://u:p@h/r", ["https://u:p@h"]])
def test_redact_passes_non_strings_through(value):
    assert redact_url_credentials(value) is value


# ── strip_url_userinfo (stored and displayed remotes) ────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://u:p@ss@host/r.git", "https://host/r.git"),
        ("https://u:p%40ss@host/r.git", "https://host/r.git"),
        ("https://tok@[::1]:8443/r.git", "https://[::1]:8443/r.git"),
        ("https://host/r@v1.git", "https://host/r@v1.git"),
        ("https://host/r?ref=a@b", "https://host/r?ref=a@b"),
        # ssh with a password is a secret like any other...
        ("ssh://git:pw@host/r.git", "ssh://host/r.git"),
        ("ssh://alice:s3cr3t@host/r.git", "ssh://host/r.git"),
        ("git+ssh://u:pw@host/r.git", "git+ssh://host/r.git"),
        ("ssh+git://u:pw@host/r.git", "ssh+git://host/r.git"),
        ("ssh://git:p@w@host/r.git", "ssh://host/r.git"),
        # ...but an ssh login name without one is kept.
        ("ssh://git@host/r.git", "ssh://git@host/r.git"),
        ("ssh://alice@host:2222/r.git", "ssh://alice@host:2222/r.git"),
        ("git+ssh://git@host/r.git", "git+ssh://git@host/r.git"),
        ("SSH://git@host/r.git", "SSH://git@host/r.git"),
        # scp-like remotes carry no scheme and are never touched.
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("alice@host:path/r.git", "alice@host:path/r.git"),
        # A scheme that merely ends in "ssh" is not ssh.
        ("xssh://tok@host/r", "xssh://host/r"),
    ],
)
def test_strip_userinfo(raw, expected):
    assert strip_url_userinfo(raw) == expected


@pytest.mark.parametrize("value", [None, 0, {"u": "https://t@h"}])
def test_strip_passes_non_strings_through(value):
    assert strip_url_userinfo(value) is value


# ── scrub_url_credentials_text (transcript write path) ───────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgres://u:p@ss@db:5432/app", "postgres://u:***@db:5432/app"),
        ("redis://:p@ss@cache:6379/0", "redis://:***@cache:6379/0"),
        ("postgres://u:p%40ss@db/app", "postgres://u:***@db/app"),
        ("https://u:p@[::1]:8443/x", "https://u:***@[::1]:8443/x"),
        ("ssh://user:pw@host/r", "ssh://user:***@host/r"),
        (
            "a=postgres://u:x@y@h1/d b=mysql://r:z@h2/d",
            "a=postgres://u:***@h1/d b=mysql://r:***@h2/d",
        ),
    ],
)
def test_scrub_text_redacts_the_whole_password(raw, expected):
    assert scrub_url_credentials_text(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r.git",
        "https://host/path@v1",
        "https://example.com:8443/users/@me",
        "contact ops@example.com, http://localhost:8080 is local",
        '{"a":"http://x.com:80","b":"u@h"}',
    ],
)
def test_scrub_text_leaves_look_alikes_alone(raw):
    assert scrub_url_credentials_text(raw) is raw


def test_scrub_recursive_with_at_in_password():
    payload = {"cells": ["postgres://u:p@ss@h/d", {"k": "ok"}], "n": 1}
    assert scrub_url_credentials(payload) == {
        "cells": ["postgres://u:***@h/d", {"k": "ok"}],
        "n": 1,
    }


# ── shape of the match and cost ──────────────────────────────────────────────


def test_leading_non_letters_before_a_scheme_are_kept():
    assert scrub_url_credentials_text("1https://u:p@h ..ab://x:y@h") == (
        "1https://u:***@h ..ab://x:***@h"
    )
    assert redact_url_credentials("(1https://tok@h)") == "(1https://***@h)"


@pytest.mark.parametrize(
    "blob",
    ["3f2a9b" * 100_000, "a" * 600_000, "1.2-" * 150_000, "x://" + "a:" * 300_000],
    # pytest exports the node id in PYTEST_CURRENT_TEST. Auto-ids embed the
    # entire blob and exceed Windows' environment-variable size limit.
    ids=["hex", "letters", "punctuation", "colon-chain"],
)
def test_long_tokens_scan_in_linear_time(blob, request):
    assert len(request.node.nodeid) < 32767, "PYTEST_CURRENT_TEST must fit on Windows"
    # A 60 KB hex blob took over a second per call on the transcript write
    # path while every position restarted the scheme scan (quadratic).
    import time

    start = time.perf_counter()
    for fn in (scrub_url_credentials_text, redact_url_credentials, strip_url_userinfo):
        assert fn(blob) == blob
    assert time.perf_counter() - start < 2.0
