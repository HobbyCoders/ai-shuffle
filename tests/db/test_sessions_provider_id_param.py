"""``create_session(provider_id=...)`` — the explicit per-session provider binding
(multi-provider phase 3, chunk P3-A).

``sessions.provider_id`` already existed (v66) but had NO setter: it was computed
at INSERT from the profile's ``config.provider`` and nothing could override it, so
a ProviderAgent worker could not be bound to the provider its dispatch named.
The new keyword parameter closes that, under two rules this module pins down:

* supplied → it WINS over the profile-derived value, and
* omitted (or empty) → behaviour is byte-identical to before, i.e. the profile
  still decides and an unresolvable/native profile still yields NULL.

NO ``SCHEMA_VERSION`` bump is involved — the column is pre-existing.
"""

import json
import uuid

import pytest

from app.db import database


def _make_profile(profile_id: str, config: dict) -> None:
    """Insert a profile row directly (config is a JSON column)."""
    from app.db.base import get_db, get_cursor

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO profiles (id, name, config) VALUES (%s, %s, %s)",
            (profile_id, profile_id, json.dumps(config)),
        )


def _new_session(profile_id: str = "default", **kwargs) -> str:
    session_id = str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id=profile_id, **kwargs)
    return session_id


# ---------------------------------------------------------------------------
# Omitted → unchanged behaviour
# ---------------------------------------------------------------------------

def test_omitted_native_profile_stays_null(mock_db):
    """A profile with no ``config.provider`` still binds NULL (= native Claude)."""
    _make_profile("p-native", {"model": "sonnet"})

    row = database.get_session(_new_session("p-native"))

    assert row is not None
    assert row["provider_id"] is None


def test_omitted_profile_provider_is_still_honored(mock_db):
    """The pre-existing profile-derived binding is untouched by the new param."""
    _make_profile("p-bound", {"provider": "prov-from-profile"})

    row = database.get_session(_new_session("p-bound"))

    assert row["provider_id"] == "prov-from-profile"


def test_omitted_unresolvable_profile_stays_null(mock_db, monkeypatch):
    """A profile lookup that BLOWS UP still yields NULL rather than raising —
    the binding is best-effort and NULL means native Claude."""
    _make_profile("p-boom", {"provider": "prov-from-profile"})

    from app.db import profiles as profiles_mod

    def _boom(_profile_id):
        raise RuntimeError("profile store unavailable")

    monkeypatch.setattr(profiles_mod, "get_profile", _boom)

    row = database.get_session(_new_session("p-boom"))

    assert row["provider_id"] is None


# ---------------------------------------------------------------------------
# Supplied → wins
# ---------------------------------------------------------------------------

def test_supplied_provider_id_is_persisted(mock_db):
    """An explicit binding lands on the row even when the profile is native."""
    _make_profile("p-native-2", {"model": "sonnet"})

    row = database.get_session(
        _new_session("p-native-2", provider_id="prov-codex")
    )

    assert row["provider_id"] == "prov-codex"


def test_supplied_provider_id_beats_profile(mock_db):
    """Explicit WINS over the profile-derived value — the whole point of the
    parameter: a worker session is bound to the provider the ProviderAgent call
    named, not to whatever its inherited profile configures."""
    _make_profile("p-bound-2", {"provider": "prov-from-profile"})

    row = database.get_session(
        _new_session("p-bound-2", provider_id="prov-explicit")
    )

    assert row["provider_id"] == "prov-explicit"


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_provider_id_falls_back_to_profile(mock_db, empty):
    """An empty/blank explicit value is treated as "not supplied" so a caller
    threading an optional through cannot accidentally blank a real binding."""
    _make_profile(f"p-empty-{abs(hash(str(empty)))}", {"provider": "prov-from-profile"})
    pid = f"p-empty-{abs(hash(str(empty)))}"

    row = database.get_session(_new_session(pid, provider_id=empty))

    assert row["provider_id"] == "prov-from-profile"


def test_provider_id_is_keyword_only(mock_db):
    """It sits in the keyword-only block, so no positional call can shift into
    it by accident."""
    import inspect

    from app.db import sessions as sessions_mod

    sig = inspect.signature(sessions_mod.create_session)
    param = sig.parameters["provider_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None
