"""Envelope, membership and policy contracts."""
import pytest

from app.network import crypto, envelope, membership, policy


def _device():
    signing = crypto.generate_signing_key()
    enc = crypto.generate_encryption_key()
    public = crypto.signing_public_key(signing)
    return {
        "signing": signing, "enc": enc, "public": public,
        "enc_public": crypto.encryption_public_key(enc),
        "id": membership.device_id_for(public),
    }


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def test_envelope_round_trip_and_tamper_detection():
    dev = _device()
    key = crypto.generate_network_key()
    env = envelope.build(network_id="net_1", collection="user_preferences", record_id="admin|@owner|layout",
                         op=envelope.OP_UPSERT, clock=5, origin_device_id=dev["id"], origin_incarnation=1,
                         origin_seq=1, schema_version=1, key_version=1, network_key=key,
                         signing_private=dev["signing"], record={"sidebarWidth": 320, "cost": 1.5})
    wire = env.to_dict()
    back = envelope.Envelope.from_dict(wire)
    assert back.verify_signature(dev["public"])
    assert back.open(key) == {"cost": 1.5, "sidebarWidth": 320}

    # Changing any header field breaks the signature and the AAD binding.
    tampered = dict(wire, record_id="admin|@owner|theme")
    bad = envelope.Envelope.from_dict(tampered)
    assert not bad.verify_signature(dev["public"])
    with pytest.raises(crypto.CryptoError):
        bad.open(key)

    # A different network key cannot open it.
    with pytest.raises(crypto.CryptoError):
        back.open(crypto.generate_network_key())


def test_envelope_shape_rules():
    dev = _device()
    key = crypto.generate_network_key()
    delete = envelope.build(network_id="n", collection="tags", record_id="t1", op=envelope.OP_DELETE, clock=1,
                            origin_device_id=dev["id"], origin_incarnation=1, origin_seq=2, schema_version=1,
                            key_version=1, network_key=key, signing_private=dev["signing"])
    assert delete.ciphertext == "" and delete.open(key) is None
    with pytest.raises(envelope.EnvelopeError):
        envelope.Envelope.from_dict(dict(delete.to_dict(), op="rename"))
    with pytest.raises(envelope.EnvelopeError):
        envelope.Envelope.from_dict(dict(delete.to_dict(), origin_seq=0))
    with pytest.raises(envelope.EnvelopeError):
        envelope.Envelope.from_dict(dict(delete.to_dict(), extra=1))
    with pytest.raises(envelope.EnvelopeError):
        envelope.Envelope.from_dict(dict(delete.to_dict(), clock="5"))


def test_last_writer_wins_order():
    assert envelope.compare(5, "dev_a", 4, "dev_b") == 1
    assert envelope.compare(4, "dev_a", 5, "dev_b") == -1
    assert envelope.compare(5, "dev_b", 5, "dev_a") == 1
    assert envelope.compare(5, "dev_a", 5, "dev_a") == 0


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

def test_membership_chain_certificate_revocation_and_key_rotation():
    founder, joiner, third = _device(), _device(), _device()
    net = "net_x"
    members = {}
    root = membership.issue_certificate(
        network_id=net, issuer_device_id=founder["id"], issuer_signing_private=founder["signing"],
        device_id=founder["id"], signing_public=founder["public"], encryption_public=founder["enc_public"],
        name="Desk", kind="worker", generation=0)
    members = membership.apply_record(root, network_id=net, members=members, founder_device_id=founder["id"])
    assert members[founder["id"]].status == "active"

    # A non-founder self-signed certificate is rejected.
    rogue = membership.issue_certificate(
        network_id=net, issuer_device_id=joiner["id"], issuer_signing_private=joiner["signing"],
        device_id=joiner["id"], signing_public=joiner["public"], encryption_public=joiner["enc_public"],
        name="Rogue", kind="worker", generation=1)
    with pytest.raises(membership.MembershipError):
        membership.apply_record(rogue, network_id=net, members=dict(members), founder_device_id=founder["id"])

    cert = membership.issue_certificate(
        network_id=net, issuer_device_id=founder["id"], issuer_signing_private=founder["signing"],
        device_id=joiner["id"], signing_public=joiner["public"], encryption_public=joiner["enc_public"],
        name="Laptop", kind="worker", generation=1)
    members = membership.apply_record(cert, network_id=net, members=members, founder_device_id=founder["id"])
    assert members[joiner["id"]].name == "Laptop"

    # The joiner can now admit a third device.
    cert3 = membership.issue_certificate(
        network_id=net, issuer_device_id=joiner["id"], issuer_signing_private=joiner["signing"],
        device_id=third["id"], signing_public=third["public"], encryption_public=third["enc_public"],
        name="Homelab", kind="worker", generation=2)
    members = membership.apply_record(cert3, network_id=net, members=members, founder_device_id=founder["id"])

    # Revoke the joiner; its certificate for the third device stays valid.
    revocation = membership.issue_revocation(
        network_id=net, issuer_device_id=founder["id"], issuer_signing_private=founder["signing"],
        device_id=joiner["id"], generation=3, last_seq_accepted=42)
    members = membership.apply_record(revocation, network_id=net, members=members, founder_device_id=founder["id"])
    assert members[joiner["id"]].status == "revoked" and members[joiner["id"]].last_seq_accepted == 42
    assert members[third["id"]].status == "active"

    # A revoked device can no longer issue anything.
    late = membership.issue_certificate(
        network_id=net, issuer_device_id=joiner["id"], issuer_signing_private=joiner["signing"],
        device_id=_device()["id"] if False else third["id"], signing_public=third["public"],
        encryption_public=third["enc_public"], name="X", kind="worker", generation=4)
    with pytest.raises(membership.MembershipError):
        membership.apply_record(late, network_id=net, members=dict(members), founder_device_id=founder["id"])
    late_revocation = membership.issue_revocation(
        network_id=net, issuer_device_id=joiner["id"], issuer_signing_private=joiner["signing"],
        device_id=founder["id"], generation=4, last_seq_accepted=0)
    with pytest.raises(membership.MembershipError):
        membership.apply_record(late_revocation, network_id=net, members=dict(members), founder_device_id=founder["id"])

    # Rotate the key to the remaining members only.
    new_key = crypto.generate_network_key()
    rotation = membership.issue_key_record(
        network_id=net, issuer_device_id=founder["id"], issuer_signing_private=founder["signing"],
        key_version=2, generation=3, new_key=new_key,
        recipients={founder["id"]: founder["enc_public"], third["id"]: third["enc_public"]})
    membership.apply_record(rotation, network_id=net, members=members, founder_device_id=founder["id"])
    assert membership.unwrap_from_key_record(rotation, third["id"], third["enc"]) == new_key
    assert membership.unwrap_from_key_record(rotation, joiner["id"], joiner["enc"]) is None
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key(joiner["enc"], rotation["wraps"][third["id"]])


def test_recovery_certificate_needs_the_network_key():
    founder, restored = _device(), _device()
    net = "net_r"
    key = crypto.generate_network_key()
    members = {}
    root = membership.issue_certificate(
        network_id=net, issuer_device_id=founder["id"], issuer_signing_private=founder["signing"],
        device_id=founder["id"], signing_public=founder["public"], encryption_public=founder["enc_public"],
        name="Desk", kind="worker", generation=0)
    members = membership.apply_record(root, network_id=net, members=members, founder_device_id=founder["id"])

    cert = membership.issue_recovery_certificate(
        network_id=net, network_key=key, signing_private=restored["signing"],
        encryption_public=restored["enc_public"], name="New laptop", kind="worker", generation=5)
    with pytest.raises(membership.MembershipError):
        membership.apply_record(cert, network_id=net, members=dict(members), founder_device_id=founder["id"])
    with pytest.raises(membership.MembershipError):
        membership.apply_record(cert, network_id=net, members=dict(members), founder_device_id=founder["id"],
                                network_key=crypto.generate_network_key())
    members = membership.apply_record(cert, network_id=net, members=members, founder_device_id=founder["id"],
                                      network_key=key)
    assert members[restored["id"]].status == "active"
    forged = dict(cert, name="Other name")
    with pytest.raises(membership.MembershipError):
        membership.apply_record(forged, network_id=net, members=dict(members), founder_device_id=founder["id"],
                                network_key=key)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def test_settings_policy_keeps_device_bound_keys_local():
    shared = ["tts_model", "image_model", "security_instructions", "onboarding_complete", "fal_api_key",
              "voice_model", "text_generation_model"]
    local = ["workspace_path", "workspace_path:desk-1234", "cluster_secret", "cluster_share_sessions",
             "mobile_account_hub_secret:desk-1234", "credential_key_state_v1", "claude_cli_relogin_notice:desk-1234",
             "encryption_salt", "owner_preference_identity", "schema_migrations_fingerprint",
             "migration:sessions_is_worker_backfilled", "installation_configured", "network_state", ""]
    assert all(policy.is_shared_setting_key(k) for k in shared)
    assert not any(policy.is_shared_setting_key(k) for k in local)


@pytest.mark.parametrize("resource,collections", [
    ("agents", {"agents"}),
    ("settings", {"system_settings", "model_providers", "permission_rules"}),
    ("hooks", {"project_hooks"}),
])
def test_catalog_changes_use_ui_resource_names(resource, collections):
    assert collections <= {coll.name for coll in policy.collections_for_resource(resource)}


def test_collection_registry_shapes():
    prefs = policy.collection("user_preferences")
    assert prefs.record_id({"user_type": "admin", "user_id": "x", "key": "layout"}) == "admin|x|layout"
    assert prefs.key_values("admin|x|layout") == ("admin", "x", "layout")
    assert prefs.key_values("admin|x|a|b") == ("admin", "x", "a|b")
    assert "updated_at" not in prefs.hash_fields and "value" in prefs.hash_fields
    assert policy.collection("system_settings").shared_row({"key": "tts_model"})
    assert not policy.collection("system_settings").shared_row({"key": "cluster_secret"})
    names = [c.name for c in policy.ordered_collections()]
    assert names[0] == "membership" and names.index("agents") < names.index("templates")
    assert "sessions" not in policy.SCANNED_COLLECTIONS and "user_preferences" in policy.SCANNED_COLLECTIONS
    with pytest.raises(KeyError):
        policy.collection("nope")
    assert any(row["collection"] == "model_providers" and "auth_token" in row["encrypted"] for row in policy.inventory())


def test_every_collection_names_real_columns(mock_db):
    """The registry must match the schema the shim built from schema_postgres."""
    cursor = mock_db.cursor()
    for coll in policy.ordered_collections():
        cursor.execute(f"PRAGMA table_info({coll.table})")
        columns = {row[1] for row in cursor.fetchall()}
        assert columns, coll.table
        missing = [f for f in coll.fields + coll.local_fields + coll.keys if f not in columns]
        assert not missing, f"{coll.name}: {missing}"
        assert set(coll.keys) <= set(coll.fields)
