"""
U1 regression: a parked session's /live delta poll must surface activeTasks,
never a bare 304.

The /live endpoint is the sole authority the session poller uses to decide
whether to keep a parked session alive. When a delta poll (since_update_id > 0)
finds no MESSAGE updates but the session still has active background work (a
sub-agent / SDK-backgrounded shell) — or an unpark follow-up mid-handoff — the
endpoint MUST return a lightweight 200 body carrying activeTasks / unpark_pending
instead of a 304 that would hide the state and let the poller stop, stranding the
parked UI until a full reload.

This guard runs on the OWNING install. The cluster /live endpoint delegates to
this same function (app.api.cluster.cluster_live -> get_live_session), and
cloud_relay.relay_live forwards the owner's 200 body verbatim — so locking the
owner-side behavior here also pins cloud-session revival across the relay (U1).
"""

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.responses import Response

from app.api.sessions import get_live_session
from app.core.task_registry import task_registry
from app.core.live_session_store import live_session_store


def _owner_path_patches(session):
    """Force the LOCAL owner path: not a remote/relayed session, no worktree DB."""
    return [
        patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)),
        patch("app.api.sessions.enrich_session_worktree_meta", new=MagicMock(return_value={})),
        patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=False)),
    ]


async def test_parked_session_delta_poll_returns_active_tasks_not_304():
    """Active background work + no message delta -> 200 body with activeTasks."""
    session_id = "u1-parked-sess"
    session = {"id": session_id, "title": "Parked"}
    # A background sub-agent is in flight; the parent turn ended (not streaming).
    task_registry.start_task(session_id, "task-1", "agent", "bg work")

    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_since", return_value=None):
            result = await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=7, token="tok",
            )
    finally:
        for p in patches:
            p.stop()
        task_registry.clear_session(session_id)

    # NOT a 304: a dict body carrying the live activeTasks so the poller keeps
    # the parked session alive.
    assert not isinstance(result, Response)
    assert isinstance(result, dict)
    assert result["activeTasks"], "parked session must surface its active task"
    assert result["activeTasks"][0]["id"] == "task-1"
    assert result["is_streaming"] is False


async def test_unpark_pending_delta_poll_returns_body_not_304():
    """Unpark follow-up mid-handoff (no task yet) still returns a body, not 304."""
    session_id = "u1-unpark-sess"
    session = {"id": session_id, "title": "Unparking"}
    task_registry.mark_unpark_pending(session_id)

    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_since", return_value=None):
            result = await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=3, token="tok",
            )
    finally:
        for p in patches:
            p.stop()
        task_registry.clear_session(session_id)

    assert not isinstance(result, Response)
    assert isinstance(result, dict)
    assert result["unpark_pending"] is True


async def test_idle_session_delta_poll_returns_304():
    """Negative control: no work + no message delta -> genuine 304."""
    session_id = "u1-idle-sess"
    session = {"id": session_id, "title": "Idle"}
    task_registry.clear_session(session_id)  # ensure no stray tasks

    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_since", return_value=None):
            result = await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=7, token="tok",
            )
    finally:
        for p in patches:
            p.stop()

    assert isinstance(result, Response)
    assert result.status_code == 304


# =============================================================================
# Cloud relay passthrough (U1's original framing): a parked CLOUD session must
# revive on poll. The owner runs the same guard above; relay_live forwards its
# 200 body verbatim, so activeTasks reaches the viewing install untouched.
# =============================================================================

async def test_cloud_relay_passes_through_owner_active_tasks():
    """Relayed session: the owner's 200 body (with activeTasks) flows through."""
    session_id = "u1-cloud-sess"
    session = {"id": session_id, "title": "Cloud", "cloud_branch": "feature/x"}
    owner_body = {
        "session_id": session_id,
        "is_streaming": False,
        "last_update_id": 12,
        "messages": [],
        "activeTasks": [{"id": "remote-task", "status": "running", "kind": "agent"}],
        "unpark_pending": False,
        "session_meta": {"title": "Cloud"},
    }
    with patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)), \
         patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=True)), \
         patch("app.core.cloud_relay.relay_live", new=AsyncMock(return_value=(200, owner_body))):
        result = await get_live_session(
            request=MagicMock(), session_id=session_id,
            since_update_id=5, token="tok",
        )

    assert not isinstance(result, Response)
    assert isinstance(result, dict)
    assert result["activeTasks"][0]["id"] == "remote-task"
    # Relay block stamps cloud meta onto the owner's body.
    assert result["session_meta"]["execution_mode"] == "cloud"
    assert result["session_meta"]["device_online"] is True


async def test_cloud_relay_304_when_owner_idle():
    """Relayed session: owner 304 (no work) passes straight through as 304."""
    session_id = "u1-cloud-idle"
    session = {"id": session_id, "title": "Cloud"}
    with patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)), \
         patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=True)), \
         patch("app.core.cloud_relay.relay_live", new=AsyncMock(return_value=(304, None))):
        result = await get_live_session(
            request=MagicMock(), session_id=session_id,
            since_update_id=5, token="tok",
        )

    assert isinstance(result, Response)
    assert result.status_code == 304


# =============================================================================
# Relay FAILURE degradation (B2): when a remote session's owner is registry-
# online but the /live relay itself fails, get_live_session must degrade to
# read-only shared-DB history AND stamp session_meta.relay_error with a short,
# secret-free reason so the UI can explain WHY control is unavailable — instead
# of the ambiguous plain "device offline" (or, worse, a hard load error).
#
# These drive the delta-poll branch (since_update_id > 0 + a live-store snapshot)
# so the degraded response returns with session_meta attached without needing the
# full DB/JSONL message-loader path mocked.
# =============================================================================

async def _relay_failure_result(relay_exc):
    """Run get_live_session for a remote session whose relay_live raises, routed
    through the delta-poll live-store branch that attaches session_meta."""
    session_id = "b2-remote-unreachable"
    session = {"id": session_id, "title": "Remote"}
    snapshot = {
        "session_id": session_id,
        "is_streaming": False,
        "last_update_id": 7,
        "messages": [],
    }
    with patch("app.api.sessions.require_session", new=AsyncMock(return_value=session)), \
         patch("app.api.sessions.enrich_session_worktree_meta", new=MagicMock(return_value={})), \
         patch("app.core.cloud_relay.is_remote_session", new=MagicMock(return_value=True)), \
         patch("app.core.cloud_relay.relay_live", new=AsyncMock(side_effect=relay_exc)), \
         patch.object(task_registry, "get_visible", return_value=[]), \
         patch.object(task_registry, "is_unpark_pending", return_value=False), \
         patch.object(live_session_store, "has_session", return_value=True), \
         patch.object(live_session_store, "is_streaming", return_value=False), \
         patch.object(live_session_store, "get_session_since", return_value=dict(snapshot)):
        return await get_live_session(
            request=MagicMock(), session_id=session_id,
            since_update_id=5, token="tok",
        )


async def test_relay_offline_stamps_relay_error_and_degrades():
    """InstallOffline (owner offline/view-only) → view-only meta + relay_error."""
    from app.core import cloud_relay

    result = await _relay_failure_result(cloud_relay.InstallOffline("Device 'Laptop' is offline"))

    assert isinstance(result, dict)
    meta = result["session_meta"]
    assert meta["execution_mode"] == "cloud"
    assert meta["device_online"] is False
    assert meta["controllable"] is False
    # The InstallOffline message is already user-safe (device name only).
    assert meta["relay_error"] == "Device 'Laptop' is offline"


async def test_relay_peer_auth_error_stamps_categorized_reason():
    """A peer 403 (cluster-secret mismatch) → 'authentication rejected' reason,
    never the raw peer body / URL."""
    from app.core import cloud_relay

    exc = cloud_relay.CloudRelayError(
        "Peer install cluster live failed (403): secret-xyz",
        status_code=403,
        detail="secret-xyz",
    )
    result = await _relay_failure_result(exc)

    assert isinstance(result, dict)
    meta = result["session_meta"]
    assert meta["controllable"] is False
    assert meta["relay_error"] == "authentication rejected by device"
    # No secrets/detail leaked into the surfaced reason.
    assert "secret-xyz" not in meta["relay_error"]


async def test_relay_peer_5xx_error_stamps_status_reason():
    """A peer 502 → a categorized 'device returned an error (502)' reason."""
    from app.core import cloud_relay

    exc = cloud_relay.CloudRelayError("boom", status_code=502, detail="bad gateway")
    result = await _relay_failure_result(exc)

    meta = result["session_meta"]
    assert meta["relay_error"] == "device returned an error (502)"


async def test_relay_transport_error_stamps_unreachable_reason():
    """A CloudRelayError with no status_code (non-JSON body / timeout wrapper) →
    'device is unreachable'."""
    from app.core import cloud_relay

    exc = cloud_relay.CloudRelayError("Peer install cluster live returned non-JSON body")
    result = await _relay_failure_result(exc)

    meta = result["session_meta"]
    assert meta["relay_error"] == "device is unreachable"


# =============================================================================
# Parked fresh load (switch-away / switch-back): frozen sub-agent cards.
#
# A parked session (turn ended, background sub-agents still running) advances
# sub-agent progress by mutating agentChildren IN PLACE on an existing live
# message — the message COUNT never changes. When the user switches back, the
# poller re-attaches with since_update_id=0 (fresh load). The completeness heal
# then compared lengths: live buffer == store transcript → the STORE won the
# tie, serving frozen agentChildren AND last_update_id=0 — pinning every
# subsequent park poll to another since=0 fresh load of the same stale store
# transcript. The agent cards froze (e.g. stuck at "5/5 tools") while the
# sub-agents kept working. On a count tie WITH running tasks, the live buffer
# must win, carrying its real last_update_id to put the client back on deltas.
# =============================================================================

def _snapshot_and_store(session_id):
    """A live buffer and a store transcript with EQUAL message counts, where the
    buffer's subagent message has advanced agentChildren (in-place progress)."""
    live_snapshot = {
        "session_id": session_id,
        "is_streaming": False,
        "last_update_id": 42,
        "initial_message_count": 0,
        "messages": [
            {"id": "m1", "role": "user", "type": "text", "content": "go",
             "streaming": False},
            {"id": "m2", "role": "assistant", "type": "subagent", "content": "",
             "streaming": False,
             "agentChildren": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]},
        ],
    }
    store_messages = [
        {"id": "m1", "role": "user", "type": "text", "content": "go",
         "streaming": False},
        {"id": "m2", "role": "assistant", "type": "subagent", "content": "",
         "streaming": False, "agentChildren": [{"id": "c1"}]},
    ]
    return live_snapshot, store_messages


async def _fresh_load_with_tie(session_id, session):
    live_snapshot, store_messages = _snapshot_and_store(session_id)
    patches = _owner_path_patches(session)
    for p in patches:
        p.start()
    try:
        with patch("app.core.config.settings.session_store_mode", "mirror"), \
             patch("app.core.db_message_loader.load_messages_from_db",
                   return_value=(store_messages, True)), \
             patch.object(live_session_store, "has_session", return_value=True), \
             patch.object(live_session_store, "is_streaming", return_value=False), \
             patch.object(live_session_store, "get_session_dict",
                          return_value=live_snapshot):
            return await get_live_session(
                request=MagicMock(), session_id=session_id,
                since_update_id=0, token="tok",
            )
    finally:
        for p in patches:
            p.stop()


async def test_parked_fresh_load_count_tie_serves_live_snapshot():
    """Running background task + count tie -> the live buffer wins the heal."""
    session_id = "u3-parked-fresh"
    session = {"id": session_id, "title": "Parked", "sdk_session_id": None,
               "total_tokens_out": 0}
    task_registry.start_task(session_id, "task-1", "agent", "bg work")
    try:
        result = await _fresh_load_with_tie(session_id, session)
    finally:
        task_registry.clear_session(session_id)

    assert isinstance(result, dict)
    # The buffer's real update id is served — the client re-enters the delta
    # path instead of looping since=0 fresh loads of the stale store transcript.
    assert result["last_update_id"] == 42
    # Fresh (in-place mutated) sub-agent progress, not the store's frozen copy.
    assert result["messages"][1]["agentChildren"] == [
        {"id": "c1"}, {"id": "c2"}, {"id": "c3"},
    ]
    assert result["activeTasks"][0]["id"] == "task-1"


async def test_idle_fresh_load_count_tie_still_serves_store():
    """No running tasks + count tie -> the store wins (crash-heal preserved)."""
    session_id = "u3-idle-fresh"
    session = {"id": session_id, "title": "Idle", "sdk_session_id": None,
               "total_tokens_out": 0}
    task_registry.clear_session(session_id)  # ensure no stray tasks

    result = await _fresh_load_with_tie(session_id, session)

    assert isinstance(result, dict)
    # Store transcript served: last_update_id 0 (fresh-load reset), store's
    # agentChildren — the stale-buffer heal for crashed/empty-seeded turns.
    assert result["last_update_id"] == 0
    assert result["messages"][1]["agentChildren"] == [{"id": "c1"}]
