"""The fake Claude Code CLI's replay fixtures stay synthetic and well formed.

The fixtures in ``scripts/devloop/fake_claude/compaction/`` (and the CLI's
scenarios) were derived from real Claude Code output on a worker, and this
repository is public. Only the shape, the order and the sizes may carry over:
no ids, paths, accounts, tokens or anything naming the owner's setup. Every id
in a fixture is a ``$placeholder`` the fake fills in at run time.

The owner-specific words are checked by hash, so this test does not publish
them either.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.devloop.fake_claude import COMPACTION_DIR, SCENARIOS_DIR
from scripts.devloop.fake_claude import cli as fake_cli

FIXTURES = sorted(COMPACTION_DIR.glob("*.json")) + sorted(SCENARIOS_DIR.glob("*.json"))

LEAKS = {
    "a home or machine path": re.compile(
        r"(/home/|/Users/|/root/|/workspace/|/data/|/mnt/|/var/|[A-Za-z]:\\\\|\\\\Users\\\\)"),
    "an email address": re.compile(r"[\w.+-]+@(?!devloop\.invalid\b)[\w-]+\.[\w.-]+"),
    "an API key or token": re.compile(
        r"(sk-ant-|sk-[A-Za-z0-9]{16}|ghp_|gho_|github_pat_|xox[abpr]-|AKIA[0-9A-Z]{12}|"
        r"Bearer\s+[A-Za-z0-9._-]{8}|eyJ[A-Za-z0-9_-]{10,}\.)"),
    "a literal uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                                 re.IGNORECASE),
    "a real-looking CLI or app id": re.compile(
        r"\b(toolu_[A-Za-z0-9]{8,}|msg_0[A-Za-z0-9]{8,}|req_[A-Za-z0-9]{8,}|cmp_[0-9a-f]{12}|"
        r"ses_[0-9a-f]{12}|node_[0-9a-f]{12})"),
    "a private network address": re.compile(r"\b(10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    # Random-looking: digits, upper and lower case in one 32+ character run
    # (a tool name such as mcp__server__ListAgents has no digits).
    "a long secret-like string": re.compile(
        r"(?=[A-Za-z0-9+/=_-]*\d)(?=[A-Za-z0-9+/=_-]*[A-Z])(?=[A-Za-z0-9+/=_-]*[a-z])"
        r"\b[A-Za-z0-9+/=_-]{32,}"),
}

#: sha256 (first 16 hex digits) of words that name the owner, their machines,
#: their projects or their communities, lower-cased.
OWNER_WORDS = {
    "0345573f9908b378", "c6d48d605a8f97b4", "7addef03d3b8a076", "168c8c3ab9bff61e",
    "97ad511d79da16df", "5bcd4e8b7a2d2607", "3fa8118697f94444", "71e2d0b692936e3b",
    "f959181ac4a0912f", "73917113cee8dd13", "6baff1894abb437c", "8be0b5445fca6eb1",
    "5c7c54798401e338", "5aa05d4ec7d3085c", "1763c2c2c410767e", "d2ba4ba1400bae4d",
    "d02c355d9e87764f",
}
MAX_FIXTURE_BYTES = 20_000


def _phrases(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9-]*", text.lower())
    return {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}


def _digest(phrase: str) -> str:
    return hashlib.sha256(phrase.encode()).hexdigest()[:16]


def _names_owner(text: str, words: set[str]) -> bool:
    return any(_digest(phrase) in words for phrase in _phrases(text))


def test_the_owner_word_check_itself_works():
    # A stand-in word (never an owner word in plain text: the repo-wide guard,
    # tests/test_public_release_files.py, scans this file too).
    words = OWNER_WORDS | {_digest("zebra lantern")}
    assert _names_owner("A chat on the Zebra Lantern box.", words)
    assert not _names_owner("A zebra and a lantern.", words)
    assert not _names_owner("Fake Claude Code CLI here.", OWNER_WORDS)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_fixture_carries_nothing_of_the_owner(path: Path):
    text = path.read_text(encoding="utf-8")
    assert len(text.encode()) <= MAX_FIXTURE_BYTES, "a fixture is a shape, not a recording"
    for what, pattern in LEAKS.items():
        found = pattern.search(text)
        assert found is None, f"{path.name} holds {what}: {found.group(0)[:12]}…"
    assert not _names_owner(text, OWNER_WORDS), f"{path.name} names the owner's setup"


def _leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


ID_KEYS = re.compile(r"(uuid|_id$|Uuid$|^id$|parentUuid|sessionId)")


def _id_values(value, key=""):
    if isinstance(value, dict):
        for k, item in value.items():
            yield from _id_values(item, k)
    elif isinstance(value, list):
        for item in value:
            yield from _id_values(item, key)
    elif ID_KEYS.search(key) and value is not None:
        yield key, value


@pytest.mark.parametrize("path", sorted(COMPACTION_DIR.glob("*.json")), ids=lambda p: p.name)
def test_a_compaction_fixture_is_well_formed_and_every_id_is_a_placeholder(path: Path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert path.stem == f"{fixture['trigger']}-{fixture['outcome']}"
    assert fixture["description"] and fixture["evidence"]
    kinds = {"bridge", "hook", "frame", "transcript", "apply", "summarize", "replay", "result"}
    for step in fixture["sequence"]:
        assert len(step.keys() & kinds) == 1, step
    assert sum(1 for step in fixture["sequence"] if step.get("bridge")) == 1
    for key, value in _id_values(fixture["sequence"]):
        assert isinstance(value, str) and value.startswith("$"), f"{key}={value!r} is not a placeholder"
    placeholders = {m for leaf in _leaves(fixture["sequence"]) if isinstance(leaf, str)
                    for m in re.findall(r"\$([a-z_]+)", leaf)}
    known = set(fake_cli._FRESH) | {
        "sid", "logical_parent", "pre_tokens", "post_tokens", "dropped_tokens", "duration_ms",
        "boundary_uuid", "summary_uuid", "summary", "reason", "instructions",
        "preserved_uuids", "preserved_head", "preserved_tail"}
    assert placeholders <= known, placeholders - known


@pytest.mark.parametrize("trigger", ["auto", "manual"])
def test_what_prints_before_the_mods_hook_is_the_same_for_every_outcome(trigger):
    """The fake prints this part before it knows the bridge's answer."""
    preludes = {path.stem: fake_cli.split_at_bridge(json.loads(path.read_text()))[0]
                for path in COMPACTION_DIR.glob(f"{trigger}-*.json")}
    assert len(preludes) >= 2 and len({json.dumps(p) for p in preludes.values()}) == 1


def test_placeholders_bind_whole_values_and_text_and_drop_unkept_fields():
    ctx = {"pre_tokens": 1200, "reason": "nothing", "preserved_uuids": []}
    bound = fake_cli.bind({"a": "$pre_tokens", "b": "skipped: $reason", "c": ["$uuid", "$uuid"],
                           "preserved_messages": {"uuids": "$preserved_uuids"}}, ctx)
    assert bound["a"] == 1200 and bound["b"] == "skipped: nothing"
    assert bound["c"][0] != bound["c"][1]  # fresh at every use
    assert "preserved_messages" not in bound
