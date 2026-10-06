"""Authorize artifact bytes through the session that produced or references them."""
import json

from fastapi import HTTPException

from app.core.config import settings
from app.db import sessions, mobile_tool_identity, mobile_hub
from app.db.base import get_db


def _network_mirror(session):
    """True when another worker of this install's network runs the session."""
    from app.network import control
    try:
        return control.owner_of(session) is not None
    except Exception:  # noqa: BLE001 - an unreadable network context fails closed
        return False


def session_provenance(session, session_id=None):
    """The account record that decides who may read ``session``, or None.

    A chat another worker runs has no local record here; its session row
    carries the owner from that worker (#1849), so the account rules apply to
    a mirror exactly as they do where the chat runs. A worker the chat moved
    away from keeps its old record, so there the running worker's label comes
    first: it may have changed since (a claim there).
    """
    carried = mobile_tool_identity.mirrored(session)
    if carried is not None and _network_mirror(session):
        return carried
    return mobile_tool_identity.read(session_id or session.get('id')) or carried


def _paired_client_allowed(client, session, provenance):
    """Whether a paired controller may read this chat (its images and transcript)."""
    owner = mobile_hub.ownership(paired_client_id=client['id'])
    expected = 'account:' + owner['account_id'] if owner else 'paired:' + client['id']
    allowed = bool(provenance and provenance['owner'] == expected)
    if provenance and not allowed:
        from app.core.mobile_tool_identity import same_install_owner
        allowed = same_install_owner(provenance, {
            'kind': 'paired', 'principal_id': client['id'],
            'account_id': owner['account_id'] if owner else None,
        }, session)
    if not provenance:
        # The owner's own device has this install's admin authority. A chat
        # run by another worker of this network is a mirror here: its
        # instance_id is local-only (unset) and its provenance stays on the
        # worker that runs it, so accept that worker's mirror as well.
        allowed = bool(client.get('account_owner_admin') is True
                       and (session.get('instance_id') == settings.effective_instance_id
                            or _network_mirror(session))
                       and not session.get('api_user_id'))
    return allowed


def paired_client_may_read(client, session):
    """The account rule tool images use, for a chat's transcript too (#1877).

    A paired controller (an account phone or a directly paired device) gets
    the same answer for a chat's transcript as for its images, on the worker
    that runs the chat and on a worker that mirrors it. Blocking: call it off
    the event loop.
    """
    return _paired_client_allowed(client, session, session_provenance(session))


_CHAT_PARENT_DEPTH = 8


def _chat_provenance(session):
    """:func:`session_provenance`, inherited from the parent chat by a subagent.

    A worker (``is_worker``) a chat spawns has no account record of its own;
    its transcript belongs to whoever owns the chat that spawned it.
    """
    for _ in range(_CHAT_PARENT_DEPTH):
        if not session:
            return None
        provenance = session_provenance(session)
        if provenance or not session.get('is_worker') or not session.get('parent_session_id'):
            return provenance
        session = sessions.get_session(session['parent_session_id'])
    return None


def _reader_owner_authority(provenance):
    """Whether a paired principal holds this install's owner authority. Fails closed."""
    from app.core.mobile_tool_identity import _owner_authority
    try:
        if provenance.get('principal_id'):
            return bool(_owner_authority(provenance))
        if provenance.get('account_id'):
            # A synced label: the device is paired with another worker.
            from app.core.preference_identity import local_owner_account
            return provenance['account_id'] == local_owner_account()
    except Exception:  # noqa: BLE001 - an unreadable account fails closed
        return False
    return False


def chat_reader(session_id):
    """The paired principal a chat's own recall tools read as (#1897), or None.

    ``session_id`` is the calling chat (its app id or its transcript-store id).
    A chat a paired controller started (an account member's phone or a direct
    controller) reads past chats only as that device may over REST
    (:func:`paired_client_may_read`). None means no device rule applies: the
    owner's chats and legacy chats read as before, and an API key's chat is
    scoped by :mod:`app.core.api_user_scope`. Blocking.
    """
    if not session_id:
        return None
    row = sessions.get_session(session_id) or sessions.get_session_by_sdk_id(session_id)
    provenance = _chat_provenance(row)
    if not provenance or provenance.get('kind') != 'paired':
        return None
    return dict(provenance, owner_authority=_reader_owner_authority(provenance))


def chat_reader_may_read(reader, session):
    """:func:`paired_client_may_read` for a :func:`chat_reader` principal.

    The same answer the device gets for ``session``'s transcript over REST:
    its own account's chats; the owner's chats only while the device holds
    the install owner's authority. Blocking.
    """
    if not session:
        return False
    if reader is None:
        return True
    provenance = _chat_provenance(session)
    if provenance:
        if provenance['owner'] == reader['owner']:
            return True
        # ``mobile_tool_identity.same_install_owner``: an owner-labelled chat
        # for a device that holds the owner's authority.
        from app.core.mobile_tool_identity import _network_mirror as _running_mirror
        return bool(provenance['kind'] == 'admin' and reader['owner_authority']
                    and not session.get('api_user_id')
                    and (session.get('instance_id') == settings.effective_instance_id
                         or _running_mirror(session)))
    return bool(reader['owner_authority']
                and (session.get('instance_id') == settings.effective_instance_id
                     or _network_mirror(session))
                and not session.get('api_user_id'))


def readable_store_ids(store_ids, reader):
    """The transcript-store ids whose app chat ``reader`` may read.

    A store id with no app session row is nobody's account's, so a paired
    reader never sees it. Blocking.
    """
    ids = sorted({sid for sid in store_ids if sid})
    if not ids:
        return set()
    if reader is None:
        return set(ids)
    readable = set()
    for sid in ids:
        if chat_reader_may_read(reader, sessions.get_session_by_sdk_id(sid)):
            readable.add(sid)
    return readable


def authorize_session_artifact(request, row, digest, session_id=None):
    owner_session = (row or {}).get('created_by_session')
    target = session_id or owner_session
    if not target:
        # Historical non-session artifacts retain the authenticated download contract.
        return
    session = sessions.get_session(target)
    if not session:
        raise HTTPException(404, 'Not found')
    if target != owner_session:
        # A shared digest can legitimately appear in several users' sessions.
        # Possession of an unrelated accessible session ID is not a grant.
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM session_events WHERE session_id=%s "
                           "AND ((event_type='artifact.created' AND payload ->> 'hash'=%s) "
                           "OR (event_type='tool.use' AND payload ->> 'screenshot'=%s)) LIMIT 1",
                           (target, digest, '/api/v1/artifacts/' + digest))
            referenced = cursor.fetchone() is not None
            if not referenced:
                # Child workers can publish their image hashes into a parent's
                # durable progress without duplicating artifact.created there.
                # Only inspect matching candidates, with bounded memory/work;
                # a hash mentioned in ordinary output is never a reference.
                cursor.execute("SELECT payload ->> 'payload' AS progress_payload FROM session_events "
                               "WHERE session_id=%s AND event_type IN ('subagent.progress','team.member_progress') "
                               "AND payload ->> 'kind'='tool_result' AND CAST(payload AS TEXT) LIKE %s "
                               "ORDER BY seq DESC LIMIT 64", (target, '%"' + digest + '"%'))
                for row in cursor.fetchall():
                    payload = row['progress_payload']
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except (ValueError, TypeError):
                            continue
                    hashes = payload.get('artifact_hashes') if isinstance(payload, dict) else None
                    if isinstance(hashes, list) and digest in hashes:
                        referenced = True
                        break
            if not referenced:
                raise HTTPException(404, 'Not found')
    provenance = session_provenance(session, target)
    api_user = getattr(request.state, 'api_user', None)
    client = getattr(request.state, 'paired_client', None)
    allowed = False
    if api_user:
        from app.api.sessions_helpers import (
            api_user_owns_session,
            check_session_access_for_api_user,
        )

        # Another principal's session stays a 404 here (no existence leak).
        allowed = api_user_owns_session(api_user, session)
        if allowed:
            check_session_access_for_api_user(api_user, session)
    elif client:
        allowed = _paired_client_allowed(client, session, provenance)
    elif getattr(request.state, 'is_admin', False):
        if provenance and provenance.get('account_id'):
            from app.core.preference_identity import local_owner_account
            allowed = provenance['account_id'] == local_owner_account()
        else:
            # Admins can view sessions across workers (check_session_access).
            # instance_id is execution placement, not an access boundary, and
            # is unset on network mirrors. Keep explicit account provenance
            # above authoritative, but allow the same session's screenshots.
            allowed = True
    if not allowed:
        raise HTTPException(404, 'Not found')
