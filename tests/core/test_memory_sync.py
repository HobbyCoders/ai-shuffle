"""Unit tests for cross-install agent-memory sync (``app.core.memory_sync``).

The DB layer (``app.db.agent_memory``) is replaced with an in-memory fake; the
local memory directory is a real temp dir so the on-disk read/write/delete path
is exercised end to end. File mtimes are pinned with ``os.utime`` so the
last-write-wins decisions are deterministic regardless of FS timestamp
granularity.

Coverage:
    reconcile  — pull new file, apply remote tombstone, keep newer-local
                 (revive), db-newer-content wins, local-newer-content kept,
                 path-escape guard
    push       — upload new file, tombstone local deletion (gated),
                 empty-dir guard blocks tombstoning, LWW skips newer remote,
                 revive a tombstone
    entry pts  — native-only gate (a delegated leg is a no-op); flag disable
                 no-ops; key resolves via link_group_id
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import memory_sync


KEY = "git:deadbeef"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# =============================================================================
# Fakes / fixtures
# =============================================================================

class FakeMemDb:
    """In-memory stand-in for app.db.agent_memory."""

    def __init__(self):
        self.rows = {}  # (memory_key, relative_path) -> row dict
        self.clock = T0  # timestamp stamped on writes that don't pass updated_at

    def get_memory_files(self, memory_key):
        return [dict(r) for k, r in self.rows.items() if k[0] == memory_key]

    def get_memory_file(self, memory_key, relative_path):
        row = self.rows.get((memory_key, relative_path))
        return dict(row) if row else None

    def upsert_memory_file(self, memory_key, relative_path, content,
                           content_hash, updated_by=None, updated_at=None):
        self.rows[(memory_key, relative_path)] = {
            "memory_key": memory_key,
            "relative_path": relative_path,
            "content": content,
            "content_hash": content_hash,
            "deleted": False,
            "updated_at": updated_at or self.clock,
            "updated_by": updated_by,
        }

    def tombstone_memory_file(self, memory_key, relative_path,
                              updated_by=None, updated_at=None):
        self.rows[(memory_key, relative_path)] = {
            "memory_key": memory_key,
            "relative_path": relative_path,
            "content": None,
            "content_hash": None,
            "deleted": True,
            "updated_at": updated_at or self.clock,
            "updated_by": updated_by,
        }

    # convenience for arranging DB state in tests
    def seed_live(self, rel, content, updated_at):
        self.upsert_memory_file(
            KEY, rel, content, memory_sync._sha256(content), "remote", updated_at
        )

    def seed_tombstone(self, rel, updated_at):
        self.tombstone_memory_file(KEY, rel, "remote", updated_at)


@pytest.fixture
def memdb():
    fake = FakeMemDb()
    with patch.object(memory_sync, "memdb", fake):
        yield fake


@pytest.fixture
def memdir(tmp_path):
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def reset_reconciled():
    memory_sync.reset_state()
    yield
    memory_sync.reset_state()


# =============================================================================
# Helpers
# =============================================================================

def write_file(memory_dir: Path, rel: str, content: str, mtime: datetime) -> Path:
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    ts = mtime.timestamp()
    os.utime(p, (ts, ts))
    return p


def read_file(memory_dir: Path, rel: str) -> str:
    return (memory_dir / rel).read_text(encoding="utf-8")


# =============================================================================
# reconcile  (DB -> local)
# =============================================================================

def test_reconcile_pulls_new_db_file_into_empty_dir(memdb, memdir):
    memdb.seed_live("MEMORY.md", "shared index", T0)

    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert read_file(memdir, "MEMORY.md") == "shared index"
    # The gate is keyed per (memory_key, local mirror dir) — see ruling MEM2.
    assert (KEY, str(memdir)) in memory_sync._reconciled_keys


def test_reconcile_applies_remote_tombstone(memdb, memdir):
    write_file(memdir, "fact.md", "old local", T0)
    memdb.seed_tombstone("fact.md", T0 + timedelta(hours=1))  # remote delete is newer

    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert not (memdir / "fact.md").exists()


def test_reconcile_keeps_local_when_newer_than_tombstone(memdb, memdir):
    # Local was re-created AFTER the remote deletion -> revive, keep it.
    write_file(memdir, "fact.md", "revived locally", T0 + timedelta(hours=2))
    memdb.seed_tombstone("fact.md", T0 + timedelta(hours=1))

    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert read_file(memdir, "fact.md") == "revived locally"


def test_reconcile_db_newer_content_overwrites_local(memdb, memdir):
    write_file(memdir, "fact.md", "stale local", T0)
    memdb.seed_live("fact.md", "fresh remote", T0 + timedelta(hours=1))

    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert read_file(memdir, "fact.md") == "fresh remote"


def test_reconcile_local_newer_content_is_kept(memdb, memdir):
    write_file(memdir, "fact.md", "fresh local", T0 + timedelta(hours=1))
    memdb.seed_live("fact.md", "stale remote", T0)

    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert read_file(memdir, "fact.md") == "fresh local"


def test_reconcile_refuses_path_escape(memdb, memdir, tmp_path):
    outside = tmp_path / "evil.md"
    memdb.seed_live("../evil.md", "pwned", T0)

    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert not outside.exists()


def test_scan_skips_symlink_to_outside_secret(memdb, memdir, tmp_path):
    # A planted .md symlink pointing at a secret outside the memory tree must
    # NOT be read and uploaded to the shared DB (cross-install exfiltration).
    secret = tmp_path / "secret"
    secret.write_text("BEGIN RSA PRIVATE KEY", encoding="utf-8")
    link = memdir / "exfil.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=False)

    assert memdb.get_memory_file(KEY, "exfil.md") is None
    assert memdb.get_memory_files(KEY) == []


# =============================================================================
# push  (local -> DB)
# =============================================================================

def test_push_uploads_new_local_file(memdb, memdir):
    write_file(memdir, "fact.md", "local only", T0)

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=False)

    row = memdb.get_memory_file(KEY, "fact.md")
    assert row["content"] == "local only"
    assert row["content_hash"] == memory_sync._sha256("local only")
    assert row["deleted"] is False
    assert row["updated_by"] == "inst-a"


def test_push_tombstones_genuine_local_deletion(memdb, memdir):
    # A file we HELD (reconciled down into the baseline) and then deleted on disk
    # is a real delete -> tombstone it so peers drop their copies too.
    memdb.seed_live("gone.md", "was here", T0)
    memory_sync.reconcile(KEY, memdir, "inst-a")  # pulls gone.md down -> baseline
    os.remove(memdir / "gone.md")                 # user/agent deletes it locally

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)

    assert memdb.get_memory_file(KEY, "gone.md")["deleted"] is True


def test_push_does_not_tombstone_unheld_db_file(memdb, memdir):
    # Regression for the cross-install mirror-delete data loss: a live DB row this
    # install never materialized (a peer authored it, or it only lives in a peer's
    # memory dir) must NOT be tombstoned by our push even with deletes allowed.
    memory_sync.reconcile(KEY, memdir, "inst-a")     # baseline is empty
    memdb.seed_live("peer.md", "peer authored", T0)  # appears AFTER our reconcile

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)

    row = memdb.get_memory_file(KEY, "peer.md")
    assert row["deleted"] is False
    assert row["content"] == "peer authored"


def test_push_does_not_tombstone_peer_file_added_during_our_turn(memdb, memdir):
    # Two installs share a memory_key but keep separate memory dirs. We reconcile,
    # author our own file, and a peer adds theirs to the DB mid-turn. Our push must
    # upload ours and leave the peer's file untouched (was the file-loss bug).
    write_file(memdir, "ours.md", "our fact", T0)
    memory_sync.reconcile(KEY, memdir, "inst-a")     # baseline = {ours.md}
    memdb.seed_live("theirs.md", "peer fact", T0 + timedelta(minutes=5))

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)

    assert memdb.get_memory_file(KEY, "ours.md")["content"] == "our fact"
    assert memdb.get_memory_file(KEY, "theirs.md")["deleted"] is False


def test_reconcile_materializes_peer_file_after_it_survives_push(memdb, memdir):
    # End-to-end index/file consistency: the peer's live row survives our push
    # (above), so a subsequent reconcile pulls the topic file down onto disk —
    # no dangling MEMORY.md links.
    memory_sync.reconcile(KEY, memdir, "inst-a")
    memdb.seed_live("MEMORY.md", "- see peer topic", T0)
    memdb.seed_live("peer-topic.md", "the topic body", T0)

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)
    memory_sync.reconcile(KEY, memdir, "inst-a")

    assert read_file(memdir, "peer-topic.md") == "the topic body"
    assert memdb.get_memory_file(KEY, "peer-topic.md")["deleted"] is False


def test_push_empty_dir_guard_does_not_tombstone(memdb, memdir):
    memdb.seed_live("keep.md", "precious", T0)

    # allow_deletes=False mirrors "reconcile never ran" -> no destructive wipe.
    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=False)

    assert memdb.get_memory_file(KEY, "keep.md")["deleted"] is False


def test_push_skips_overwriting_newer_remote(memdb, memdir):
    write_file(memdir, "fact.md", "local edit", T0)  # older
    memdb.seed_live("fact.md", "remote edit", T0 + timedelta(hours=1))  # newer

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)

    assert memdb.get_memory_file(KEY, "fact.md")["content"] == "remote edit"


def test_push_revives_tombstone_when_local_present(memdb, memdir):
    memdb.seed_tombstone("fact.md", T0)
    write_file(memdir, "fact.md", "back again", T0 + timedelta(hours=1))

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)

    row = memdb.get_memory_file(KEY, "fact.md")
    assert row["deleted"] is False
    assert row["content"] == "back again"


def test_push_unchanged_file_is_not_rewritten(memdb, memdir):
    content = "identical"
    write_file(memdir, "fact.md", content, T0 + timedelta(hours=1))
    memdb.seed_live("fact.md", content, T0)  # same hash already in DB

    memory_sync.push(KEY, memdir, "inst-a", allow_deletes=True)

    # untouched: updated_by stays the seeded "remote", not "inst-a"
    assert memdb.get_memory_file(KEY, "fact.md")["updated_by"] == "remote"


# =============================================================================
# Public entry points
# =============================================================================

def test_entrypoints_noop_when_disabled(memdb, memdir):
    write_file(memdir, "fact.md", "local", T0)
    with patch.object(memory_sync.settings, "agent_memory_sync", False):
        memory_sync.push_memory_sync("p1", "/work/x", "native")
    assert memdb.get_memory_files(KEY) == []


def test_entrypoint_keys_by_link_group_id(memdb, memdir):
    write_file(memdir, "fact.md", "local", T0)
    project = {"id": "p1", "link_group_id": KEY}
    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.database, "get_project", return_value=project), \
         patch.object(memory_sync, "memory_dir_for_working_dir", return_value=memdir):
        memory_sync.reconcile_memory_sync("p1", "/work/x", "native")
        memory_sync.push_memory_sync("p1", "/work/x", "native")

    # Pushed under the link_group_id key, not the project id.
    assert memdb.get_memory_file(KEY, "fact.md")["content"] == "local"
    assert memdb.get_memory_files("p1") == []


def test_entrypoint_falls_back_to_project_id_when_unlinked(memdb, memdir):
    write_file(memdir, "fact.md", "local", T0)
    project = {"id": "p1", "link_group_id": None}
    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.database, "get_project", return_value=project), \
         patch.object(memory_sync, "memory_dir_for_working_dir", return_value=memdir):
        memory_sync.push_memory_sync("p1", "/work/x", "native")

    assert memdb.get_memory_file("p1", "fact.md")["content"] == "local"


# =============================================================================
# Worktree cwd anchoring (regression: sync silently no-op'd for worktrees)
# =============================================================================

def test_repo_root_strips_worktree_suffix():
    # A worktree cwd folds back to its main checkout before slugging.
    assert (
        memory_sync._repo_root_for_working_dir(
            "/workspace/ai-shuffle/.claude/worktrees/fix-thing-bb7d"
        )
        == "/workspace/ai-shuffle"
    )
    # Deeper nesting under the worktree still reduces to the repo root.
    assert (
        memory_sync._repo_root_for_working_dir(
            "/repo/.claude/worktrees/name/sub/dir"
        )
        == "/repo"
    )
    # A plain (non-worktree) cwd is left untouched.
    assert memory_sync._repo_root_for_working_dir("/workspace/proj") == "/workspace/proj"


def test_memory_dir_for_worktree_cwd_matches_main_checkout(tmp_path):
    # The resolved memory dir for a worktree cwd is the MAIN checkout's slug dir,
    # not a per-worktree slug the CLI never writes into.
    projects = tmp_path / "projects"
    with patch.object(memory_sync.settings, "claude_projects_dir", projects):
        main = memory_sync.memory_dir_for_working_dir("/workspace/ai-shuffle")
        worktree = memory_sync.memory_dir_for_working_dir(
            "/workspace/ai-shuffle/.claude/worktrees/fix-thing-bb7d"
        )
    assert worktree == main


def test_worktree_session_syncs_to_main_project_memory_dir(memdb, tmp_path):
    # End to end: a worktree session's cwd is <repo>/.fluid/worktrees/<name>,
    # but the memory dir is anchored on the MAIN checkout's slug. push must
    # operate on that main dir — proof is that the file left there gets
    # uploaded, rather than a scan of an empty/absent worktree slug dir no-op'ing.
    main_repo = "/workspace/ai-shuffle"
    worktree_cwd = "/workspace/ai-shuffle/.claude/worktrees/fix-thing-bb7d"
    projects = tmp_path / "projects"
    main_slug = memory_sync.get_project_dir_name(main_repo)
    main_mem = projects / main_slug / "memory"
    main_mem.mkdir(parents=True)
    write_file(main_mem, "MEMORY.md", "worktree turn wrote this", T0)

    project = {"id": "p1", "link_group_id": KEY, "path": main_repo}
    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(projects)), \
         patch.object(memory_sync.database, "get_project", return_value=project):
        memory_sync.push_memory_sync("p1", worktree_cwd, "native")

    assert memdb.get_memory_file(KEY, "MEMORY.md")["content"] == "worktree turn wrote this"


def test_target_anchors_on_project_path_over_session_cwd(memdb, tmp_path):
    # Even when the raw cwd is a worktree, _resolve_target slugs the project's
    # root path (main checkout), so reconcile pulls DB files into the main dir.
    main_repo = "/workspace/ai-shuffle"
    worktree_cwd = "/workspace/ai-shuffle/.claude/worktrees/fix-thing-bb7d"
    projects = tmp_path / "projects"
    main_mem = projects / memory_sync.get_project_dir_name(main_repo) / "memory"

    memdb.seed_live("MEMORY.md", "shared index", T0)
    project = {"id": "p1", "link_group_id": KEY, "path": main_repo}
    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(projects)), \
         patch.object(memory_sync.database, "get_project", return_value=project):
        memory_sync.reconcile_memory_sync("p1", worktree_cwd, "native")

    assert (main_mem / "MEMORY.md").read_text(encoding="utf-8") == "shared index"


def test_push_entrypoint_delete_gate_requires_prior_reconcile(memdb, memdir):
    # DB has a file; local dir is empty. Without a prior reconcile in this
    # process the gate must block tombstoning even though the dir exists.
    memdb.seed_live("keep.md", "precious", T0)
    project = {"id": "p1", "link_group_id": KEY}
    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.database, "get_project", return_value=project), \
         patch.object(memory_sync, "memory_dir_for_working_dir", return_value=memdir):
        memory_sync.push_memory_sync("p1", "/work/x", "native")  # no reconcile first
        assert memdb.get_memory_file(KEY, "keep.md")["deleted"] is False

        # After a reconcile establishes the dir as authoritative, deletes flow.
        memory_sync.reconcile_memory_sync("p1", "/work/x", "native")
        os.remove(memdir / "keep.md")  # reconcile pulled it down; user removes it
        memory_sync.push_memory_sync("p1", "/work/x", "native")
        assert memdb.get_memory_file(KEY, "keep.md")["deleted"] is True


# =============================================================================
# Leg-aware memory dirs (rulings MEM1/MEM2)
# =============================================================================

def test_native_leg_resolves_under_the_fluid_projects_root(tmp_path):
    # Same slug, different root: the native leg's mirror lives in
    # <fluid_projects_dir>/<slug>/memory, never in ~/.claude/projects.
    claude_root = tmp_path / "claude-projects"
    fluid_root = tmp_path / "fluid-projects"
    slug = memory_sync.get_project_dir_name("/workspace/ai-shuffle")

    with patch.object(memory_sync.settings, "claude_projects_dir", claude_root), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(fluid_root)):
        native = memory_sync.memory_dir_for_working_dir(
            "/workspace/ai-shuffle", "native"
        )
        delegated = memory_sync.memory_dir_for_working_dir(
            "/workspace/ai-shuffle", "delegated"
        )
        default = memory_sync.memory_dir_for_working_dir("/workspace/ai-shuffle")

    assert native == fluid_root / slug / "memory"
    assert delegated == claude_root / slug / "memory"
    # An untouched caller keeps today's behaviour.
    assert default == delegated


def test_worktree_fold_applies_on_both_legs(tmp_path):
    claude_root = tmp_path / "claude-projects"
    fluid_root = tmp_path / "fluid-projects"
    worktree = "/workspace/ai-shuffle/.claude/worktrees/fix-thing-bb7d"

    with patch.object(memory_sync.settings, "claude_projects_dir", claude_root), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(fluid_root)):
        for leg in ("delegated", "native"):
            assert memory_sync.memory_dir_for_working_dir(worktree, leg) == \
                memory_sync.memory_dir_for_working_dir("/workspace/ai-shuffle", leg)


def test_resolve_target_shares_the_key_across_legs(tmp_path):
    # One memory_key, two dirs. Only the native dir is ever synced; the claude
    # dir is resolvable purely so the explicit import can find it.
    project = {"id": "p1", "link_group_id": KEY, "path": "/workspace/ai-shuffle"}
    with patch.object(memory_sync.settings, "claude_projects_dir", tmp_path / "c"), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(tmp_path / "f")), \
         patch.object(memory_sync.database, "get_project", return_value=project):
        d_key, d_dir = memory_sync._resolve_target("p1", "/work/x", "delegated")
        n_key, n_dir = memory_sync._resolve_target("p1", "/work/x", "native")

    assert d_key == n_key == KEY
    assert d_dir != n_dir


def test_native_reconcile_materializes_existing_shared_memory(memdb, tmp_path):
    # Rows already in the shared DB (put there by a peer install's native
    # session) come down into THIS install's fluid dir on reconcile. Nothing is
    # copied dir-to-dir; migrating claude-side memory is the explicit import.
    memdb.seed_live("MEMORY.md", "shared index", T0)
    fluid_root = tmp_path / "fluid-projects"
    project = {"id": "p1", "link_group_id": KEY, "path": "/workspace/ai-shuffle"}

    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(fluid_root)), \
         patch.object(memory_sync.database, "get_project", return_value=project):
        memory_sync.reconcile_memory_sync("p1", "/work/x", "native")

    slug = memory_sync.get_project_dir_name("/workspace/ai-shuffle")
    assert (fluid_root / slug / "memory" / "MEMORY.md").read_text(
        encoding="utf-8"
    ) == "shared index"


def test_delegated_leg_sync_is_a_noop(memdb, tmp_path):
    # The claude CLI's memory dir is its own business: neither entry point
    # touches the DB or the filesystem for a delegated leg.
    claude_root = tmp_path / "claude-projects"
    fluid_root = tmp_path / "fluid-projects"
    slug = memory_sync.get_project_dir_name("/workspace/ai-shuffle")
    claude_mem = claude_root / slug / "memory"
    claude_mem.mkdir(parents=True)
    write_file(claude_mem, "cli-fact.md", "the CLI wrote this", T0)
    project = {"id": "p1", "link_group_id": KEY, "path": "/workspace/ai-shuffle"}

    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.settings, "claude_projects_dir", claude_root), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(fluid_root)), \
         patch.object(memory_sync.database, "get_project", return_value=project):
        memory_sync.reconcile_memory_sync("p1", "/work/x", "delegated")
        memory_sync.push_memory_sync("p1", "/work/x", "delegated")
        # The default leg is "delegated" too — an undecidable session must be
        # just as inert as an explicitly delegated one.
        memory_sync.reconcile_memory_sync("p1", "/work/x")
        memory_sync.push_memory_sync("p1", "/work/x")

    # Nothing uploaded, no reconcile bookkeeping, and no dir conjured anywhere.
    assert memdb.get_memory_files(KEY) == []
    assert memory_sync._reconciled_keys == set()
    assert not fluid_root.exists()
    # The CLI's own file is left exactly as it was.
    assert read_file(claude_mem, "cli-fact.md") == "the CLI wrote this"


def test_delete_authorization_does_not_leak_across_native_dirs(memdb, tmp_path):
    # Two checkouts share a memory_key (git link group) but keep separate
    # native mirrors. Reconciling one must not authorize deletes against the
    # other: an empty (or merely stale) sibling dir would otherwise tombstone
    # live files. The gate is keyed per (memory_key, dir).
    memdb.seed_live("keep.md", "precious", T0)
    fluid_root = tmp_path / "fluid-projects"
    here = {"id": "p1", "link_group_id": KEY, "path": "/workspace/ai-shuffle"}
    peer = {"id": "p2", "link_group_id": KEY, "path": "/workspace/ai-shuffle-clone"}
    projects = {"p1": here, "p2": peer}
    here_mem = fluid_root / memory_sync.get_project_dir_name(here["path"]) / "memory"
    peer_mem = fluid_root / memory_sync.get_project_dir_name(peer["path"]) / "memory"
    peer_mem.mkdir(parents=True)  # exists, but empty

    with patch.object(memory_sync.settings, "agent_memory_sync", True), \
         patch.object(memory_sync.settings, "fluid_projects_dir", str(fluid_root)), \
         patch.object(memory_sync.database, "get_project",
                      side_effect=lambda pid: projects[pid]):
        memory_sync.reconcile_memory_sync("p1", "/work/x", "native")
        memory_sync.push_memory_sync("p2", "/work/y", "native")
        assert memdb.get_memory_file(KEY, "keep.md")["deleted"] is False

        # Our own dir's reconcile DID authorize deletes for that mirror, so
        # removing the file there still propagates.
        os.remove(here_mem / "keep.md")
        memory_sync.push_memory_sync("p1", "/work/x", "native")
        assert memdb.get_memory_file(KEY, "keep.md")["deleted"] is True


# =============================================================================
# Leg decision (memory_leg_for_options)
# =============================================================================

class _Options:
    """Stand-in for a built ClaudeAgentOptions (only the read attrs matter)."""

    def __init__(self, **attrs):
        self.model = attrs.pop("model", "sonnet")
        for name, value in attrs.items():
            setattr(self, name, value)


def test_memory_leg_for_delegated_claude_options():
    assert memory_sync.memory_leg_for_options(_Options(model="sonnet")) == "delegated"


def test_memory_leg_for_native_openai_shaped_options():
    options = _Options(
        model="gpt-4o",
        aih_provider_id="prov-1",
        aih_provider_type="openai",
        aih_provider_auth_mode="api_key",
    )
    assert memory_sync.memory_leg_for_options(options) == "native"


def test_memory_leg_degrades_to_delegated_when_undecidable():
    assert memory_sync.memory_leg_for_options(None) == "delegated"
    with patch.object(
        memory_sync.logger, "debug"
    ):  # the decision table raising must not surface
        assert memory_sync.memory_leg_for_options(object()) in (
            "delegated", "native"
        )
