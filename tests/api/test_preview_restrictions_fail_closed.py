"""An unreadable preview grant never lifts the limits a valid one imposes.

One request consults the bound session several times: at the preview listener,
in ``enforce_limits``, when admitting the grant and again in a route's own
"previews may not do this" guard. Those guards deny on a grant being present,
so a lookup error read as "no grant" would switch the restriction off while the
request stays authenticated (by the earlier checks, or by another credential).

These tests drive the real listener, the real auth dependencies and the real
guards. Only the session lookup is made to fail, by call ordinal, because that
is the fault under test: the row, the identity and the preference write are the
real ones. An owner with no preview cookie, or a cookie whose session is really
gone, must keep working throughout.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.api import (
    auth,
    chat_access,
    generation_choices,
    plugin_oauth,
    preferences,
    remote_workspace_files,
    sessions_create,
    settings_updates,
    settings_workers,
    support_reports,
    terminals,
    tools,
    voice,
)
from app.core import owner_auth, preview_access
from app.core.config import settings
from app.db import database, sessions
from app.serve import IngressApplication

SESSION = "sess-preview-fail-closed"
LOCAL, PREVIEW = 8000, 8002
HOST = "10.0.0.30"
PEER = ("10.0.0.20", 54321)
KEY = "composer.modelFast.test-model"
PATH = f"/api/v1/preferences/{KEY}"
BODY = {"key": KEY, "value": True}
ALWAYS = "always"
OWNER_COOKIE = "owner-session-cookie"


class Lookup:
    """The real session lookup, failing on chosen calls of one request."""

    def __init__(self, real):
        self.real, self.calls, self.fail = real, 0, ()

    def __call__(self, session_id):
        self.calls += 1
        if self.fail == ALWAYS or self.calls in self.fail:
            raise OSError(9, "Bad file descriptor")
        return self.real(session_id)

    def arm(self, fail):
        self.calls, self.fail = 0, fail


@pytest.fixture
def lookup(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    monkeypatch.setattr(
        owner_auth,
        "owner_session",
        lambda request, token: token == OWNER_COOKIE,
    )
    sessions.create_session(SESSION, "default")
    assert sessions.get_session(SESSION) is not None
    wrapped = Lookup(sessions.get_session)
    monkeypatch.setattr(sessions, "get_session", wrapped)
    return wrapped


def _client(app, port, *, preview=True, session=None):
    origin = f"http://{HOST}:{port}"
    client = TestClient(
        IngressApplication(app, LOCAL, None, PREVIEW),
        base_url=origin,
        client=PEER,
        headers={"Origin": origin},
    )
    if preview:
        client.cookies.set(preview_access.cookie_name(), preview_access.mint(SESSION))
    if session:
        client.cookies.set("session", session)
    return client


def _saved():
    from app.core.preference_identity import principal_preference_identity

    return database.get_user_preference(*principal_preference_identity({"kind": "admin"}), KEY)


@pytest.fixture
def prefs_app():
    api = FastAPI()
    api.include_router(preferences.router)
    return api


# ---------------------------------------------------------------------------
# The reported path: a preference write through the real listener and auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fail",
    [(1,), (2,), (3,), (4,), (2, 3), (3, 4), ALWAYS],
    ids=lambda fail: "always" if fail == ALWAYS else "call-" + "+".join(map(str, fail)),
)
@pytest.mark.parametrize("method", ["PUT", "POST"])
def test_preview_listener_never_writes_when_a_lookup_fails(lookup, prefs_app, method, fail):
    client = _client(prefs_app, PREVIEW)
    lookup.arm(fail)
    response = client.request(method, PATH, json=BODY)
    lookup.arm(())
    assert response.status_code in (401, 403, 503), response.text
    assert _saved() is None


def test_preview_listener_delete_is_refused_when_the_recheck_fails(lookup, prefs_app):
    owner = _client(prefs_app, LOCAL, preview=False, session=OWNER_COOKIE)
    assert owner.put(PATH, json=BODY).status_code == 200
    client = _client(prefs_app, PREVIEW)
    lookup.arm((4,))
    response = client.delete(PATH)
    lookup.arm(())
    assert response.status_code in (401, 403, 503), response.text
    assert _saved()["value"] is True


@pytest.mark.parametrize(
    "fail",
    [(1,), (2,), ALWAYS],
    ids=lambda fail: "always" if fail == ALWAYS else "call-" + "+".join(map(str, fail)),
)
@pytest.mark.parametrize("session", [None, OWNER_COOKIE], ids=["direct-lan", "owner-session"])
def test_local_listener_with_a_preview_cookie_never_writes(lookup, prefs_app, session, fail):
    """The cookie's limits bind even a browser that is independently the owner."""
    client = _client(prefs_app, LOCAL, session=session)
    lookup.arm(fail)
    response = client.put(PATH, json=BODY)
    lookup.arm(())
    assert response.status_code in (403, 503), response.text
    assert _saved() is None


def test_unavailable_check_is_retryable_not_a_sign_out(lookup, prefs_app):
    """The first lookup of a request failing is reported as "try again"."""
    client = _client(prefs_app, LOCAL)
    lookup.arm(ALWAYS)
    response = client.put(PATH, json=BODY)
    assert response.status_code == 503
    assert "could not be checked" in response.json()["detail"]


def test_next_request_is_decided_afresh(lookup, prefs_app):
    """The decision belongs to one request: a healed lookup restores reads, and
    a deleted session stops restricting on the very next request."""
    client = _client(prefs_app, LOCAL)
    lookup.arm(ALWAYS)
    assert client.get(PATH).status_code == 503
    lookup.arm(())
    assert client.get(PATH).status_code == 200
    assert client.put(PATH, json=BODY).status_code == 403
    assert lookup.calls == 2
    lookup.real = lambda session_id: None
    assert client.put(PATH, json=BODY).status_code == 200


def test_one_request_looks_the_session_up_once_past_the_listener(lookup, prefs_app):
    client = _client(prefs_app, PREVIEW)
    lookup.arm(())
    assert client.put(PATH, json=BODY).status_code == 403
    assert lookup.calls == 2  # The listener's check, then enforce_limits.


def test_recheck_survives_a_signing_key_failure(lookup, prefs_app, monkeypatch):
    """The first check decided; a later key read cannot undo that decision."""
    client = _client(prefs_app, LOCAL)
    real = preview_access.verify
    seen = []

    def verify_then_break(token, **kwargs):
        found = real(token, **kwargs)
        seen.append(found)

        def unreadable(*args, **inner):
            raise OSError("unreadable fixture key")

        monkeypatch.setattr(preview_access, "_sign", unreadable)
        monkeypatch.setattr(preview_access, "cookie_name", unreadable)
        return found

    monkeypatch.setattr(preview_access, "verify", verify_then_break)
    response = client.put(PATH, json=BODY)
    assert seen and seen[0] is not None
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Legitimate owners
# ---------------------------------------------------------------------------

def test_owner_without_a_preview_cookie_never_consults_the_lookup(lookup, prefs_app):
    for session in (None, OWNER_COOKIE):
        client = _client(prefs_app, LOCAL, preview=False, session=session)
        lookup.arm(ALWAYS)
        response = client.put(PATH, json={**BODY, "value": session or "direct"})
        assert response.status_code == 200, response.text
        assert lookup.calls == 0
    lookup.arm(())
    assert _saved()["value"] == OWNER_COOKIE


@pytest.mark.parametrize("session", [None, OWNER_COOKIE], ids=["direct-lan", "owner-session"])
def test_cookie_for_a_deleted_session_does_not_restrict_the_owner(lookup, prefs_app, session):
    client = _client(prefs_app, LOCAL, session=session)
    lookup.real = lambda session_id: None
    lookup.arm(())
    assert client.put(PATH, json=BODY).status_code == 200
    assert _saved()["value"] is True


@pytest.mark.parametrize("cookie", ["expired", "forged"])
def test_dead_cookie_does_not_consult_the_lookup_or_restrict(lookup, prefs_app, cookie):
    client = _client(prefs_app, LOCAL, preview=False, session=OWNER_COOKIE)
    token = preview_access.mint(SESSION)
    if cookie == "forged":
        token = token[:-1] + ("1" if token[-1] == "0" else "0")
    else:
        token = preview_access.mint(SESSION, ttl_seconds=60, now=0)
    client.cookies.set(preview_access.cookie_name(), token)
    lookup.arm(ALWAYS)
    assert client.put(PATH, json=BODY).status_code == 200
    assert lookup.calls == 0


# ---------------------------------------------------------------------------
# Every other guard that denies on the grant being present
# ---------------------------------------------------------------------------

async def _update_owner(request):
    settings_updates.require_update_owner(request, "token")


async def _worker_action(request):
    await settings_workers.act_on_worker(SimpleNamespace(), request, None)


async def _reporter(request):
    support_reports._reporter(request)


async def _owner_action(request):
    support_reports._owner_action(request)


async def _send_report(request):
    await support_reports.send_report("report", SimpleNamespace(confirm=False), request, "owner")


async def _capabilities(request):
    if not (await support_reports.get_capabilities(request))["can_report_ai_content"]:
        raise preview_access_refused()


async def _choices_migration(request):
    await generation_choices.list_choices(request, "token")
    if not generation_choices.generation_choices.load.ran_migration:
        raise preview_access_refused()


async def _session_elsewhere(request):
    await sessions_create._resolve_target("another-worker", None, {}, request)


async def _voice_elsewhere(request):
    voice._refuse_preview_elsewhere(request, {"id": "session"})


async def _terminal(request):
    terminals.require_terminal_owner(request)


async def _private_inputs(request):
    await chat_access.checked(request, "session")


async def _plugin_sign_in(request):
    plugin_oauth._private_transport(request)


async def _remote_files(request):
    remote_workspace_files.require_remote_files_owner(request, "another-worker")


async def _peer_message(request):
    body = SimpleNamespace(model_dump=lambda **kwargs: {})
    await tools.send_peer_message("sender", body, request, "token")


def preview_access_refused():
    from fastapi import HTTPException

    return HTTPException(403, "restricted")


GUARDS = {
    "updates": (auth.require_admin, _update_owner),
    "worker-action": (auth.require_admin, _worker_action),
    "report-draft": (auth.require_auth, _reporter),
    "report-diagnosis": (auth.require_admin, _owner_action),
    "report-send": (auth.require_auth, _send_report),
    "report-capability": (auth.require_auth, _capabilities),
    "choices-migration": (auth.require_auth, _choices_migration),
    "session-elsewhere": (auth.require_auth, _session_elsewhere),
    "voice-elsewhere": (auth.require_auth, _voice_elsewhere),
    "terminal": (None, _terminal),
    "private-inputs": (auth.require_admin, _private_inputs),
    "plugin-sign-in": (auth.require_admin, _plugin_sign_in),
    "remote-files": (None, _remote_files),
    "peer-message": (auth.require_admin, _peer_message),
}


@pytest.fixture
def guard_app(lookup, monkeypatch):
    """Each guard at a neutral path, so only the guard itself can refuse it."""
    from app.core import voice_remote

    class Passed(Exception):
        """Raised by the first thing past a guard: it let the request through."""

    def passed(*args, **kwargs):
        raise Passed

    async def passed_async(*args, **kwargs):
        raise Passed

    monkeypatch.setattr(settings_workers.worker_settings, "dispatch", passed_async)
    monkeypatch.setattr(support_reports, "_writable_draft", passed)
    monkeypatch.setattr(support_reports.reports, "capabilities", lambda: {})
    monkeypatch.setattr(support_reports, "request_preference_identity", lambda request: ("admin", "owner"))
    monkeypatch.setattr(database, "get_install", passed)
    monkeypatch.setattr(voice_remote, "owner_of", lambda row: True)
    monkeypatch.setattr(owner_auth, "loopback_request", lambda request: True)
    monkeypatch.setattr(chat_access, "require_session", passed_async)
    monkeypatch.setattr(remote_workspace_files.engine, "context", passed)
    monkeypatch.setattr("app.api.sessions_helpers.require_transcript_session", passed_async)
    monkeypatch.setattr(generation_choices, "_identity", lambda request: ("admin", "owner"))
    monkeypatch.setattr(generation_choices, "_here", lambda: "worker")

    def load(identity, *, run_migration=True):
        load.ran_migration = run_migration
        return {}

    load.ran_migration = None
    monkeypatch.setattr(generation_choices.generation_choices, "load", load)

    api = FastAPI()

    def add(name, dependency, guard):
        async def probe(request: Request, _: str = Depends(dependency or (lambda: "none"))):
            try:
                await guard(request)
            except Passed:
                pass
            return {"passed": name}

        api.post(f"/api/v1/probe/{name}")(probe)

    for name, (dependency, guard) in GUARDS.items():
        add(name, dependency, guard)
    return api


@pytest.mark.parametrize("name", GUARDS)
@pytest.mark.parametrize(
    "fail",
    [(1,), (2,), ALWAYS],
    ids=lambda fail: "always" if fail == ALWAYS else "call-" + "+".join(map(str, fail)),
)
def test_guard_holds_when_a_lookup_fails(lookup, guard_app, name, fail):
    client = _client(guard_app, LOCAL, session=OWNER_COOKIE)
    lookup.arm(fail)
    response = client.post(f"/api/v1/probe/{name}")
    assert response.status_code in (403, 503), f"{name}: {response.status_code} {response.text}"


@pytest.mark.parametrize("name", GUARDS)
def test_guard_refuses_a_healthy_preview_cookie(lookup, guard_app, name):
    client = _client(guard_app, LOCAL, session=OWNER_COOKIE)
    lookup.arm(())
    response = client.post(f"/api/v1/probe/{name}")
    assert response.status_code == 403, f"{name}: {response.status_code} {response.text}"


@pytest.mark.parametrize("name", GUARDS)
def test_guard_passes_the_owner_while_lookups_are_failing(lookup, guard_app, name):
    client = _client(guard_app, LOCAL, preview=False, session=OWNER_COOKIE)
    lookup.arm(ALWAYS)
    response = client.post(f"/api/v1/probe/{name}")
    assert response.status_code == 200, f"{name}: {response.status_code} {response.text}"
    assert lookup.calls == 0


@pytest.mark.parametrize("session", [None, OWNER_COOKIE], ids=["no-login", "owner-session"])
def test_mutation_ceiling_holds_when_the_lookup_fails(lookup, session):
    """The ceiling itself, not only the routes' own guards."""
    api = FastAPI()

    @api.post("/api/v1/settings/test-only")
    def change_settings(_: str = Depends(auth.require_auth)):
        return {"changed": True}

    client = _client(api, LOCAL, session=session)
    lookup.arm(ALWAYS)
    assert client.post("/api/v1/settings/test-only").status_code == 503


# ---------------------------------------------------------------------------
# Admission stays closed; the callers that read verify directly stay safe
# ---------------------------------------------------------------------------

def test_token_redirect_reports_an_unavailable_check(lookup):
    api = FastAPI()
    api.include_router(auth.router)
    client = TestClient(
        IngressApplication(api, LOCAL, None, PREVIEW),
        base_url=f"http://{HOST}:{PREVIEW}",
        client=PEER,
        follow_redirects=False,
    )
    lookup.arm(ALWAYS)
    response = client.get(
        preview_access.REDIRECT_PATH, params={"token": preview_access.mint(SESSION)}
    )
    assert response.status_code == 503
    assert preview_access.cookie_name() not in response.cookies
    lookup.arm(())
    assert client.get(
        preview_access.REDIRECT_PATH, params={"token": preview_access.mint(SESSION)}
    ).status_code == 303


def test_preview_listener_refuses_when_it_cannot_check(lookup, prefs_app):
    client = _client(prefs_app, PREVIEW)
    lookup.arm(ALWAYS)
    assert client.get(PATH).status_code == 403


def test_saved_preview_link_is_not_reused_when_it_cannot_check(lookup, tmp_path, monkeypatch):
    import json

    from app import preview_run

    saved = tmp_path / "saved-preview.json"
    saved.write_text(json.dumps({"token": preview_access.mint(SESSION)}), encoding="utf-8")
    monkeypatch.setenv("SHUFFLE_DEV_ACCESS_FILE", str(saved))
    monkeypatch.setattr(preview_run, "disposable_preview_data", lambda: True)
    lookup.arm(ALWAYS)
    assert preview_run._saved_access() is None
    lookup.arm(())
    assert preview_run._saved_access() is not None
