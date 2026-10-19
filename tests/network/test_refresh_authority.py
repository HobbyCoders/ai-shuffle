"""Which worker renews a synced ChatGPT/SuperGrok login (refresh_authority)."""

from types import SimpleNamespace

from app.network import refresh_authority
from app.network.membership import KIND_CONSUMER, KIND_WORKER, STATUS_ACTIVE, STATUS_REVOKED


def _member(device_id, *, kind=KIND_WORKER, status=STATUS_ACTIVE):
    return SimpleNamespace(device_id=device_id, kind=kind, status=status)


def _ctx(me, founder, *members):
    by_id = {m.device_id: m for m in members}
    return SimpleNamespace(
        device_id=me, founder_device_id=founder, members=by_id, member=by_id.get,
    )


def test_founder_renews_when_it_is_an_active_worker():
    ctx = _ctx("dev_b", "dev_b", _member("dev_a"), _member("dev_b"))
    assert refresh_authority.authority_device_id(ctx) == "dev_b"


def test_lowest_active_worker_takes_over_from_a_revoked_founder():
    ctx = _ctx("dev_c", "dev_a",
               _member("dev_a", status=STATUS_REVOKED), _member("dev_c"), _member("dev_b"),
               _member("dev_0", kind=KIND_CONSUMER))
    assert refresh_authority.authority_device_id(ctx) == "dev_b"


def test_no_active_worker_means_no_authority():
    ctx = _ctx("dev_a", "dev_a", _member("dev_a", status=STATUS_REVOKED))
    assert refresh_authority.authority_device_id(ctx) is None


def test_standalone_install_renews_its_own_logins(monkeypatch):
    monkeypatch.setattr(refresh_authority.engine, "load_context", lambda **_: None)
    assert refresh_authority.may_refresh_here() is True


def test_only_the_authority_renews_in_a_network(monkeypatch):
    members = (_member("dev_founder"), _member("dev_peer"))
    monkeypatch.setattr(refresh_authority.engine, "load_context",
                        lambda **_: _ctx("dev_founder", "dev_founder", *members))
    assert refresh_authority.may_refresh_here() is True
    monkeypatch.setattr(refresh_authority.engine, "load_context",
                        lambda **_: _ctx("dev_peer", "dev_founder", *members))
    assert refresh_authority.may_refresh_here() is False


def test_unreadable_membership_does_not_renew(monkeypatch):
    def broken(**_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(refresh_authority.engine, "load_context", broken)
    assert refresh_authority.may_refresh_here() is False


def _named(device_id, name, **kwargs):
    member = _member(device_id, **kwargs)
    member.name = name
    return member


def test_renewing_worker_has_no_pending_message(monkeypatch):
    members = (_named("dev_founder", "Unraid"), _named("dev_peer", "GamingPC"))
    monkeypatch.setattr(refresh_authority.engine, "load_context",
                        lambda **_: _ctx("dev_founder", "dev_founder", *members))
    assert refresh_authority.renewal_pending_message("ChatGPT") is None
    monkeypatch.setattr(refresh_authority.engine, "load_context", lambda **_: None)
    assert refresh_authority.renewal_pending_message("ChatGPT") is None


def test_pending_message_names_the_renewing_worker(monkeypatch):
    members = (_named("dev_founder", "Unraid"), _named("dev_peer", "GamingPC"))
    monkeypatch.setattr(refresh_authority.engine, "load_context",
                        lambda **_: _ctx("dev_peer", "dev_founder", *members))
    message = refresh_authority.renewal_pending_message("ChatGPT")
    assert "ChatGPT" in message and "'Unraid'" in message


def test_pending_message_without_any_active_worker_asks_for_sign_in(monkeypatch):
    members = (_named("dev_founder", "Unraid", status=STATUS_REVOKED),
               _named("dev_peer", "GamingPC", status=STATUS_REVOKED))
    monkeypatch.setattr(refresh_authority.engine, "load_context",
                        lambda **_: _ctx("dev_peer", "dev_founder", *members))
    assert "Sign in again" in refresh_authority.renewal_pending_message("Grok")
