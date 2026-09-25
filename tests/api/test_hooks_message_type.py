"""Message hooks are never delivered, so the hooks API refuses to create one
or to turn a hook into one (422); other edits of an existing message hook,
such as disabling it, still work so it can be cleaned up."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import hooks as hooks_api
from app.core.models import MESSAGE_HOOKS_NOT_CREATABLE, HookCreate, HookUpdate
from tests.api.helpers import fake_request


def _message_hook():
    return HookCreate(name="m", event="Stop", hook_type="message", message_template="hi")


async def test_creating_an_app_message_hook_is_refused():
    db = MagicMock()
    with patch.object(hooks_api, "database", db), pytest.raises(HTTPException) as exc:
        await hooks_api.create_app_hook(hook_data=_message_hook(), request=fake_request(), token="t")
    assert exc.value.status_code == 422
    assert exc.value.detail == MESSAGE_HOOKS_NOT_CREATABLE
    db.create_hook.assert_not_called()


async def test_creating_a_project_message_hook_is_refused():
    db = MagicMock()
    with patch.object(hooks_api, "database", db), pytest.raises(HTTPException) as exc:
        await hooks_api.create_project_hook(
            project_id="p", hook_data=_message_hook(), request=fake_request(), token="t"
        )
    assert exc.value.status_code == 422
    db.create_hook.assert_not_called()


async def test_changing_a_hook_to_a_message_hook_is_refused():
    db = MagicMock()
    db.get_hook.return_value = {"id": "h1", "project_id": None}
    with patch.object(hooks_api, "database", db), pytest.raises(HTTPException) as exc:
        await hooks_api.update_hook(
            hook_id="h1", hook_data=HookUpdate(hook_type="message"),
            request=fake_request(), token="t",
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == MESSAGE_HOOKS_NOT_CREATABLE
    db.update_hook.assert_not_called()


async def test_an_existing_message_hook_can_still_be_disabled():
    db = MagicMock()
    db.get_hook.return_value = {"id": "h1", "project_id": None, "hook_type": "message"}
    with patch.object(hooks_api, "database", db):
        await hooks_api.update_hook(
            hook_id="h1", hook_data=HookUpdate(enabled=False),
            request=fake_request(), token="t",
        )
    db.update_hook.assert_called_once()
