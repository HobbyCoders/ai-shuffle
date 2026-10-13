"""Only fresh authenticated worker responses can suppress duplicate status calls."""
import json

import pytest
from fastapi import HTTPException

from app.core import account_hub


def test_authorization_proof_is_bounded_scoped_and_cleared_on_rejection(monkeypatch):
    binding = {'hub_url': 'https://hub.example', 'server_id': 'worker', 'encrypted_secret': 'a'}
    now = [0]
    reply = {'status': 200, 'body': {'devices': []}}

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            self.status = reply['status']
            return self

        def read(self, limit):
            return json.dumps(reply['body']).encode()

        def close(self):
            pass

    monkeypatch.setattr(account_hub, '_recent_authorization', {})
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', Connection)
    monkeypatch.setattr(account_hub, 'trusted_origin', lambda value: value)
    monkeypatch.setattr(account_hub, 'identifier', lambda value: value)
    monkeypatch.setattr(account_hub, 'secret_for', lambda _: 'test-secret')
    monkeypatch.setattr(account_hub.time, 'monotonic', lambda: now[0])
    account_hub.call(binding, 'GET', 'account')
    assert not account_hub.recently_authorized(binding)
    account_hub.call(binding, 'GET', 'network/pending')
    assert account_hub.recently_authorized(binding)
    assert not account_hub.recently_authorized({**binding, 'encrypted_secret': 'b'})
    assert not account_hub.recently_authorized({**binding, 'server_id': 'another'})
    # The proof outlives one ten-minute directory poll, so the remote-access
    # supervisor never asks ``status`` between two polls.
    now[0] = account_hub.BACKGROUND_CHECK_SECONDS
    assert account_hub.recently_authorized(binding)
    now[0] = account_hub.AUTHORIZATION_REUSE_SECONDS
    assert not account_hub.recently_authorized(binding)
    # The merged poll (``GET network``) refreshes it too.
    account_hub.call(binding, 'GET', 'network')
    assert account_hub.recently_authorized(binding)
    account_hub.call(binding, 'GET', 'network/pending')
    reply['status'] = 403
    with pytest.raises(HTTPException):
        account_hub.call(binding, 'GET', 'network/pending')
    assert not account_hub.recently_authorized(binding)
