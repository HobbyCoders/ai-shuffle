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


def lock_paired_credential(cursor, principal):
    """Serialize a checked principal with renewal/logout, using this transaction.

    Callers validate account membership before taking DB locks. Recheck only
    the credential here so concurrent binds cannot overwrite a renewed hash,
    without nested connection checkouts or network requests under the lock.
    """
    if not principal or principal['kind'] != 'paired':
        return
    cursor.execute('SELECT id FROM paired_clients WHERE id=%s FOR SHARE',
                   (principal['principal_id'],))
    cursor.execute("""SELECT id FROM paired_clients p WHERE p.id=%s
        AND p.revoked_at IS NULL AND (
            (p.session_token_hash=%s AND p.session_expires_at>NOW()) OR EXISTS (
                SELECT 1 FROM paired_client_native_sessions n WHERE n.client_id=p.id
                AND n.token_hash=%s AND n.expires_at>NOW()))""",
        (principal['principal_id'], principal['credential_hash'], principal['credential_hash']))
    if cursor.fetchone() is None:
        from app.core.mobile_tool_identity import CONTROLLER_RECOVERY
        raise PermissionError(CONTROLLER_RECOVERY)


def bind(session_id, principal, *, new=False, claim=False, conn=None):
    if conn is None:
        with get_db() as connection:
            return bind(session_id, principal, new=new, claim=claim, conn=connection)
    c = conn.cursor()
    c.execute("SELECT id, api_user_id, instance_id, owner_device_id, provenance_owner "
              "FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
    session = c.fetchone()
    if not session:
        raise PermissionError('Unknown session')
    c.execute("SELECT * FROM mobile_tool_identity WHERE session_id = %s", (session_id,))
    previous = c.fetchone()
    if not new and not claim:
        # Continuing a chat: decide from the record the image check uses
        # (``artifact_access.session_provenance``), so the transcript and the
        # images of a chat get one answer (#1877). A chat another worker runs
        # is decided by that worker's synced label; a chat with no record here
        # by its label when it has one. Neither writes anything.
        carried = mirrored(dict(session))
        if carried is not None and (not previous or _runs_elsewhere(session)):
            from app.core.mobile_tool_identity import admit_labelled
            admit_labelled(carried, principal, dict(session))
            return
        if not previous:
            # Unmapped legacy sessions remain usable but never acquire
            # authority; a paired device needs the owner's authority, as
            # for their images.
            from app.core.mobile_tool_identity import admit_unrecorded
            admit_unrecorded(principal, dict(session))
            return
    if previous:
        if not principal or previous['owner'] != principal['owner']:
            from app.core.mobile_tool_identity import same_install_owner
            if same_install_owner(dict(previous), principal, dict(session)):
                # Permit owner continuation without transferring Android grants
                # or overwriting the original account/device provenance.
                return
            raise PermissionError('Session principal mismatch')
    elif not new:
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
        # The session row carries the record's owner to the workers that
        # mirror this chat (``mirrored``), so where the chat runs the column
        # follows the record this worker decides from. The owner never
        # changes once recorded (ON CONFLICT keeps it); a first record here
        # can still differ from a label mirrored before the chat moved to this
        # worker (an admin claim after a handoff), and then replaces it. A
        # worker the chat moved away from keeps the running worker's label.
        if not _runs_elsewhere(session):
            owner = previous['owner'] if previous else principal['owner']
            c.execute("UPDATE sessions SET provenance_owner = %s WHERE id = %s "
                      "AND (provenance_owner IS NULL OR provenance_owner <> %s)",
                      (owner, session_id, owner))


def owner_label(session_id, conn=None):
    """Who started ``session_id``, as its row says (``provenance_owner``).

    Where the chat runs, ``bind`` and the migration keep the column equal to
    the local record's owner; elsewhere it is the running worker's label.
    """
    if conn is None:
        with get_db() as connection:
            return owner_label(session_id, connection)
    c = conn.cursor()
    c.execute("SELECT provenance_owner FROM sessions WHERE id = %s", (session_id,))
    row = c.fetchone()
    return row['provenance_owner'] if row else None


def inherit_label(session_id, parent_session_id, conn=None):
    """Give a chat started by another chat (a Task or Swarm worker, a peer) the
    parent's label, at creation (#1877).

    The parent's creator then reads the child as it reads the parent, and other
    account members do not, on this worker and on mirrors. Only the label: the
    child gets no record, so no Android tool authority. A parent with no label
    leaves the child without one (the same rule as the parent). Returns the
    label written, or None.
    """
    if conn is None:
        with get_db() as connection:
            return inherit_label(session_id, parent_session_id, connection)
    if not parent_session_id:
        return None
    owner = owner_label(parent_session_id, conn)
    if mirrored({'id': session_id, 'provenance_owner': owner}) is None:
        return None
    conn.cursor().execute("UPDATE sessions SET provenance_owner = %s WHERE id = %s "
                          "AND provenance_owner IS NULL", (owner, session_id))
    return owner


def _runs_elsewhere(session):
    """True when another worker of this network runs the chat."""
    try:
        from app.network import control
        return control.owner_of(dict(session)) is not None
    except Exception:  # noqa: BLE001 - no readable network: the chat runs here
        return False


def mirrored(session):
    """The account record a session row carries, shaped like :func:`read`.

    Set on the worker that ran the chat when its record was bound, and
    synced with the session catalog, so a worker that only mirrors the chat
    (and so has no ``mobile_tool_identity`` row) knows whose it is. Only the
    owner travels: principal ids and credential hashes stay on that worker,
    so ``principal_id`` and ``credential_hash`` are None here. None when the
    row names no owner (an owner on an older build, or a chat with no record).
    """
    owner = (session or {}).get('provenance_owner')
    if not isinstance(owner, str) or not owner:
        return None
    if owner == 'install-admin':
        kind, account_id = 'admin', None
    elif owner.startswith('account:') and len(owner) > len('account:'):
        # Only a paired device's principal carries an account id.
        kind, account_id = 'paired', owner[len('account:'):]
    elif owner.startswith(('paired:', 'api_user:')):
        kind, account_id = owner.split(':', 1)[0], None
    else:
        return None  # An owner this build cannot interpret proves nothing.
    return dict(session_id=session.get('id'), owner=owner, kind=kind, principal_id=None,
                account_id=account_id, credential_hash=None)

