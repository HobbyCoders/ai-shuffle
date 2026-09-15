"""Organization-bound browser balances remain isolated from API-key polling."""

import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import provider_usage as usage
from app.core.config import settings
from app.db import database as db


@pytest.fixture
def provider(mock_db):
    provider_id = str(uuid.uuid4())
    db.create_model_provider(provider_id, "Billing test", "openai", "https://api.openai.com", auth_token="test-model-key")
    return provider_id


def bind(provider, org="org-test", instance=None):
    return db.set_openai_billing_binding(provider, org, instance or settings.effective_instance_id)


def save(provider, amount=21.09, org="org-test", instance=None):
    return db.upsert_provider_usage_snapshot(
        provider, "openai", None, [], {"balance_remaining": amount},
        {"source": "browser_platform", "organization_id": org}, 1000,
        expected_binding=(org, instance or settings.effective_instance_id),
    )


def snapshot(provider):
    return next((s for s in db.get_all_provider_usage_snapshots() if s["provider_id"] == provider), None)


def test_binding_is_public_but_does_not_change_api_credentials(provider):
    row = bind(provider)
    assert row["openai_billing_org_id"] == "org-test"
    assert row["openai_billing_instance_id"] == settings.effective_instance_id
    assert "auth_token" not in row
    assert db.get_model_provider(provider, include_token=True)["auth_token"] == "test-model-key"
    db.update_model_provider(provider, openai_billing_org_id="org-untrusted")
    assert db.get_model_provider(provider)["openai_billing_org_id"] == "org-test"


@pytest.mark.parametrize("change", [{"enabled": False}, {"base_url": "https://example.com"}, {"provider_type": "custom"}])
def test_provider_changed_during_login_cannot_bind(provider, change):
    db.update_model_provider(provider, **change)
    assert bind(provider) is None


def test_rebind_and_disconnect_clear_browser_balance_only(provider):
    bind(provider)
    assert save(provider)
    bind(provider, "org-new")
    assert snapshot(provider) is None
    assert save(provider, 0, "org-new")
    db.clear_openai_billing_binding(provider)
    assert snapshot(provider) is None
    assert db.get_model_provider(provider, include_token=True)["auth_token"] == "test-model-key"
    db.upsert_provider_usage_snapshot(provider, "openai", None, [], {"balance_remaining": 5}, {}, 2000)
    db.clear_openai_billing_binding(provider)
    assert snapshot(provider)["credits"]["balance_remaining"] == 5


def test_failure_keeps_balance_source_and_last_success_time(provider):
    bind(provider)
    save(provider, 0)
    db.record_provider_usage_failure(provider, "openai", "billing_sign_in_required", ("org-test", settings.effective_instance_id))
    row = snapshot(provider)
    assert row["credits"]["balance_remaining"] == 0
    assert row["fetched_at"] == 1000
    assert row["raw"]["billing_fetched_at"] == 1000
    assert row["raw"]["source"] == "browser_platform"
    assert row["raw"]["organization_id"] == "org-test"
    assert row["raw"]["usage_error"] == "billing_sign_in_required"


def test_remote_rebind_rejects_both_late_success_and_failure(provider):
    bind(provider, "org-new", "another-installation")
    assert save(provider, 8, "org-new", "another-installation")
    assert save(provider, 99) is False
    db.record_provider_usage_failure(provider, "openai", "billing_timeout", ("org-test", settings.effective_instance_id))
    assert snapshot(provider)["credits"]["balance_remaining"] == 8
    assert "usage_error" not in snapshot(provider)["raw"]


def test_other_installation_cannot_take_over_or_clear_binding(provider):
    bind(provider, instance="remote-installation")
    assert bind(provider) is None
    assert db.clear_openai_billing_binding(provider, settings.effective_instance_id) is None
    assert db.get_model_provider(provider)["openai_billing_instance_id"] == "remote-installation"


def test_api_key_attempts_cannot_replace_bound_browser_balance(provider):
    bind(provider)
    save(provider)
    assert db.upsert_provider_usage_snapshot(provider, "openai", None, [], {"balance_remaining": 999}, {}, 2000) is False
    db.record_provider_usage_failure(provider, "openai", "billing_access_denied")
    row = snapshot(provider)
    assert row["credits"]["balance_remaining"] == 21.09
    assert "usage_error" not in row["raw"]
    db.upsert_provider_usage_snapshot(provider, "openai", None, [{"type": "requests"}], None, None, 3000)
    row = snapshot(provider)
    assert row["credits"]["balance_remaining"] == 21.09
    assert row["raw"]["billing_fetched_at"] == 1000
