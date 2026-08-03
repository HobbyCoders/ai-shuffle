"""Tests for app/core/working_set.py — the V2 Phase 4 (Q6) working-set sync.

Real git throughout (a work repo + a ``git init --bare`` origin wired via a
``file://`` URL) — git itself is never mocked. The only seam is the DB setter
``sessions.set_working_set_ref``, replaced with an in-process recorder so the
column write can be asserted without a database. The session_runtime hook
flag-gating (case 10) is checked by driving the runtime's own hook coroutines
with the flag off and asserting the working_set functions are never called.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import working_set

SID = "sess-q6"
REF = f"refs/aishuffle/sessions/{SID}"


# ---------------------------------------------------------------------------
# git helpers / fixtures (real repos, no git mocking)
# ---------------------------------------------------------------------------

def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _configure(repo):
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


def _init_work(path):
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _configure(path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


def _clone(url, dest):
    _git(dest.parent, "clone", url, dest.name)
    _configure(dest)
    return dest


def _porcelain(repo):
    return _git(repo, "status", "--porcelain").stdout


def _has_ref(repo, ref=REF):
    return _git(repo, "rev-parse", ref, check=False).returncode == 0


@pytest.fixture
def env(tmp_path):
    """A work repo pushed to a bare ``origin`` over ``file://``."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    _init_work(work)
    url = origin.as_uri()
    _git(work, "remote", "add", "origin", url)
    _git(work, "push", "origin", "main")
    return SimpleNamespace(origin=origin, work=work, url=url, tmp=tmp_path)


class _RefStore:
    """Stand-in for ``sessions.set_working_set_ref`` — records every write."""

    UNSET = object()

    def __init__(self):
        self.value = self.UNSET
        self.calls = []

    def __call__(self, session_id, value):
        self.calls.append((session_id, value))
        self.value = value
        return True

    def wrote_none(self):
        return any(v is None for _, v in self.calls)


@pytest.fixture
def ref_store(monkeypatch):
    store = _RefStore()
    monkeypatch.setattr(working_set.sessions_db, "set_working_set_ref", store)
    return store


# ---------------------------------------------------------------------------
# snapshot_out
# ---------------------------------------------------------------------------

def test_snapshot_dirty_tracked_creates_scratch_ref(env, ref_store):
    """Case 1: a dirty tracked change → scratch ref in the BARE origin, tree
    byte-identical before/after, column JSON carries ref/sha/runner_id."""
    (env.work / "README.md").write_text("hello world\n", encoding="utf-8")
    before_status = _porcelain(env.work)
    before_content = (env.work / "README.md").read_text(encoding="utf-8")

    working_set.snapshot_out(SID, env.work, "runnerA", False)

    # Ref landed in the bare origin.
    origin_ref = _git(env.origin, "rev-parse", REF, check=False)
    assert origin_ref.returncode == 0
    # Tree byte-identical — stash create must not touch the working tree.
    assert _porcelain(env.work) == before_status
    assert (env.work / "README.md").read_text(encoding="utf-8") == before_content
    # Column JSON.
    assert ref_store.value not in (ref_store.UNSET, None)
    meta = json.loads(ref_store.value)
    assert meta["ref"] == REF
    assert meta["runner_id"] == "runnerA"
    assert meta["sha"] == origin_ref.stdout.strip()


def test_snapshot_clean_tree_is_noop(env, ref_store):
    """Case 2: clean tree → no ref, no column write, nothing pushed."""
    working_set.snapshot_out(SID, env.work, "runnerA", False)
    assert not _has_ref(env.origin)
    assert ref_store.calls == []
    assert ref_store.value is ref_store.UNSET


def test_snapshot_ships_unpushed_commit_objects(env, ref_store):
    """Case 3: an unpushed local commit under the dirty change → the bare origin
    receives that commit's objects (rev-parse of the stash parent succeeds in a
    fresh clone)."""
    (env.work / "file.txt").write_text("v1\n", encoding="utf-8")
    _git(env.work, "add", "-A")
    _git(env.work, "commit", "-m", "local-unpushed")
    unpushed = _git(env.work, "rev-parse", "HEAD").stdout.strip()
    # dirty on top of the unpushed commit
    (env.work / "file.txt").write_text("v2-dirty\n", encoding="utf-8")

    working_set.snapshot_out(SID, env.work, "runnerA", False)

    fresh = _clone(env.url, env.tmp / "fresh")
    _git(fresh, "fetch", "origin", f"+{REF}:{REF}")
    parent = _git(fresh, "rev-parse", f"{REF}^").stdout.strip()
    assert parent == unpushed
    assert _git(fresh, "cat-file", "-e", unpushed, check=False).returncode == 0


def test_snapshot_local_origin_is_noop(env, ref_store):
    """Case 9a: a ``local:`` origin → silent no-op (nothing to sync through)."""
    (env.work / "README.md").write_text("dirty\n", encoding="utf-8")
    working_set.snapshot_out(SID, env.work, "runnerA", True)
    assert not _has_ref(env.origin)
    assert ref_store.calls == []


def test_snapshot_non_repo_is_noop(tmp_path, ref_store):
    """Case 9b: a cwd that is not a git repo → silent no-op."""
    d = tmp_path / "notrepo"
    d.mkdir()
    working_set.snapshot_out(SID, d, "runnerA", False)
    assert ref_store.calls == []


def test_snapshot_no_origin_is_noop(tmp_path, ref_store):
    """Case 9c: a dirty repo with no ``origin`` remote → silent no-op."""
    work = tmp_path / "w"
    _init_work(work)
    (work / "README.md").write_text("dirty\n", encoding="utf-8")
    working_set.snapshot_out(SID, work, "runnerA", False)
    assert ref_store.calls == []


# ---------------------------------------------------------------------------
# restore_in
# ---------------------------------------------------------------------------

def _snapshot_and_store(env, ref_store, content="hello world\n"):
    """Dirty README, snapshot it, and return the stored column JSON."""
    (env.work / "README.md").write_text(content, encoding="utf-8")
    working_set.snapshot_out(SID, env.work, "runnerA", False)
    assert ref_store.value not in (ref_store.UNSET, None)
    return ref_store.value


def test_restore_clean_head_equals_parent(env, ref_store):
    """Case 4: a clean second clone whose HEAD == the stash parent → the dirty
    change appears and the column is cleared."""
    stored = _snapshot_and_store(env, ref_store)
    clone2 = _clone(env.url, env.tmp / "clone2")  # HEAD == origin main == parent

    note = working_set.restore_in(SID, clone2, "runnerB", stored, False)

    assert note == "Applied in-progress changes from your previous device."
    assert (clone2 / "README.md").read_text(encoding="utf-8") == "hello world\n"
    assert ref_store.calls[-1] == (SID, None)
    assert ref_store.value is None


def test_restore_fast_forward_then_apply(env, ref_store):
    """Case 5: the second clone is behind (its HEAD is an ancestor of the stash
    parent) → fast-forward, then apply, succeeds."""
    clone2 = _clone(env.url, env.tmp / "clone2")  # HEAD == base
    # work advances one commit past base, then dirties a tracked file.
    (env.work / "README.md").write_text("v2\n", encoding="utf-8")
    _git(env.work, "add", "-A")
    _git(env.work, "commit", "-m", "C")
    c = _git(env.work, "rev-parse", "HEAD").stdout.strip()
    (env.work / "README.md").write_text("v2-dirty\n", encoding="utf-8")
    working_set.snapshot_out(SID, env.work, "runnerA", False)
    stored = ref_store.value

    note = working_set.restore_in(SID, clone2, "runnerB", stored, False)

    assert note == "Applied in-progress changes from your previous device."
    assert _git(clone2, "rev-parse", "HEAD").stdout.strip() == c
    assert (clone2 / "README.md").read_text(encoding="utf-8") == "v2-dirty\n"
    assert ref_store.value is None


def test_restore_dirty_checkout_skips(env, ref_store):
    """Case 6: a dirty second clone → skipped with a note, column intact, local
    work untouched."""
    stored = _snapshot_and_store(env, ref_store, content="snap\n")
    clone2 = _clone(env.url, env.tmp / "clone2")
    (clone2 / "README.md").write_text("local-work\n", encoding="utf-8")

    note = working_set.restore_in(SID, clone2, "runnerB", stored, False)

    assert note is not None and "local changes" in note
    assert not ref_store.wrote_none()
    assert (clone2 / "README.md").read_text(encoding="utf-8") == "local-work\n"


def test_restore_diverged_skips(env, ref_store):
    """Case 7a: diverged branches → skipped with a note, column intact."""
    clone2 = _clone(env.url, env.tmp / "clone2")
    # clone2 commits its own change → diverges from work's line.
    (clone2 / "README.md").write_text("D\n", encoding="utf-8")
    _git(clone2, "add", "-A")
    _git(clone2, "commit", "-m", "D")
    # work commits C (sibling of D under base) + a dirty change, then snapshots.
    (env.work / "README.md").write_text("C\n", encoding="utf-8")
    _git(env.work, "add", "-A")
    _git(env.work, "commit", "-m", "C")
    (env.work / "README.md").write_text("C-dirty\n", encoding="utf-8")
    working_set.snapshot_out(SID, env.work, "runnerA", False)
    stored = ref_store.value

    note = working_set.restore_in(SID, clone2, "runnerB", stored, False)

    assert note is not None and "diverged" in note
    assert not ref_store.wrote_none()


def test_restore_apply_failure_resets_and_keeps_ref(env, ref_store):
    """Case 7b: ``git stash apply`` exits non-zero → the (clean) tree is reset
    back and a manual-apply note is returned, column kept. Driven with real git
    by pointing the scratch ref at a NON-stash commit whose parent == HEAD, so
    restore reaches the apply and it genuinely fails (a true 3-way conflict is
    unreachable once the tree is fast-forwarded to the stash parent)."""
    # A normal commit C on top of base; push the scratch ref pointing at it.
    (env.work / "README.md").write_text("C\n", encoding="utf-8")
    _git(env.work, "add", "-A")
    _git(env.work, "commit", "-m", "C")
    c = _git(env.work, "rev-parse", "HEAD").stdout.strip()
    _git(env.work, "update-ref", REF, c)
    _git(env.work, "push", "--force", "origin", f"{REF}:{REF}")
    stored = json.dumps({"ref": REF, "sha": c, "runner_id": "runnerA"})

    clone2 = _clone(env.url, env.tmp / "clone2")  # HEAD == base == C^

    note = working_set.restore_in(SID, clone2, "runnerB", stored, False)

    assert note is not None and "apply" in note.lower()
    # Tree clean after the reset (was clean before, nothing lost).
    assert _porcelain(clone2).strip() == ""
    # Column kept — never cleared on failure.
    assert not ref_store.wrote_none()


def test_same_runner_guard_makes_no_git_calls(env, ref_store, monkeypatch):
    """Case 8: stored runner_id == the executing runner → None and ZERO git
    subprocess calls (short-circuits before any _run)."""
    calls = []
    real_run = working_set._run

    def spy(*a, **k):
        calls.append((a, k))
        return real_run(*a, **k)

    monkeypatch.setattr(working_set, "_run", spy)
    stored = json.dumps({"ref": REF, "sha": "deadbeef", "runner_id": "runnerA"})

    note = working_set.restore_in(SID, env.work, "runnerA", stored, False)

    assert note is None
    assert calls == []
    assert ref_store.calls == []


def test_restore_local_origin_is_noop(env, ref_store):
    """Case 9 (restore side): a ``local:`` origin → silent None, no column write."""
    stored = json.dumps({"ref": REF, "sha": "x", "runner_id": "runnerA"})
    note = working_set.restore_in(SID, env.work, "runnerB", stored, True)
    assert note is None
    assert ref_store.calls == []


# ---------------------------------------------------------------------------
# session_runtime hook invocation (case 10)
# ---------------------------------------------------------------------------

async def test_hooks_invoke_working_set_when_flag_on(monkeypatch):
    """F5: the runtime's snapshot and restore hooks call
    ``working_set.snapshot_out`` / ``restore_in`` with the expected
    (session_id, cwd, runner_id[, stored]) shapes. The restore hook feeds the
    FRESHLY fetched row's ref (F1: it always re-reads the row and never trusts
    a pre-claim value) — proven by patching ``database.get_session`` to return
    a ref distinct from any prior value and asserting the stub sees it. No real
    git or DB runs (both seams patched)."""
    from app.core import session_runtime
    from app.core import project_resolver
    from app.core.config import settings
    from app.db import database as database_mod
    import app.core.working_set as ws

    FRESH_STORED = json.dumps(
        {"ref": REF, "sha": "freshsha", "runner_id": "runnerA"}
    )
    fresh_row = {
        "session_id": "s",
        "id": "t",
        "working_set_ref": FRESH_STORED,
    }
    monkeypatch.setattr(database_mod, "get_session", lambda sid: fresh_row)
    monkeypatch.setattr(
        project_resolver, "resolve_existing_cwd", lambda row: "/some/cwd"
    )
    # is_local_origin → False (no local: origin), without touching the DB.
    monkeypatch.setattr(
        project_resolver, "resolve_project_v2", lambda row: None
    )

    snap_calls = []
    rest_calls = []

    def snap_stub(*a):
        snap_calls.append(a)

    def rest_stub(*a):
        rest_calls.append(a)
        return None  # None → no drained event recorded, no further seams needed

    monkeypatch.setattr(ws, "snapshot_out", snap_stub)
    monkeypatch.setattr(ws, "restore_in", rest_stub)

    rt = session_runtime.SessionRuntime({"session_id": "s", "id": "t"})
    await rt._snapshot_working_set("s")
    await rt._restore_working_set("s")

    runner = settings.effective_instance_id

    # snapshot_out(session_id, Path(cwd), runner_id, is_local)
    assert len(snap_calls) == 1
    s_sid, s_cwd, s_runner, s_local = snap_calls[0]
    assert s_sid == "s"
    assert s_cwd == Path("/some/cwd")
    assert s_runner == runner
    assert s_local is False

    # restore_in(session_id, Path(cwd), runner_id, stored, is_local)
    assert len(rest_calls) == 1
    r_sid, r_cwd, r_runner, r_stored, r_local = rest_calls[0]
    assert r_sid == "s"
    assert r_cwd == Path("/some/cwd")
    assert r_runner == runner
    assert r_local is False
    # F1: the ref handed to restore_in is the FRESHLY fetched row's value.
    assert r_stored == FRESH_STORED


# ---------------------------------------------------------------------------
# F3 "ahead" note arm (real git) + F4 id validation
# ---------------------------------------------------------------------------

def test_restore_ahead_of_base_skips_with_distinct_note(env, ref_store):
    """F3: HEAD is not an ancestor of the stash parent but the stash parent IS an
    ancestor of HEAD → this checkout is AHEAD of the snapshot base. Distinct
    'ahead' wording, skipped, column kept, local work untouched."""
    stored = _snapshot_and_store(env, ref_store, content="snap\n")  # parent == base
    clone2 = _clone(env.url, env.tmp / "clone2")  # HEAD == base
    # clone2 advances one commit past base → base is now an ancestor of HEAD.
    (clone2 / "AHEAD.txt").write_text("x\n", encoding="utf-8")
    _git(clone2, "add", "-A")
    _git(clone2, "commit", "-m", "ahead")
    ahead_head = _git(clone2, "rev-parse", "HEAD").stdout.strip()

    note = working_set.restore_in(SID, clone2, "runnerB", stored, False)

    assert note is not None and "ahead" in note
    assert "diverged" not in note
    # Skipped: HEAD unchanged, column kept.
    assert _git(clone2, "rev-parse", "HEAD").stdout.strip() == ahead_head
    assert not ref_store.wrote_none()


def test_ref_for_rejects_unsafe_id():
    """F4: _ref_for raises ValueError on ids that could escape the ref namespace,
    and accepts normal ids."""
    with pytest.raises(ValueError):
        working_set._ref_for("../evil")
    with pytest.raises(ValueError):
        working_set._ref_for("")
    with pytest.raises(ValueError):
        working_set._ref_for("a/b")
    # normal server-generated ids pass through unchanged.
    assert working_set._ref_for("abc-123_.X") == (
        "refs/aishuffle/sessions/abc-123_.X"
    )


def test_snapshot_out_malicious_id_logs_and_noops(env, ref_store, caplog):
    """F4: a malicious session id degrades to a logged no-op — snapshot_out never
    raises and never writes the column."""
    import logging

    (env.work / "README.md").write_text("dirty\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.core.working_set"):
        # must not raise despite the bad id
        working_set.snapshot_out("../evil", env.work, "runnerA", False)

    assert ref_store.calls == []
    assert any(
        "snapshot_out failed" in r.getMessage() for r in caplog.records
    )
