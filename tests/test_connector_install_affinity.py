"""
Tests for connector install affinity (multi-install runner_id):

* ``ConnectorManager.start_all`` only starts connectors this install owns,
  claims unowned (NULL) rows race-safely, and skips foreign-pinned ones.
* ``database.claim_connector_runner`` performs an atomic NULL-only claim.

Two installs share one Postgres; without this, both would connect the same
bot token and double-handle every inbound message.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.connectors.manager import ConnectorManager


ME = "install-me"
OTHER = "install-other"


@pytest.fixture
def manager():
    return ConnectorManager()


def _patch_settings():
    """Patch the settings singleton so start_all sees effective_instance_id=ME."""
    mock_settings = MagicMock()
    mock_settings.effective_instance_id = ME
    return patch("app.core.config.settings", mock_settings)


# =============================================================================
# start_all filtering
# =============================================================================


class TestStartAllFiltering:
    @pytest.mark.asyncio
    async def test_starts_connector_owned_by_me(self, manager):
        rows = [{"id": "c-mine", "runner_id": ME}]
        with _patch_settings(), \
                patch("app.core.connectors.manager.database.get_active_connectors", return_value=rows), \
                patch("app.core.connectors.manager.database.claim_connector_runner") as claim, \
                patch.object(manager, "start_connector", new_callable=AsyncMock) as start:
            await manager.start_all()

        start.assert_awaited_once_with("c-mine")
        claim.assert_not_called()  # already owned — no claim attempt

    @pytest.mark.asyncio
    async def test_claims_and_starts_unowned_when_claim_wins(self, manager):
        rows = [{"id": "c-null", "runner_id": None}]
        with _patch_settings(), \
                patch("app.core.connectors.manager.database.get_active_connectors", return_value=rows), \
                patch("app.core.connectors.manager.database.claim_connector_runner", return_value=True) as claim, \
                patch.object(manager, "start_connector", new_callable=AsyncMock) as start:
            await manager.start_all()

        claim.assert_called_once_with("c-null", ME)
        start.assert_awaited_once_with("c-null")

    @pytest.mark.asyncio
    async def test_skips_unowned_when_claim_lost(self, manager):
        rows = [{"id": "c-null", "runner_id": None}]
        with _patch_settings(), \
                patch("app.core.connectors.manager.database.get_active_connectors", return_value=rows), \
                patch("app.core.connectors.manager.database.claim_connector_runner", return_value=False) as claim, \
                patch.object(manager, "start_connector", new_callable=AsyncMock) as start:
            await manager.start_all()

        claim.assert_called_once_with("c-null", ME)
        start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_connector_pinned_elsewhere(self, manager):
        rows = [{"id": "c-foreign", "runner_id": OTHER}]
        with _patch_settings(), \
                patch("app.core.connectors.manager.database.get_active_connectors", return_value=rows), \
                patch("app.core.connectors.manager.database.claim_connector_runner") as claim, \
                patch.object(manager, "start_connector", new_callable=AsyncMock) as start:
            await manager.start_all()

        start.assert_not_awaited()
        claim.assert_not_called()  # never claim a foreign-pinned connector

    @pytest.mark.asyncio
    async def test_mixed_rows_only_starts_eligible(self, manager):
        rows = [
            {"id": "c-mine", "runner_id": ME},
            {"id": "c-null", "runner_id": None},
            {"id": "c-foreign", "runner_id": OTHER},
        ]
        with _patch_settings(), \
                patch("app.core.connectors.manager.database.get_active_connectors", return_value=rows), \
                patch("app.core.connectors.manager.database.claim_connector_runner", return_value=True), \
                patch.object(manager, "start_connector", new_callable=AsyncMock) as start:
            await manager.start_all()

        started = {call.args[0] for call in start.await_args_list}
        assert started == {"c-mine", "c-null"}


# =============================================================================
# claim_connector_runner (atomic NULL-only claim)
# =============================================================================


class TestClaimConnectorRunner:
    @pytest.mark.asyncio
    async def test_claim_unowned_succeeds_and_sets_runner(self, mock_db):
        from app.db import database

        database.create_connector(
            connector_id="c1",
            connector_type="webhook",
            name="Test",
            config={"mode": "inbound", "inbound_path": "t"},
            events=[],
            api_user_id=None,
            runner_id=None,
        )

        assert database.claim_connector_runner("c1", ME) is True
        assert database.get_connector("c1")["runner_id"] == ME

    @pytest.mark.asyncio
    async def test_claim_already_owned_fails_and_preserves_owner(self, mock_db):
        from app.db import database

        database.create_connector(
            connector_id="c1",
            connector_type="webhook",
            name="Test",
            config={"mode": "inbound", "inbound_path": "t"},
            events=[],
            api_user_id=None,
            runner_id=OTHER,
        )

        # Someone else already owns it → our claim is a no-op.
        assert database.claim_connector_runner("c1", ME) is False
        assert database.get_connector("c1")["runner_id"] == OTHER

    @pytest.mark.asyncio
    async def test_second_claim_by_same_runner_is_idempotent_false(self, mock_db):
        from app.db import database

        database.create_connector(
            connector_id="c1",
            connector_type="webhook",
            name="Test",
            config={"mode": "inbound", "inbound_path": "t"},
            events=[],
            api_user_id=None,
            runner_id=None,
        )

        assert database.claim_connector_runner("c1", ME) is True
        # Row is no longer NULL, so a repeat claim updates nothing.
        assert database.claim_connector_runner("c1", ME) is False
        assert database.get_connector("c1")["runner_id"] == ME

    @pytest.mark.asyncio
    async def test_create_connector_defaults_runner_null(self, mock_db):
        from app.db import database

        database.create_connector(
            connector_id="c1",
            connector_type="webhook",
            name="Test",
            config={"mode": "inbound", "inbound_path": "t"},
            events=[],
            api_user_id=None,
        )
        assert database.get_connector("c1")["runner_id"] is None
