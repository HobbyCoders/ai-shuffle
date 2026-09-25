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


def bind_session(session_id, value, *, new=False, claim=False, conn=None):
    validate(value)
    return store.bind(session_id, value, new=new, claim=claim, conn=conn)


def same_install_owner(previous, value, session):
    """Chat continuation is distinct from replacing Android tool provenance."""
    from app.core.config import settings
    if (not value or session['api_user_id']
            or session['instance_id'] != settings.effective_instance_id):
        return False
    if {previous['kind'], value['kind']} != {'admin', 'paired'}:
        return False
    paired_value = value if value['kind'] == 'paired' else previous
    client = pairing.get_paired_client(paired_value['principal_id'])
    if not client or client.get('revoked_at'):
        return False
    owner = mobile_hub.ownership(paired_client_id=client['id'])
    return bool(owner and owner['account_id'] == paired_value.get('account_id')
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
