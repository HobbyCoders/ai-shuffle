"""HTTP doubles: an httpx client served by a handler, and a canned response."""
import json
from unittest.mock import MagicMock

import httpx


def mock_transport_client(handler):
    """An AsyncClient whose requests are served by ``handler`` (no network)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fake_response(status=200, body=None, text=""):
    """A MagicMock response with ``status_code``, ``json()`` and ``text``."""
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body if body is not None else {})
    r.text = text or json.dumps(body or {})
    return r
