"""URL-credential scrubbing on the fluid store's write path (issue R12).

A model pasted a full ``DATABASE_URL`` into a code-mode cell and the password
was persisted verbatim in ``fluid_session_nodes``. :mod:`app.core.scrub` now
redacts the password of URL-embedded credentials as the payload crosses into
jsonb.

The first half is pure-function: no DB, no engine. The second half runs the
real ``PgFluidStore`` against the pg_shim (``mock_db``) to prove the hook is
actually wired on BOTH durable tables — nodes and the event log — and that the
redaction is what survives a cold reload, not just what the cache holds.

The false-negative direction matters as much as the false-positive one: git
remotes, host:port URLs, and password-less userinfo must come out byte
identical, because mangling them would corrupt ordinary transcript text.
"""

from __future__ import annotations

import pytest

from app.core.scrub import scrub_url_credentials, scrub_url_credentials_text

from fluid.events.model import TextDelta, ToolUse
from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode

from app.core.provider.fluid_pg_store import PgFluidStore


# ─── Scrubbed: real URL-embedded credentials ─────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The exact R12 shape.
        (
            "postgres://appuser:s3cr3t@db.internal:5432/appdb",
            "postgres://appuser:***@db.internal:5432/appdb",
        ),
        (
            "postgresql://appuser:s3cr3t@db.internal:5432/appdb",
            "postgresql://appuser:***@db.internal:5432/appdb",
        ),
        # SQLAlchemy-style dialect+driver scheme.
        (
            "postgresql+psycopg2://u:p@h/db",
            "postgresql+psycopg2://u:***@h/db",
        ),
        # Other schemes get the same treatment — the rule is about the URL
        # shape, not about Postgres specifically.
        ("mysql://root:root@127.0.0.1:3306/db", "mysql://root:***@127.0.0.1:3306/db"),
        ("amqp://guest:guest@rabbit:5672/", "amqp://guest:***@rabbit:5672/"),
        ("https://bot:hunter2@api.example.com/v1", "https://bot:***@api.example.com/v1"),
        # Password-only userinfo (empty user) — the common redis form.
        ("redis://:pw@cache:6379/0", "redis://:***@cache:6379/0"),
        # Punctuation-heavy password.
        (
            "postgres://u:p!$a-b_c.d~9@h/db",
            "postgres://u:***@h/db",
        ),
        # Only the password goes; everything else stays readable.
        (
            "DATABASE_URL=postgres://svc_acct:Tr0ub4dor@pg-primary.prod:5432/shuffle?sslmode=require",
            "DATABASE_URL=postgres://svc_acct:***@pg-primary.prod:5432/shuffle?sslmode=require",
        ),
    ],
)
def test_password_is_redacted(raw: str, expected: str):
    assert scrub_url_credentials_text(raw) == expected


def test_two_credentials_on_one_line_both_go():
    raw = "primary postgres://a:one@h1/db replica postgres://b:two@h2/db"
    assert scrub_url_credentials_text(raw) == (
        "primary postgres://a:***@h1/db replica postgres://b:***@h2/db"
    )


def test_credential_inside_surrounding_prose_and_quotes():
    raw = 'conn = psycopg2.connect("postgres://u:pw@h:5432/d")  # local only'
    assert scrub_url_credentials_text(raw) == (
        'conn = psycopg2.connect("postgres://u:***@h:5432/d")  # local only'
    )


# ─── Untouched: everything that only LOOKS like a credential ─────────────────


@pytest.mark.parametrize(
    "raw",
    [
        # ssh-style git remotes: no "://" at all, so nothing can match. Mangling
        # these would break every pasted clone command in a transcript.
        "git@github.com:HobbyCoders/ai-shuffle.git",
        "git remote add origin git@github.com:HobbyCoders/ai-shuffle.git",
        "user@host:/srv/path",
        # scheme:// git remotes whose userinfo carries no password.
        "ssh://git@github.com/HobbyCoders/ai-shuffle.git",
        "https://github.com/HobbyCoders/ai-shuffle.git",
        # userinfo with a user but no password — there is no ":" to redact at.
        "http://user@host/path",
        "https://token@gitlab.example.com/group/repo.git",
        # host:port, the biggest false-positive risk. The "/" (or the end of the
        # string) terminates the candidate before any "@" can close it.
        "http://localhost:8080/api/v1",
        "http://localhost:8080",
        "https://db.internal:5432/appdb",
        "postgres://db.internal:5432/appdb",
        "redis://cache:6379/0",
        # Bare colons and emails in ordinary prose.
        "note: the password is stored in the vault, ask ops@example.com",
        "ratio 3:1, see docs at https://example.com/guide",
        "TODO: fix timing (took 12:34) — ping me@work.example",
        # Minified JSON: a greedy password must not run across the string
        # boundary and grab the unrelated "@" from the next value.
        '{"a":"http://x.com:80","b":"u@h"}',
        # Windows path and a Python dict repr.
        r"C:\Users\dev\project",
        "{'url': 'http://svc:8080/health', 'owner': 'a@b.com'}",
        # Empty password: nothing to redact, and rewriting it would only add
        # a secret-looking marker where no secret exists.
        "postgres://user:@host/db",
    ],
)
def test_non_credentials_pass_through_untouched(raw: str):
    out = scrub_url_credentials_text(raw)
    assert out == raw
    # The no-match hot path must not copy.
    assert out is raw


# ─── Recursive walk ──────────────────────────────────────────────────────────


def test_nested_structures_are_scrubbed_recursively():
    payload = {
        "role": "assistant",
        "content": [
            {
                "kind": "tool_use",
                "name": "codemode",
                "input": {
                    "code": "engine = create_engine('postgres://u:hunter2@db/app')",
                    "notes": ["also try mysql://root:toor@h/x", "clean line"],
                },
            },
            {"kind": "text", "text": "cloned git@github.com:HobbyCoders/ai-shuffle.git"},
        ],
    }
    out = scrub_url_credentials(payload)

    code = out["content"][0]["input"]["code"]
    assert "hunter2" not in code
    assert code == "engine = create_engine('postgres://u:***@db/app')"
    assert out["content"][0]["input"]["notes"][0] == "also try mysql://root:***@h/x"
    assert out["content"][0]["input"]["notes"][1] == "clean line"
    # Structure survives, and the git remote is not mangled.
    assert out["role"] == "assistant"
    assert out["content"][0]["name"] == "codemode"
    assert out["content"][1]["text"].endswith("git@github.com:HobbyCoders/ai-shuffle.git")


def test_dict_keys_are_left_alone():
    # Keys are structure the message/event codecs look up by name; a rewritten
    # key would silently break decoding. Secrets land in values.
    out = scrub_url_credentials({"postgres://u:p@h": "postgres://u:p@h"})
    assert list(out.keys()) == ["postgres://u:p@h"]
    assert out["postgres://u:p@h"] == "postgres://u:***@h"


def test_non_string_scalars_pass_through():
    assert scrub_url_credentials(42) == 42
    assert scrub_url_credentials(3.14) == 3.14
    assert scrub_url_credentials(True) is True
    assert scrub_url_credentials(None) is None


# ─── Wiring: PgFluidStore's two durable tables ───────────────────────────────

_SECRET_URL = "postgres://appuser:s3cr3t@db.internal:5432/appdb"


@pytest.mark.asyncio
async def test_node_write_redacts_and_the_redaction_is_what_reloads(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_scrub_node")
    await store.append(
        "ses_scrub_node",
        SessionNode(
            node_id="n1",
            parent_id=None,
            message=Message(
                role="user",
                content=[Text(text=f"run this: {_SECRET_URL}")],
            ),
        ),
    )

    # Cold cache: this is what a resume, an export, or a recall search sees.
    reloaded = PgFluidStore()
    path = await reloaded.load_path("ses_scrub_node")
    text = path[0].message.text()
    assert "s3cr3t" not in text
    assert text == "run this: postgres://appuser:***@db.internal:5432/appdb"


@pytest.mark.asyncio
async def test_node_write_leaves_a_git_remote_alone(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_scrub_git")
    remote = "git clone git@github.com:HobbyCoders/ai-shuffle.git"
    await store.append(
        "ses_scrub_git",
        SessionNode(
            node_id="n1",
            parent_id=None,
            message=Message(role="user", content=[Text(text=remote)]),
        ),
    )

    reloaded = PgFluidStore()
    path = await reloaded.load_path("ses_scrub_git")
    assert path[0].message.text() == remote


@pytest.mark.asyncio
async def test_event_log_gets_the_same_scrub(mock_db):
    # Cell text reaches fluid_session_events too: ToolUse.input carries the
    # code-mode cell, TextDelta.text carries the assistant's prose.
    store = PgFluidStore()
    await store.create_session("ses_scrub_ev")
    await store.append_event(
        "ses_scrub_ev",
        ToolUse(
            session_id="ses_scrub_ev",
            seq=1,
            tool_use_id="tu_1",
            name="codemode",
            input={"code": f"db = connect('{_SECRET_URL}')"},
        ),
    )
    await store.append_event(
        "ses_scrub_ev",
        TextDelta(
            session_id="ses_scrub_ev",
            seq=2,
            index=0,
            text=f"connecting to {_SECRET_URL} now",
        ),
    )

    reloaded = PgFluidStore()
    events = await reloaded.events_since("ses_scrub_ev", 0)
    assert len(events) == 2
    assert "s3cr3t" not in events[0].input["code"]
    assert events[0].input["code"] == "db = connect('postgres://appuser:***@db.internal:5432/appdb')"
    assert "s3cr3t" not in events[1].text
    assert events[1].text.startswith("connecting to postgres://appuser:***@")
