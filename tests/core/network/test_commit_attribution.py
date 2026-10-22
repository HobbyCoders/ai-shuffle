"""The receiving worker reads the replicated owner policy for Task children."""
import pytest

from app.core import commit_attribution
from app.core.provider_agent import worker_session_fields
from app.db import database
from app.network import engine
from fluid.context.attribution import prompt_block
from tests.core.network.harness import exchange, join
from tests.core.test_commit_attribution import capture_engines, connect  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_remote_task_child_reads_synced_owner_setting(
    workers, tmp_path, capture_engines, enabled,
):
    owner, peer = workers("owner"), workers("peer")
    with owner.active():
        engine.create_network("Attribution test")
        commit_attribution.write(not enabled)
        engine.scan_all()
    join(owner, peer)
    with owner.active():
        commit_attribution.write(enabled)
        engine.scan_all()
    result = exchange(owner, peer)
    assert result.rejected == result.retry == result.pending == 0
    with peer.active():
        assert commit_attribution.read() is enabled
        database.create_session(session_id="parent", profile_id="default", title="Parent")
        # This is the shared local Task/remote-task child-producing row builder.
        database.create_session(**worker_session_fields(
            worker_session_id="child", profile_id="default", title="Task child",
            parent_session_id="parent", provider_id=None, model_id="gpt-5", effort=None,
        ))
        adapter = await connect(tmp_path, session_id="child")
        try:
            assert database.get_session("child")["parent_session_id"] == "parent"
            assert adapter._engine.kwargs["system"].count(prompt_block(enabled)) == 1
        finally:
            await adapter.disconnect()
