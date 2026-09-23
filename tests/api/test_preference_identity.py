from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request


@pytest.mark.asyncio
@pytest.mark.parametrize("paired,authenticated", [(False, True), (True, True), (False, False)])
async def test_auth_status_preference_identity_is_account_not_device(paired, authenticated):
    from app.api import auth

    headers = [(b'host', b'localhost'), (b'origin', b'http://localhost')]
    if authenticated:
        headers.append((b'cookie', b'session=shr_test-recovery'))
    request = Request({'type': 'http', 'scheme': 'http', 'path': '/api/v1/auth/status',
                       'headers': headers, 'client': ('127.0.0.1', 1234),
                       'server': ('127.0.0.1', 80), 'shuffle.ingress': 'local'})
    with (
        patch.object(auth, "auth_service") as service,
        patch.object(auth, "get_paired_client_from_token", return_value={"id": "phone", "device_name": "Phone"} if paired else None),
        patch.object(auth.owner_auth, "preference_identity", return_value="alice"),
        patch.object(auth, "is_github_authenticated_cached", return_value=False),
        patch.object(auth.owner_auth, "local_access", return_value=False),
    ):
        service.get_admin_username.return_value = "alice"
        service.validate_session.return_value = authenticated and not paired
        result = await auth.get_auth_status(request)

    expected = {"user_type": "paired", "user_id": "phone"} if paired else {"user_type": "admin", "user_id": "alice"}
    assert result["preference_identity"] == (expected if authenticated else None)
    if paired:
        assert result["username"] == "Phone"
        assert result["is_admin"] is False
