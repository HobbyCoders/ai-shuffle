"""Trusted WS principals and live Android authority. No model-supplied identity."""
import hashlib
from app.db import mobile_tool_identity as store, pairing, mobile, mobile_hub
from app.core import account_hub


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def principal(kind, identity, token):
    value = dict(kind=kind, principal_id=identity, credential_hash=digest(token), account_id=None)
    if kind == 'paired':
        client = live_client(value)
        owner = mobile_hub.ownership(paired_client_id=client['id'])
        account_hub.require_active(owner)
        if owner:
            value['account_id'] = owner['account_id']
    value['owner'] = ('account:' + value['account_id'] if value['account_id'] else
                      'install-admin' if kind == 'admin' else kind + ':' + identity)
    return value


def live_client(value):
    client = pairing.get_paired_client_by_session_hash(value['credential_hash'])
    if (not client or client['id'] != value['principal_id']
            or (client.get('scopes') is not None and 'controller' not in client['scopes'])):
        raise PermissionError('Controller expired or revoked')
    account_hub.check_paired_client(client)
    return client


def validate(value):
    if not value:
        return
    if value['kind'] == 'admin':
        if not store.admin_active(value['credential_hash']):
            raise PermissionError('Admin session expired or logged out')
    elif value['kind'] == 'paired':
        client = live_client(value)
        owner = mobile_hub.ownership(paired_client_id=client['id'])
        account_hub.require_active(owner)
        if (owner['account_id'] if owner else None) != value.get('account_id'):
            raise PermissionError('Account membership changed')
    elif value['kind'] == 'api_user':
        # The key that authenticated must still be this user's live key: a
        # disabled, deleted or rotated key no longer matches.
        from app.db import database
        user = database.get_api_user_by_key_hash(value['credential_hash'])
        if not user or user['id'] != value['principal_id']:
            raise PermissionError('API key disabled, deleted or rotated')
    else:
        raise PermissionError('Unknown principal')


def revalidate(value):
    """:func:`validate` for an open socket's periodic re-check.

    A paired device must still be paired (not revoked), its token generation
    unchanged (the session hash still matches, so an unpair, a logout or a
    rotation all fail) and its account membership live. The device's live
    checks refuse with HTTP 401/403; those mean the principal is gone and
    become ``PermissionError`` like every other refusal. An outage (5xx) is
    raised as is, so the caller retries instead of dropping a live socket.
    """
    from fastapi import HTTPException
    try:
        validate(value)
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise PermissionError(str(exc.detail)) from exc
        raise


def request_principal(request):
    """The paired controller a REST request acts as, shaped like :func:`principal`.

    None for the owner login, an API key or a preview: a chat they create
    over REST stays without a record, as before (#1877). ``require_auth``
    already checked the device live; this only names it. Blocking.
    """
    state = getattr(request, 'state', None)
    client = getattr(state, 'paired_client', None)
    if not isinstance(client, dict) or not client.get('id'):
        return None
    owner = mobile_hub.ownership(paired_client_id=client['id'])
    account_id = owner['account_id'] if owner else None
    return dict(kind='paired', principal_id=client['id'], account_id=account_id,
                credential_hash=getattr(state, 'paired_credential_hash', None) or '',
                owner='account:' + account_id if account_id else 'paired:' + client['id'])


def record_creator(session_id, request, *, conn=None):
    """Record the paired controller that created a chat over REST (#1877).

    The same record the chat socket writes for a chat it creates
    (``bind_session(..., new=True)``), so the creator keeps the chat's
    images, transcript and socket, on this worker and on mirrors, and other
    account members do not. Nothing for the owner login or an API key.
    Blocking.
    """
    value = request_principal(request)
    if value:
        store.bind(session_id, value, new=True, conn=conn)
    return value


def bind_session(session_id, value, *, new=False, claim=False, conn=None):
    validate(value)
    return store.bind(session_id, value, new=new, claim=claim, conn=conn)


def _network_mirror(session):
    """True when another worker of this install's network runs the session."""
    try:
        from app.network import control, engine, membership
        ctx = engine.context()
        if ctx is None:
            return False
        owner = control.owner_of(session)
        member = ctx.member(owner) if owner else None
        return bool(member and member.kind == membership.KIND_WORKER
                    and member.status == membership.STATUS_ACTIVE)
    except Exception:  # noqa: BLE001 - an unreadable network context fails closed
        return False


def same_install_owner(previous, value, session):
    """Chat continuation is distinct from replacing Android tool provenance.

    A chat run by another worker of this network is a mirror here, so its
    instance_id is not this install's. The owner's own devices continue it all
    the same (the account checks below are unchanged); without this the
    owner's phone was refused a chat the owner's desktop started.
    """
    from app.core.config import settings
    if (not value or session['api_user_id']
            or (session['instance_id'] != settings.effective_instance_id
                and not _network_mirror(session))):
        return False
    if {previous['kind'], value['kind']} != {'admin', 'paired'}:
        return False
    paired_value = value if value['kind'] == 'paired' else previous
    if paired_value.get('principal_id') is None:
        # A synced label (``store.mirrored``): the device that started the
        # chat is paired with the worker that runs it, not here. Where that
        # worker checks the device's owner authority, this worker checks
        # that the account is its owner's, as the admin's image check does.
        if not paired_value.get('account_id'):
            return False
        from app.core.preference_identity import local_owner_account
        return paired_value['account_id'] == local_owner_account()
    return _owner_authority(paired_value)


def admit_labelled(label, value, session):
    """Continue a chat whose owner is only known from its synced label (#1877).

    ``label`` is ``store.mirrored(session)``: on a worker that mirrors the
    chat, or that runs it without a record of its own (after a handoff). The
    principal that started it, or a principal :func:`same_install_owner`
    accepts in its place, may continue it; anyone else is refused as the
    worker with the record refuses them.
    """
    if value and value['owner'] == label['owner']:
        return
    if not same_install_owner(label, value, session):
        raise PermissionError('Session principal mismatch')


def admit_unrecorded(value, session):
    """Continue a chat with no account record or label (a legacy chat).

    The admin and API keys keep using it (an API key's own scope is checked
    by the socket). A paired device needs this install's owner authority, the
    rule the chat's images use: another member's phone or a direct controller
    is refused.
    """
    if not value or value['kind'] != 'paired':
        return
    from app.core.config import settings
    if (session['api_user_id'] or (session['instance_id'] != settings.effective_instance_id
                                   and not _network_mirror(session))
            or not _owner_authority(value)):
        raise PermissionError('Session principal mismatch')


def owner_principal(value):
    """True when the chat's principal is the install owner acting.

    No record (a legacy install-local chat) and the admin count; so does a
    paired device that holds this install's owner authority, the owner's own
    signed-in phone or browser (#2218). A synced label without a local
    principal counts when its account is the owner's, as ``same_install_owner``
    decides. Anything else, or a check that cannot be made, fails closed.
    """
    if not value or value.get('kind') == 'admin':
        return True
    if value.get('kind') != 'paired':
        return False
    if value.get('principal_id') is None:
        if not value.get('account_id'):
            return False
        from app.core.preference_identity import local_owner_account
        try:
            return value['account_id'] == local_owner_account()
        except Exception:  # noqa: BLE001 - an unreadable owner account fails closed
            return False
    try:
        return _owner_authority(value)
    except Exception:  # noqa: BLE001 - a revoked or expired device fails closed
        return False


def _owner_authority(value):
    """True when a paired principal's device holds this install's owner authority."""
    client = pairing.get_paired_client(value['principal_id'])
    if not client or client.get('revoked_at'):
        return False
    owner = mobile_hub.ownership(paired_client_id=client['id'])
    return bool(owner and owner['account_id'] == value.get('account_id')
                and account_hub.check_paired_client(client) is True)


def authorized_device_ids(session_id):
    try:
        value = store.read(session_id)
        if not value:
            return []
        validate(value)
        if value['kind'] == 'api_user':
            return []  # No explicit API-user Android grant exists.
        if value.get('account_id'):
            membership = mobile_hub.ownership(paired_client_id=value['principal_id'])
            controller_device = mobile.get_mobile_device(membership['device_id'])
            owner_controller = account_hub.check_paired_client(live_client(value)) is True
            if (not controller_device or (not owner_controller
                    and 'mobile:actions' not in (controller_device.get('scopes') or []))):
                return []
        result = []
        for device in mobile.get_mobile_devices():
            if device.get('revoked_at') or 'mobile:actions' not in (device.get('scopes') or []):
                continue
            owner = mobile_hub.ownership(device['device_id'])
            if value['kind'] == 'admin':
                allowed = owner is None  # install admin is not a global hub admin
            elif value.get('account_id'):
                allowed = bool(owner and owner['account_id'] == value['account_id']
                               and owner['server_id'] == membership['server_id'])
            else:
                allowed = owner is None and device.get('paired_client_id') == value['principal_id']
            if allowed:
                account_hub.mobile_hub_ownership(device)
                result.append(device['device_id'])
        return result
    except Exception:
        # Network failures, revocation, and malformed membership all fail closed.
        return []
