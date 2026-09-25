"""Authorize artifact bytes through the session that produced or references them."""
import json

from fastapi import HTTPException

from app.core.config import settings
from app.db import sessions, mobile_tool_identity, mobile_hub
from app.db.base import get_db


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
    provenance = mobile_tool_identity.read(target)
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
            allowed = bool(client.get('account_owner_admin') is True
                           and session.get('instance_id') == settings.effective_instance_id
                           and not session.get('api_user_id'))
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
