from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("paired,authenticated", [(False, True), (True, True), (False, False)])
async def test_auth_status_preference_identity_is_account_not_device(paired, authenticated):
    from app.api import auth

    request = MagicMock()
    request.cookies = {"session": "test-session"} if authenticated else {}
    with (
        patch.object(auth, "auth_service") as service,
        patch.object(auth, "get_paired_client_from_token", return_value={"device_name": "Phone"} if paired else None),
        patch.object(auth.direct_oauth, "ensure_fresh_credentials", new=AsyncMock()),
        patch.object(auth, "_sync_back_active_account_safe"),
        patch.object(auth, "is_github_authenticated_cached", return_value=False),
    ):
        service.get_admin_username.return_value = "alice"
        service.validate_session.return_value = authenticated and not paired
        result = await auth.get_auth_status(request)

    assert result["preference_identity"] == ({"user_type": "admin", "user_id": "alice"} if authenticated else None)
    if paired:
        assert result["username"] == "Phone"
        assert result["is_admin"] is False
