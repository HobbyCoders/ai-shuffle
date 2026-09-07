"""Authorize artifact bytes through the session that produced or references them."""
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
            if cursor.fetchone() is None:
                raise HTTPException(404, 'Not found')
    provenance = mobile_tool_identity.read(target)
    api_user = getattr(request.state, 'api_user', None)
    client = getattr(request.state, 'paired_client', None)
    allowed = False
    if api_user:
        from app.api.sessions_helpers import check_session_access_for_api_user
        check_session_access_for_api_user(api_user, session)
        allowed = session.get('api_user_id') == api_user['id']
    elif client:
        owner = mobile_hub.ownership(paired_client_id=client['id'])
        expected = 'account:' + owner['account_id'] if owner else 'paired:' + client['id']
        allowed = bool(provenance and provenance['owner'] == expected)
        if not provenance:
            allowed = bool(client.get('account_owner_admin') is True
                           and session.get('instance_id') == settings.effective_instance_id
                           and not session.get('api_user_id'))
    elif getattr(request.state, 'is_admin', False):
        if provenance and provenance.get('account_id'):
            from app.core.preference_identity import local_owner_account
            allowed = provenance['account_id'] == local_owner_account()
        else:
            allowed = session.get('instance_id') == settings.effective_instance_id
    if not allowed:
        raise HTTPException(404, 'Not found')
