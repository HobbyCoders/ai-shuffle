"""Tests for the model-provider registry DB helpers (app/db/model_providers.py).

Covers CRUD round-trip, ``include_token`` gating (public reads never carry the
``auth_token`` key — matching the claude_accounts ``include_credentials``
precedent), the TEXT-encoded JSON columns (``models_json`` / ``extra_env_json``)
round-tripping through the pg_shim, the enabled filter, and that a partial
update leaves the stored token intact. Runs on the in-memory SQLite shim via the
autouse DB-routing fixture (documented by depending on ``mock_db``).
"""

from app.db import database as db


def _create(provider_id="prov-1", **overrides):
    kwargs = dict(
        provider_id=provider_id,
        name="OpenRouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token="enc:tok-blob",
        models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        default_model="openai/gpt-5.6",
        small_fast_model="openai/gpt-5.6-mini",
        extra_env={"FOO": "bar"},
        context_window=200000,
        max_output_tokens=8192,
        enabled=True,
    )
    kwargs.update(overrides)
    return db.create_model_provider(**kwargs)


class TestCreateAndGet:
    def test_create_returns_public_row_without_token(self, mock_db):
        row = _create()
        assert row["id"] == "prov-1"
        assert row["name"] == "OpenRouter"
        assert row["provider_type"] == "openrouter"
        assert row["base_url"] == "https://openrouter.ai/api"
        # Token is NEVER exposed on a public read.
        assert "auth_token" not in row
        # Scalars round-trip.
        assert row["default_model"] == "openai/gpt-5.6"
        assert row["small_fast_model"] == "openai/gpt-5.6-mini"
        assert row["context_window"] == 200000
        assert row["max_output_tokens"] == 8192
        assert row["enabled"]

    def test_get_public_read_has_no_token_key(self, mock_db):
        _create()
        row = db.get_model_provider("prov-1")
        assert row is not None
        assert "auth_token" not in row

    def test_get_include_token_returns_encrypted_blob(self, mock_db):
        _create()
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:tok-blob"

    def test_get_missing_returns_none(self, mock_db):
        assert db.get_model_provider("nope") is None

    def test_json_columns_round_trip(self, mock_db):
        _create()
        row = db.get_model_provider("prov-1")
        # Parsed back to real Python structures, not JSON strings.
        assert isinstance(row["models_json"], list)
        assert row["models_json"] == [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}]
        assert isinstance(row["extra_env_json"], dict)
        assert row["extra_env_json"] == {"FOO": "bar"}

    def test_defaults_for_optional_json_and_nullable_columns(self, mock_db):
        db.create_model_provider(
            provider_id="prov-min",
            name="Custom",
            provider_type="custom",
            base_url="https://gw.example/api",
        )
        row = db.get_model_provider("prov-min", include_token=True)
        assert row["models_json"] == []
        assert row["extra_env_json"] == {}
        assert row["auth_token"] is None
        assert row["default_model"] is None
        assert row["small_fast_model"] is None
        assert row["context_window"] is None
        assert row["max_output_tokens"] is None
        assert row["enabled"]


class TestListAndEnabledFilter:
    def test_get_all_lists_every_provider_without_token(self, mock_db):
        _create(provider_id="p1")
        _create(provider_id="p2", enabled=False)
        rows = db.get_all_model_providers()
        ids = {r["id"] for r in rows}
        assert ids == {"p1", "p2"}
        assert all("auth_token" not in r for r in rows)

    def test_get_all_include_token(self, mock_db):
        _create(provider_id="p1")
        rows = db.get_all_model_providers(include_token=True)
        assert rows[0]["auth_token"] == "enc:tok-blob"

    def test_get_enabled_filters_disabled(self, mock_db):
        _create(provider_id="on")
        _create(provider_id="off", enabled=False)
        rows = db.get_enabled_model_providers()
        ids = {r["id"] for r in rows}
        assert ids == {"on"}
        assert all("auth_token" not in r for r in rows)

    def test_get_enabled_include_token(self, mock_db):
        _create(provider_id="on")
        rows = db.get_enabled_model_providers(include_token=True)
        assert rows[0]["auth_token"] == "enc:tok-blob"


class TestUpdate:
    def test_partial_update_leaves_token_intact(self, mock_db):
        _create()
        # Metadata-only update: auth_token NOT passed.
        updated = db.update_model_provider("prov-1", name="Renamed", base_url="https://new/api")
        assert updated["name"] == "Renamed"
        assert updated["base_url"] == "https://new/api"
        # The stored token must be untouched.
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:tok-blob"

    def test_update_json_columns_accept_python_structures(self, mock_db):
        _create()
        db.update_model_provider(
            "prov-1",
            models_json=[{"id": "x/y", "label": "XY"}],
            extra_env_json={"K": "V"},
        )
        row = db.get_model_provider("prov-1")
        assert row["models_json"] == [{"id": "x/y", "label": "XY"}]
        assert row["extra_env_json"] == {"K": "V"}

    def test_update_can_replace_token_when_explicitly_passed(self, mock_db):
        _create()
        db.update_model_provider("prov-1", auth_token="enc:new-blob")
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:new-blob"

    def test_update_no_fields_is_noop_returns_row(self, mock_db):
        _create()
        row = db.update_model_provider("prov-1")
        assert row["id"] == "prov-1"
        assert db.get_model_provider("prov-1", include_token=True)["auth_token"] == "enc:tok-blob"

    def test_update_missing_returns_none(self, mock_db):
        assert db.update_model_provider("nope", name="x") is None


class TestSetEnabledAndDelete:
    def test_set_enabled_toggles_flag(self, mock_db):
        _create()
        row = db.set_model_provider_enabled("prov-1", False)
        assert not row["enabled"]
        assert db.get_enabled_model_providers() == []
        row = db.set_model_provider_enabled("prov-1", True)
        assert row["enabled"]
        assert {r["id"] for r in db.get_enabled_model_providers()} == {"prov-1"}

    def test_set_enabled_missing_returns_none(self, mock_db):
        assert db.set_model_provider_enabled("nope", True) is None

    def test_delete_removes_row(self, mock_db):
        _create()
        assert db.delete_model_provider("prov-1") is True
        assert db.get_model_provider("prov-1") is None
        assert db.get_all_model_providers() == []

    def test_delete_missing_returns_false(self, mock_db):
        assert db.delete_model_provider("nope") is False


class TestCodexTokenBundle:
    """Phase-2 Codex OAuth bundle: the two token blobs are write-only (never on a
    public read, only under include_token), the bundle persists atomically, and
    the non-secret Codex fields are public."""

    def test_public_read_never_returns_codex_blobs(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1",
            access_token="enc:codex-access",
            refresh_token="enc:codex-refresh",
            account_id="acct-123",
        )
        row = db.get_model_provider("prov-1")  # public read
        assert "codex_access_token" not in row
        assert "codex_refresh_token" not in row
        # Non-secret Codex fields ARE public.
        assert row["auth_mode"] == "chatgpt"
        assert row["codex_account_id"] == "acct-123"
        assert row["needs_reauth"] in (False, 0)

    def test_include_token_returns_codex_blobs(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1",
            access_token="enc:codex-access",
            refresh_token="enc:codex-refresh",
            account_id="acct-123",
        )
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["codex_access_token"] == "enc:codex-access"
        assert row["codex_refresh_token"] == "enc:codex-refresh"
        # The phase-1 gateway token blob is still there too.
        assert row["auth_token"] == "enc:tok-blob"

    def test_set_bundle_is_atomic_and_reader_round_trips(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1",
            access_token="enc:acc-1",
            refresh_token="enc:ref-1",
            account_id="acct-1",
            id_token_expires_at="2026-07-18T12:00:00+00:00",
            last_refresh="2026-07-18T11:00:00+00:00",
        )
        bundle = db.get_codex_tokens("prov-1")
        assert bundle["auth_mode"] == "chatgpt"
        assert bundle["codex_access_token"] == "enc:acc-1"
        assert bundle["codex_refresh_token"] == "enc:ref-1"
        assert bundle["codex_account_id"] == "acct-1"
        assert bundle["codex_id_token_expires_at"] == "2026-07-18T12:00:00+00:00"
        assert bundle["codex_last_refresh"] == "2026-07-18T11:00:00+00:00"
        assert bundle["needs_reauth"] in (False, 0)

    def test_set_bundle_replaces_rotated_tokens_together(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1", access_token="enc:acc-1", refresh_token="enc:ref-1",
            account_id="acct-1",
        )
        # A refresh rotates BOTH tokens; the new pair must land together.
        db.set_codex_token_bundle(
            "prov-1", access_token="enc:acc-2", refresh_token="enc:ref-2",
            account_id="acct-1",
        )
        bundle = db.get_codex_tokens("prov-1")
        assert bundle["codex_access_token"] == "enc:acc-2"
        assert bundle["codex_refresh_token"] == "enc:ref-2"

    def test_set_bundle_auth_mode_grok(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1", access_token="enc:a", refresh_token="enc:r",
            account_id="team-1", auth_mode="grok",
        )
        bundle = db.get_codex_tokens("prov-1")
        assert bundle["auth_mode"] == "grok"
        row = db.get_model_provider("prov-1")
        assert row["auth_mode"] == "grok"

    def test_set_bundle_can_flag_needs_reauth(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1", access_token="enc:a", refresh_token="enc:r",
            account_id="acct-1", needs_reauth=True,
        )
        assert db.get_codex_tokens("prov-1")["needs_reauth"] in (True, 1)

    def test_set_bundle_leaves_gateway_auth_token_intact(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1", access_token="enc:a", refresh_token="enc:r", account_id="x",
        )
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:tok-blob"

    def test_set_bundle_missing_provider_returns_none(self, mock_db):
        assert db.set_codex_token_bundle(
            "nope", access_token="enc:a", refresh_token="enc:r", account_id="x",
        ) is None

    def test_get_codex_tokens_missing_provider_returns_none(self, mock_db):
        assert db.get_codex_tokens("nope") is None

    def test_provider_without_codex_bundle_has_null_fields(self, mock_db):
        _create()
        bundle = db.get_codex_tokens("prov-1")
        assert bundle["codex_access_token"] is None
        assert bundle["codex_refresh_token"] is None
        assert bundle["codex_account_id"] is None
        # auth_mode is NULL until a Codex bundle is stored (NULL == legacy apikey).
        assert bundle["auth_mode"] is None


def _lease(conn, provider_id="prov-1"):
    """(owner, expires_at) straight off the row — there is no public reader."""
    row = conn.execute(
        "SELECT codex_refresh_lease_owner, codex_refresh_lease_expires_at "
        "FROM model_providers WHERE id = ?",
        (provider_id,),
    ).fetchone()
    return row["codex_refresh_lease_owner"], row["codex_refresh_lease_expires_at"]


def _expire_lease(conn, provider_id="prov-1"):
    """Backdate the lease so it reads as expired on the DB clock."""
    conn.execute(
        "UPDATE model_providers SET codex_refresh_lease_expires_at = ? WHERE id = ?",
        ("2000-01-01 00:00:00", provider_id),
    )
    conn.commit()


class TestCodexRefreshClaim:
    """The durable cross-install refresh lease. The Codex refresh token rotates,
    so two installs sharing this database must not refresh the same row at once;
    the claim is a single conditional UPDATE evaluated on the DATABASE clock."""

    def test_first_claimer_wins(self, mock_db):
        _create()
        assert db.try_claim_codex_refresh("prov-1", "install-a", 120) is True
        owner, expires = _lease(mock_db)
        assert owner == "install-a"
        assert expires is not None

    def test_second_claimer_loses_while_lease_is_live(self, mock_db):
        _create()
        assert db.try_claim_codex_refresh("prov-1", "install-a", 300) is True
        assert db.try_claim_codex_refresh("prov-1", "install-b", 300) is False
        # The holder is untouched.
        assert _lease(mock_db)[0] == "install-a"

    def test_expired_lease_is_reclaimable_by_a_peer(self, mock_db):
        _create()
        assert db.try_claim_codex_refresh("prov-1", "install-a", 300) is True
        _expire_lease(mock_db)
        # TTL expiry is the crash backstop: nobody has to release the row.
        assert db.try_claim_codex_refresh("prov-1", "install-b", 300) is True
        assert _lease(mock_db)[0] == "install-b"

    def test_same_owner_reclaim_succeeds(self, mock_db):
        _create()
        assert db.try_claim_codex_refresh("prov-1", "install-a", 300) is True
        # Deliberate: after a crash-restart the same instance may re-claim inside
        # the TTL (its in-process lock still serializes locally).
        assert db.try_claim_codex_refresh("prov-1", "install-a", 300) is True
        assert _lease(mock_db)[0] == "install-a"

    def test_claim_on_missing_provider_returns_false(self, mock_db):
        assert db.try_claim_codex_refresh("nope", "install-a", 120) is False

    def test_release_clears_own_lease(self, mock_db):
        _create()
        db.try_claim_codex_refresh("prov-1", "install-a", 300)
        db.release_codex_refresh_claim("prov-1", "install-a")
        assert _lease(mock_db) == (None, None)
        # Row is immediately claimable again.
        assert db.try_claim_codex_refresh("prov-1", "install-b", 300) is True

    def test_release_with_wrong_owner_leaves_peer_lease_intact(self, mock_db):
        _create()
        db.try_claim_codex_refresh("prov-1", "install-a", 300)
        # A late release from an install whose TTL already lapsed must NOT strip
        # the current holder's lease.
        db.release_codex_refresh_claim("prov-1", "install-b")
        owner, expires = _lease(mock_db)
        assert owner == "install-a"
        assert expires is not None
        assert db.try_claim_codex_refresh("prov-1", "install-b", 300) is False

    def test_claim_does_not_disturb_the_token_bundle(self, mock_db):
        _create()
        db.set_codex_token_bundle(
            "prov-1", access_token="enc:a", refresh_token="enc:r",
            account_id="acct-1", last_refresh="2026-08-16T10:00:00+00:00",
        )
        db.try_claim_codex_refresh("prov-1", "install-a", 300)
        db.release_codex_refresh_claim("prov-1", "install-a")
        bundle = db.get_codex_tokens("prov-1")
        assert bundle["codex_access_token"] == "enc:a"
        assert bundle["codex_refresh_token"] == "enc:r"
        assert bundle["codex_last_refresh"] == "2026-08-16T10:00:00+00:00"
