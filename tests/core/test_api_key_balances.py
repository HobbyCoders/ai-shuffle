import pytest

from app.core.api_key_balances import parse_xai, usd_cents


def test_configured_balance_waits_for_first_reading(monkeypatch):
    from app.core import api_key_balances as balances
    from app.core.config import settings
    monkeypatch.setattr(balances, "credential_fingerprint", lambda row: "test-revision")
    row = {"balance_key": "test-only", "balance_instance_id": settings.effective_instance_id}
    assert balances.status(row)["status"] == "pending"
    import json
    row["balance_snapshot"] = json.dumps({"credential": "test-revision", "last_error_kind": "unavailable"})
    assert balances.status(row)["status"] == "unavailable"
    row["balance_snapshot"] = json.dumps({"credential": "test-revision",
                                          "last_deferred_reason": "shared_usage_budget", "deferred_until": 12345})
    status = balances.status(row)
    assert status["status"] == "deferred"
    assert status["last_deferred_reason"] == "shared_usage_budget"
    assert "deferred_until" not in status
    assert status["last_error_kind"] is None
    assert balances.status({})["status"] == "unavailable"


def test_xai_ledger_sign_and_cents_not_dollars():
    result = parse_xai({"total": {"val": "-4500"}}, {
        "coreInvoice": {"totalWithCorr": {"val": "100"}, "prepaidCreditsUsed": {"val": "20"}},
        "effectiveSpendingLimit": "20000"})
    assert result["balance_remaining"] == 45
    assert result["spend_month"] == 1.2
    assert result["spending_limit"] == 200
    assert parse_xai({}) == {"balance_remaining": 0, "currency": "USD"}
    assert parse_xai({"total": {}}, {})["spend_month"] == 0
    assert usd_cents({"val": "1"}) == .01
    assert usd_cents({"val": "1.5"}) == .015
    with pytest.raises(ValueError):
        usd_cents({"val": "45", "unit": "dollars"})


def test_management_template_cannot_escape_pinned_host_or_team_path():
    from app.core.usage_sources import endpoint_for
    assert endpoint_for("xai_management", "https://management-api.x.ai/v1/billing/teams/team-test/prepaid/balance")
    for url in ["https://evil.test/v1/billing/teams/team-test/prepaid/balance",
                "https://management-api.x.ai/v1/billing/teams/../prepaid/balance",
                "https://management-api.x.ai/v1/billing/teams/team-test/prepaid/balance?extra=1"]:
        with pytest.raises(ValueError):
            endpoint_for("xai_management", url)
