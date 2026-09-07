"""Durable mobile-tool provenance; one row per AI session, deleted with it.

All binds lock the parent session row, including the initially absent mapping.
This serializes concurrent cross-worker first claims without last-writer wins.
"""
from app.db.base import get_db


def init_schema():
    with get_db() as conn:
        conn.cursor().execute("""CREATE TABLE IF NOT EXISTS mobile_tool_identity (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            owner TEXT NOT NULL, kind TEXT NOT NULL,
            principal_id TEXT NOT NULL, account_id TEXT,
            credential_hash TEXT NOT NULL)""")


def read(session_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM mobile_tool_identity WHERE session_id = %s", (session_id,))
        row = c.fetchone()
        return dict(row) if row else None


def admin_active(digest):
    # Local transport authority cannot be replayed as an HTTP session token.
    from app.core import owner_auth
    if (digest == owner_auth.digest('local-transport:' + owner_auth.settings.effective_instance_id)
            and owner_auth.settings.shuffle_local_access):
        return True
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT 1 FROM owner_local_sessions
            WHERE token_hash = %s AND instance_id=%s
              AND expires_at > %s LIMIT 1""", (digest, owner_auth.settings.effective_instance_id,
                                              owner_auth.now().isoformat()))
        return c.fetchone() is not None


def bind(session_id, principal, *, new=False, claim=False, conn=None):
    if conn is None:
        with get_db() as connection:
            return bind(session_id, principal, new=new, claim=claim, conn=connection)
    c = conn.cursor()
    c.execute("SELECT api_user_id, instance_id FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
    session = c.fetchone()
    if not session:
        raise PermissionError('Unknown session')
    c.execute("SELECT * FROM mobile_tool_identity WHERE session_id = %s", (session_id,))
    previous = c.fetchone()
    if previous:
        if not principal or previous['owner'] != principal['owner']:
            raise PermissionError('Session principal mismatch')
    elif not new:
        if not claim:
            # Unmapped legacy sessions remain usable but never acquire authority.
            return
        from app.core.config import settings
        if (not principal or principal['kind'] != 'admin' or session['api_user_id']
                or session['instance_id'] != settings.effective_instance_id):
            raise PermissionError('Legacy session is not install-owned')
    if principal and (previous or new or claim):
        c.execute("""INSERT INTO mobile_tool_identity
            (session_id, owner, kind, principal_id, account_id, credential_hash)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(session_id) DO UPDATE SET
            kind=EXCLUDED.kind, principal_id=EXCLUDED.principal_id,
            account_id=EXCLUDED.account_id, credential_hash=EXCLUDED.credential_hash""",
            (session_id, principal['owner'], principal['kind'], principal['principal_id'],
             principal.get('account_id'), principal['credential_hash']))
