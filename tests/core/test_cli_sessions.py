"""Offline contracts for the owned CLI transcript implementation."""
import json
import uuid

import pytest

from app.core import cli_sessions as cs


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from app.core.config import settings
    monkeypatch.setattr(settings, "claude_projects_dir", None)
    sid = str(uuid.uuid4())
    project = cs._projects() / cs.project_key_for_directory(str(tmp_path))
    project.mkdir(parents=True)
    path = project / f"{sid}.jsonl"
    records = [
        {"type": "user", "uuid": "u", "parentUuid": None,
         "timestamp": "2024-01-01T00:00:00Z", "cwd": str(tmp_path), "gitBranch": "main",
         "message": {"role": "user", "content": "Hello"}},
        {"type": "assistant", "uuid": "old", "parentUuid": "u",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "abandoned"}]}},
        {"type": "assistant", "uuid": "a", "parentUuid": "u",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}},
        {"type": "summary", "summary": "Summary", "leafUuid": "a"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in records) + "\n{torn", encoding="utf-8")
    return sid, path, records


def test_fold_golden_and_incremental(transcript):
    sid, path, records = transcript
    key = {"session_id": sid, "project_key": path.parent.name}
    expected = {"is_sidechain": False, "created_at": 1704067200000,
                "cwd": records[0]["cwd"], "first_prompt": "Hello",
                "first_prompt_locked": True, "git_branch": "main", "summary_hint": "Summary"}
    summary = cs.fold_session_summary(None, key, records)
    assert summary == {"session_id": sid, "mtime": 0, "data": expected}
    incremental = None
    for entry in records:
        incremental = cs.fold_session_summary(incremental, key, [entry])
    assert incremental == summary


def test_metadata_messages_subagents_and_delete(transcript):
    sid, path, records = transcript
    assert [m.uuid for m in cs.get_session_messages(sid)] == ["u", "a"]
    assert [m.uuid for m in cs.get_session_messages(sid, offset=1, limit=1)] == ["a"]
    # Producer -> disk -> discovery: no fixture seeds renamed metadata.
    cs.rename_session(sid, " New title ")
    cs.tag_session(sid, "review")
    info = cs.get_session_info(sid)
    assert info.custom_title == info.summary == "New title"
    assert info.tag == "review" and info.first_prompt == "Hello"
    assert info.created_at == 1704067200000
    assert cs.list_sessions(include_worktrees=False) == [info]
    cs.tag_session(sid, None)
    assert cs.get_session_info(sid).tag == ""
    sub = path.with_suffix("") / "subagents" / "agent-child.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    assert cs.list_subagents(sid) == ["child"]
    assert cs.get_subagent_messages(sid, "child")[0].message == records[0]["message"]
    cs.delete_session(sid)
    assert not path.exists() and not sub.exists()
    assert cs.get_session_info(sid) is None


def test_no_credentials_or_outside_transcripts_opened(transcript, monkeypatch, tmp_path):
    sid, path, _ = transcript
    from pathlib import Path
    original = Path.open
    opened = []
    def checked(self, *args, **kwargs):
        assert self.resolve().is_relative_to(cs._projects().resolve())
        opened.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked)
    assert cs.get_session_info(sid)
    assert cs.list_sessions(include_worktrees=False)
    cs.rename_session(sid, "safe")
    assert opened
    for invalid in ("../secret", "../../.credentials", "/etc/passwd"):
        with pytest.raises(ValueError):
            cs.get_session_info(invalid)
    outside = tmp_path / "outside.jsonl"
    alias = path.parent / f"{uuid.uuid4()}.jsonl"
    alias.symlink_to(outside)
    assert cs.get_session_info(alias.stem) is None
    with pytest.raises(ValueError):
        cs.get_subagent_messages(sid, "../../credentials")


def test_project_key_and_worktree_discovery(transcript, monkeypatch):
    sid, path, records = transcript
    from types import SimpleNamespace
    cwd = records[0]["cwd"]
    called = []
    def git(argv, **kwargs):
        called.append(argv)
        return SimpleNamespace(stdout=f"worktree {cwd}\nHEAD fixture\n")
    monkeypatch.setattr(cs.subprocess, "run", git)
    assert [info.session_id for info in cs.list_sessions(cwd + "/worktree")] == [sid]
    assert called[0][-3:] == ["worktree", "list", "--porcelain"]
    assert cs.project_key_for_directory(cwd) == path.parent.name

def test_summary_command_fallback_and_prompt_truncation():
    key = {"session_id": "s", "project_key": "p"}
    entries = [{"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
               {"type": "user", "isMeta": True, "message": {"content": "ignore"}},
               {"type": "user", "message": {"content": "a" * 250}},
               {"type": "tag", "tag": None}]
    result = cs.fold_session_summary(None, key, entries)["data"]
    assert result == {"is_sidechain": False, "command_fallback": "/clear",
                      "first_prompt": "a" * 200 + "…", "first_prompt_locked": True}

@pytest.mark.parametrize("sidechain", [False, True])
def test_message_chain_crosses_system_records_but_hides_sidechains(transcript, sidechain):
    sid, path, records = transcript
    records = [records[0], records[2],
               {"type": "system", "uuid": "system", "parentUuid": "a"},
               {"type": "assistant", "uuid": "b", "parentUuid": "system",
                "message": {"role": "assistant", "content": "after system"}, "isSidechain": sidechain}]
    path.write_text("\n".join(json.dumps(e) for e in records), encoding="utf-8")
    expected = ["u", "a"] if sidechain else ["u", "a", "b"]
    assert [m.uuid for m in cs.get_session_messages(sid)] == expected
    assert [m.uuid for m in cs.get_session_messages(sid, limit=0)] == expected
    sub = path.with_suffix("") / "subagents" / "agent-child.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(path.read_text(), encoding="utf-8")
    assert [m.uuid for m in cs.get_subagent_messages(sid, "child")] == ["u", "a", "b"]

@pytest.mark.parametrize(("path", "expected"), [
    ("/tmp/a.b_c (d)", "-tmp-a-b-c--d-"),
    ("/tmp/" + "a" * 250, "-tmp-" + "a" * 195 + "-bxbzwn"),
    ("/tmp/" + "b" * 250, "-tmp-" + "b" * 195 + "-admrbt"),
    ("/tmp/" + "x" * 195, "-tmp-" + "x" * 195),
    ("/tmp/" + "x" * 196, "-tmp-" + "x" * 195 + "-diaimx"),
    ("/tmp/" + "e\u0301" * 110, "-tmp-" + "-" * 110),
])
def test_cli_project_key_golden_vectors(path, expected, monkeypatch):
    # These are POSIX path vectors, independent of the test host's drive/root.
    from pathlib import Path
    monkeypatch.setattr(Path, "resolve", lambda self: self)
    assert cs.project_key_for_directory(path) == expected

@pytest.mark.parametrize("damage", [
    b'{"type":"assistant","message":{"content":"\xff"}}\n',
    b'{"type":"assistant","message":{"content":"\xe2\x82',
    b'{"type":"assistant","message":',
])
def test_damaged_utf8_or_torn_records_preserve_other_records_and_sessions(transcript, damage):
    sid, path, records = transcript
    valid = b"\n".join(json.dumps(e).encode() for e in records) + b"\n"
    path.write_bytes(valid)
    cs.rename_session(sid, "durable title")
    cs.tag_session(sid, "durable tag")
    good_sid = str(uuid.uuid4())
    good = path.with_name(f"{good_sid}.jsonl")
    good.write_bytes(valid)
    with path.open("ab") as stream:
        stream.write(damage)
    info = cs.get_session_info(sid)
    assert info.first_prompt == "Hello" and info.custom_title == "durable title"
    assert info.tag == "durable tag"
    assert {s.session_id for s in cs.list_sessions(include_worktrees=False)} == {sid, good_sid}
    assert [m.uuid for m in cs.get_session_messages(sid)] == ["u", "a"]
    # A later valid producer record still survives a damaged middle record.
    cs.rename_session(sid, "after damage")
    assert cs.get_session_info(sid).custom_title == "after damage"


@pytest.mark.parametrize("body_bytes", [256 * 1024, 8 * 1024 * 1024])
def test_discovery_reads_bounded_windows_not_the_large_middle(transcript, monkeypatch, body_bytes):
    from pathlib import Path
    sid, path, records = transcript
    with path.open("wb") as stream:
        stream.write(json.dumps(records[0]).encode() + b"\n")
        stream.write(b'{"type":"assistant","message":{"content":"')
        for _ in range(body_bytes // 1024):
            stream.write(b"x" * 1024)
        stream.write(b'"}}\n')
    cs.rename_session(sid, "large transcript")
    cs.tag_session(sid, "latest tag")
    # Instrument real file I/O rather than seed an in-memory metadata cache.
    original_open = Path.open
    reads = []
    class BoundedReader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def seek(self, *args): return self.stream.seek(*args)
        def tell(self): return self.stream.tell()
        def read(self, size=-1):
            assert 0 <= size <= cs._METADATA_WINDOW_BYTES + 1
            data = self.stream.read(size)
            reads.append(len(data))
            return data
        def __iter__(self):
            pytest.fail("discovery must not iterate complete histories")
    def opened(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        return BoundedReader(stream) if self == path else stream
    monkeypatch.setattr(Path, "open", opened)
    sessions = cs.list_sessions(include_worktrees=False)
    assert len(sessions) == 1
    assert sessions[0].first_prompt == "Hello"
    assert sessions[0].custom_title == "large transcript"
    assert sessions[0].tag == "latest tag"
    assert sessions[0].file_size == path.stat().st_size
    assert sum(reads) <= 2 * cs._METADATA_WINDOW_BYTES + 1


@pytest.mark.parametrize("size", [32, 33, 64, 65])
def test_metadata_window_boundaries_keep_only_complete_records(tmp_path, monkeypatch, size):
    # Small windows make the overlapping/contiguous/separated boundary cases
    # explicit without allocating a large fixture per boundary.
    monkeypatch.setattr(cs, "_METADATA_WINDOW_BYTES", 32)
    path = tmp_path / "boundary.jsonl"
    first = b'{"n":1}\n'
    last = b'{"n":2}'  # a complete final line doesn't require a newline
    path.write_bytes(first + b"\n" * (size - len(first) - len(last)) + last)
    assert list(cs._metadata_entries(path)) == [{"n": 1}, {"n": 2}]


def test_metadata_windows_do_not_decode_fragments_as_records(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_METADATA_WINDOW_BYTES", 32)
    path = tmp_path / "boundary.jsonl"
    # Both window edges land inside multibyte text in an oversized record.
    path.write_bytes(b'{"n":1}\n{"long":"' + "é".encode() * 100 + b'"}\n{"n":2}\n')
    assert list(cs._metadata_entries(path)) == [{"n": 1}, {"n": 2}]
    assert list(cs._entries(path))[1] == {"long": "é" * 100}
