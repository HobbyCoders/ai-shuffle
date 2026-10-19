import pytest

from app.core.api_key_balances import parse_openai, parse_xai, usd_cents
from app.core.openai_platform_signin import dashboard_url, session_key


def test_openai_dollars_and_zero():
    assert parse_openai({"total_available": "0", "total_granted": "20.50", "total_used": 20.5}) == {
        "balance_remaining": 0, "total_credits": 20.5, "total_usage": 20.5, "currency": "USD"}


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", None, {}, []])
def test_invalid_openai_money(value):
    with pytest.raises((ValueError, KeyError)):
        parse_openai({"total_available": value, "total_granted": 10, "total_used": 0})


def test_xai_ledger_sign_and_cents_not_dollars():
    result = parse_xai({"total": {"val": "-4500"}}, {
        "coreInvoice": {"totalWithCorr": {"val": "100"}, "prepaidCreditsUsed": {"val": "20"}},
        "effectiveSpendingLimit": "20000"})
    assert result["balance_remaining"] == 45
    assert result["spend_month"] == 1.2
    assert result["spending_limit"] == 200
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


def test_session_capture_only_exact_dashboard_origin():
    assert dashboard_url("https://api.openai.com/dashboard/onboarding/login")
    assert dashboard_url("https://api.openai.com/v1/dashboard/billing/credit_grants")
    for url in ["https://evil.test/v1/dashboard/", "https://api.openai.com.evil.test/dashboard/", "http://api.openai.com/dashboard/", "https://api.openai.com:444/dashboard/", "https://api.openai.com/v1/responses"]:
        assert not dashboard_url(url)
    assert session_key({"authorization": "Bearer sess-test-only"}) == "sess-test-only"
    assert session_key({"Authorization": "Bearer sk-test-only"}) is None
    assert session_key({"Authorization": "Bearer sess-bad key"}) is None
