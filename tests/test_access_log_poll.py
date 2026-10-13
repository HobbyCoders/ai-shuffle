"""Expected browser long polls are quiet; real slow requests/errors are not."""
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.main import AccessLogMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,status,warning", [
    ("POST", "/api/v1/browser/panel/control/poll", 200, False),
    ("POST", "/api/v1/browser/panel/control/poll", 403, True),
    ("POST", "/api/v1/browser/panel/control/poll", 500, True),
    ("GET", "/api/v1/browser/panel/control/poll", 200, True),
    ("POST", "/api/v1/browser/panel/control/register", 200, True),
    ("GET", "/api/v1/sessions", 200, True),
])
async def test_expected_long_poll_only(method, path, status, warning):
    request = Request({"type": "http", "method": method, "path": path,
                       "headers": [], "client": ("127.0.0.1", 4321)})
    middleware = AccessLogMiddleware(AsyncMock())
    response = Response(status_code=status)
    clock = iter([0.0, 20.0])
    with patch("app.main.time.monotonic", side_effect=lambda: next(clock)), patch("app.main.logger.warning") as log:
        actual = await middleware.dispatch(request, AsyncMock(return_value=response))
    assert actual is response
    assert log.called is warning
