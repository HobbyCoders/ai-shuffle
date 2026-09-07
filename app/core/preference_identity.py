"""Account-scoped UI state derived only from authenticated server principals."""
from app.core import account_hub, owner_auth
from app.core.config import settings
from app.db import mobile_hub, pairing
from fastapi import HTTPException


def account_identity(account_id):
    # Account IDs are scoped to the administrator-selected trust root.
    return 'account', account_hub.configured_origin() + '#' + account_id


def local_owner_account():
    """Resolve this managed worker's owner using a live enrolled controller.

    A worker binding alone has no account ID. Require live proof rather than
    deriving an alias from another install, a username, or a caller's header.
    """
    binding = mobile_hub.get_binding()
    if (not binding or not binding.get('active')
            or binding.get('linked_by') != 'managed:' + settings.effective_instance_id):
        return None
    accounts = set()
    for client in pairing.list_paired_clients():
        owner = mobile_hub.ownership(paired_client_id=client['id'])
        if not owner or owner['server_id'] != binding['server_id']:
            continue
        try:
            if account_hub.check_paired_client(client) is True:
                accounts.add(owner['account_id'])
        except HTTPException as exc:
            if exc.status_code >= 500:
                raise  # Do not silently switch namespaces during hub outages.
    if len(accounts) > 1:
        raise PermissionError('Worker owner identity is ambiguous')
    return next(iter(accounts), None)


def principal_preference_identity(principal):
    if not principal:
        raise PermissionError('Authenticated identity required')
    if principal['kind'] == 'api_user':
        return 'api_user', principal['principal_id']
    if principal['kind'] == 'paired':
        if principal.get('account_id'):
            return account_identity(principal['account_id'])
        return 'paired', principal['principal_id']
    if principal['kind'] == 'admin':
        account = local_owner_account()
        return account_identity(account) if account else ('admin', owner_auth.preference_identity())
    raise PermissionError('Unknown identity')


def request_preference_identity(request):
    api_user = getattr(request.state, 'api_user', None)
    if api_user:
        return 'api_user', api_user['id']
    client = getattr(request.state, 'paired_client', None)
    if client:
        owner = mobile_hub.ownership(paired_client_id=client['id'])
        # require_auth already validated the credential and live membership.
        return account_identity(owner['account_id']) if owner else ('paired', client['id'])
    if getattr(request.state, 'is_admin', False):
        return principal_preference_identity({'kind': 'admin'})
    raise PermissionError('Authenticated identity required')
