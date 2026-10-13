"""
Security tests for WebhookConnector.verify_inbound_request (Phase 1.4).

- token comparison must be timing-safe (hmac.compare_digest) and correct
- a connector with NEITHER inbound_token NOR secret must FAIL CLOSED (return
  False) rather than accept any unauthenticated request
"""

from unittest.mock import MagicMock

from app.core.connectors.webhook_connector import WebhookConnector


def _make_connector(config):
    return WebhookConnector(
        connector_id="wh-test",
        connector_type="webhook",
        config=config,
        on_incoming_message=MagicMock(),
        on_permission_response=MagicMock(),
    )


class TestVerifyInboundToken:
    def test_correct_token_returns_true(self):
        c = _make_connector({"inbound_token": "s3cret"})
        assert c.verify_inbound_request("body", token="s3cret") is True

    def test_wrong_token_returns_false(self):
        c = _make_connector({"inbound_token": "s3cret"})
        assert c.verify_inbound_request("body", token="nope") is False

    def test_missing_token_returns_false(self):
        c = _make_connector({"inbound_token": "s3cret"})
        assert c.verify_inbound_request("body", token=None) is False


class TestVerifyInboundNoCredentials:
    def test_no_token_and_no_secret_fails_closed(self):
        """An inbound connector with no credentials must refuse the request."""
        c = _make_connector({})  # neither inbound_token nor secret
        assert c.verify_inbound_request("body", token="anything") is False
        assert c.verify_inbound_request("body", token=None) is False

    def test_no_credentials_logs_warning(self, caplog):
        c = _make_connector({})
        with caplog.at_level("WARNING"):
            result = c.verify_inbound_request("body")
        assert result is False
        assert any("neither" in r.message or "refused" in r.message.lower()
                   for r in caplog.records)


class TestVerifyInboundHmac:
    def test_valid_signature_returns_true(self):
        import hashlib
        import hmac as _hmac

        secret = "topsecret"
        body = "payload-body"
        sig = _hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

        c = _make_connector({"secret": secret})
        assert c.verify_inbound_request(body, signature=sig) is True
        # With the "sha256=" prefix too.
        assert c.verify_inbound_request(body, signature=f"sha256={sig}") is True

    def test_invalid_signature_returns_false(self):
        c = _make_connector({"secret": "topsecret"})
        assert c.verify_inbound_request("payload-body", signature="deadbeef") is False
