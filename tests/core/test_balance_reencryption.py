"""Real secret rotation must discover and preserve a bound balance key."""
from app.core import encryption
from app.core.config import settings
from app.db import database as db, provider_balances


def test_balance_key_rotates_and_stays_bound(mock_db, monkeypatch):
    encryption.set_encryption_key("old-pw", db)
    db.create_model_provider(provider_id="balance-rotate", name="xAI", provider_type="xai",
                             base_url="https://api.x.ai/v1", auth_token=encryption.encrypt_value("model-key"))
    row = db.get_model_provider("balance-rotate", include_token=True)
    assert provider_balances.bind(row, settings.effective_instance_id, encryption.encrypt_value("management-rotation-test"))
    before = db.get_model_provider(row["id"], include_token=True)
    salt = encryption.get_or_create_salt(db)
    old = encryption.derive_key_from_password("old-pw", salt)
    new = encryption.derive_key_from_password("new-pw", salt)
    plan, failures = encryption.build_reencryption_plan(old, new, db)
    assert not failures
    assert any(item.kind == "model_provider_balance" and item.ref == row["id"] for item in plan)
    encryption.apply_reencryption_plan(plan, db)
    monkeypatch.setattr(encryption, "_encryption_key", new)
    after = db.get_model_provider(row["id"], include_token=True)
    assert after["balance_key"] != before["balance_key"]
    assert encryption.decrypt_value(after["balance_key"]) == "management-rotation-test"
    assert after["balance_instance_id"] == before["balance_instance_id"]
    assert after["balance_snapshot"] == before["balance_snapshot"]
