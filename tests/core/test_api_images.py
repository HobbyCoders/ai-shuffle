"""API image transport is inert until code-reviewed verification enables it."""
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core import native_generation_settings as native, native_image_verification as verification
from app.core.provider import api_images
from app.db import database

Enabled = tuple[verification.Verification, dict[str, Any]]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> Enabled:
    record = verification.Verification(
        "probe-model", "1024x1024", "low", "png", "0.02", "fixture only", "5",
    )
    monkeypatch.setitem(verification.VERIFIED_API_IMAGES, "openai", record)
    row = {"id": "account", "provider_type": "openai", "enabled": True,
           "base_url": "https://api.openai.com/v1", "auth_token": "fake-api-key"}
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: row)
    return record, row


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 302])
async def test_rejection_never_retries_or_leaks_provider_body(
    enabled: Enabled, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    record, _ = enabled
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "private-provider-detail"}},
                              headers={"Location": "https://example.com/"})

    monkeypatch.setattr(
        api_images, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(send)),
    )
    items, warning = await api_images.generate(
        "account", "openai", "tree", native.NativeImageSettings(**record.settings()),
    )
    assert items == [] and len(calls) == 1
    assert "private-provider-detail" not in warning and "fake-api-key" not in warning


async def test_unknown_submission_outcome_never_retries(
    enabled: Enabled, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _ = enabled
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("private transport error")

    monkeypatch.setattr(
        api_images, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(send)),
    )
    items, warning = await api_images.generate(
        "account", "openai", "tree", native.NativeImageSettings(**record.settings()),
    )
    assert items == [] and len(calls) == 1 and "private transport error" not in warning


@pytest.mark.parametrize("change", [{"base_url": "https://example.com/v1"}, {"enabled": False},
                                    {"provider_type": "chatgpt"}, {"auth_token": None}])
async def test_changed_account_or_custom_host_never_opens_transport(
    enabled: Enabled, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
) -> None:
    record, row = enabled
    row.update(change)
    client = AsyncMock()
    monkeypatch.setattr(api_images, "http_client", client)
    items, _ = await api_images.generate(
        "account", "openai", "tree", native.NativeImageSettings(**record.settings()),
    )
    assert items == []
    client.assert_not_called()


def test_verification_prices_bound_input_and_reject_unknown_rates() -> None:
    record = verification.Verification("model", "1024x1024", "low", "png", "0.02", "fixture", "5")
    assert record.price("tree")["total"] == "0.02514"
    assert record.price()["total"] == "0.66512"
    for rate in ("NaN", "-1", "Infinity"):
        invalid = verification.Verification("model", "1k", None, "jpeg", rate, "fixture", "0")
        with pytest.raises(ValueError):
            invalid.price()
