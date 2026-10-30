"""v94 erases retired billing secrets without dropping mixed-version columns."""

from app.db import database as db, schema_postgres
from app.db.base import column_exists


RETIRED = ("openai_admin_key", "openai_billing_org_id", "openai_billing_instance_id",
           "manual_balance_usd", "manual_balance_set_at")


def test_v94_clears_secrets_readings_and_settings_but_keeps_other_providers(mock_db):
    for id, kind in (("api", "openai"), ("subscription", "chatgpt"), ("xai", "xai"),
                     ("router", "openrouter")):
        db.create_model_provider(id, id, kind, "https://example.test", auth_token="inference-" + id)
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE model_providers SET openai_admin_key = %s, openai_billing_org_id = %s, "
                    "openai_billing_instance_id = %s, manual_balance_usd = %s, manual_balance_set_at = %s",
                    ("enc:retired-admin", "org-retired", "old-worker", 20, 1))
        cur.execute("UPDATE model_providers SET balance_key = %s, balance_instance_id = %s, balance_snapshot = %s "
                    "WHERE id IN ('api', 'xai')", ("enc:billing", "old-worker", "{}"))
    db.upsert_provider_usage_snapshot("api", "openai", None, [], {"spend_month": 9}, {}, 123)
    db.upsert_provider_usage_snapshot("balance:api", "openai_balance", None, [], {"balance_remaining": 3}, {}, 123)
    db.upsert_provider_usage_snapshot("subscription", "chatgpt", "pro", [{"type": "five_hour", "utilization": 12}],
                                      {"balance": 2}, {}, 123)
    retired_keys = ["openai_billing_old-secret", "provider_observation:openai_admin:api",
                    "provider_observation:openai_platform_balance:api", "provider_observation:openai_manual:api"]
    kept_keys = ["openrouter_billing_key:router", "provider_observation:xai_management:xai", "fal_api_key"]
    for key in retired_keys + kept_keys:
        db.set_system_setting(key, "enc:stored-test-value")
    with db.get_db() as conn:
        cur = conn.cursor()
        assert db._retire_openai_billing(cur)
        assert db._retire_openai_billing(cur) == []
        for column in RETIRED:
            assert column_exists(cur, "model_providers", column)
        cur.execute("SELECT * FROM model_providers")
        rows = {row["id"]: row for row in cur.fetchall()}
    assert all(row[column] is None for row in rows.values() for column in RETIRED)
    assert rows["api"]["balance_key"] is None
    assert rows["api"]["balance_instance_id"] is None
    assert rows["api"]["balance_snapshot"] is None
    assert rows["xai"]["balance_key"] == "enc:billing"
    assert rows["xai"]["balance_instance_id"] == "old-worker"
    assert rows["xai"]["balance_snapshot"] == "{}"
    assert all(row["auth_token"] == "inference-" + id for id, row in rows.items())
    assert all(db.get_system_setting(key) is None for key in retired_keys)
    assert all(db.get_system_setting(key) == "enc:stored-test-value" for key in kept_keys)
    snapshots = db.get_all_provider_usage_snapshots()
    assert [s["provider_id"] for s in snapshots] == ["subscription"]
    assert snapshots[0]["windows"][0]["utilization"] == 12
    assert db.SCHEMA_VERSION == schema_postgres.SCHEMA_VERSION >= 94


def test_legacy_peer_cannot_restore_openai_billing_fields(mock_db):
    from app.network import engine, policy

    coll = policy.collection("model_providers")
    assert not set(RETIRED) & set(coll.fields)
    assert "balance_key" in coll.encrypted  # xAI still shares its management key.
    db.create_model_provider("api", "OpenAI", "openai", "https://api.openai.com/v1")
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM model_providers WHERE id = 'api'")
        record = engine.serialize_row(coll, dict(cur.fetchone()))
        record.update({
            "name": "Old peer edit", "openai_admin_key": "retired-admin", "openai_billing_org_id": "org-retired",
            "manual_balance_usd": 20, "manual_balance_set_at": 1,
            "balance_key": "retired-platform-session", "_enc": ["openai_admin_key", "balance_key"],
        })
        engine.materialize(cur, coll, "api", record)
        cur.execute("SELECT * FROM model_providers WHERE id = 'api'")
        row = cur.fetchone()
    assert row["name"] == "Old peer edit"
    assert all(row[column] is None for column in RETIRED)
    assert row["balance_key"] is None
    assert row["balance_instance_id"] is None
    assert "balance_key" not in engine.serialize_row(coll, row)
