"""Account-scoped UI state derived only from authenticated server principals."""
from app.core import account_hub, owner_auth
from app.core.config import settings
from app.db import mobile_hub, pairing
from fastapi import HTTPException
import threading
import time


_owner_cache_lock = threading.Lock()
_owner_cache = {}
_OWNER_CACHE_SECONDS = 60


def _binding_account(binding):
    """Resolve the owning account without requiring a phone to enroll first."""
    key = (settings.effective_instance_id, account_hub.configured_origin(),
           binding["server_id"], binding.get("encrypted_secret"))
    with _owner_cache_lock:
        cached = _owner_cache.get(key)
        if cached and time.monotonic() - cached[1] < _OWNER_CACHE_SECONDS:
            return cached[0]
        # This is the trusted worker backchannel, not a caller-supplied account ID.
        result = account_hub.call(binding, "GET", "account")
        account = result.get("account") if isinstance(result, dict) else None
        try:
            account_id = account_hub.identifier(account.get("id") if isinstance(account, dict) else None)
        except (HTTPException, ValueError, TypeError) as exc:
            raise HTTPException(502, "The account service did not return a valid owner identity") from exc
        _owner_cache.clear()
        _owner_cache[key] = (account_id, time.monotonic())
        return account_id


def account_identity(account_id):
    # Account IDs are scoped to the administrator-selected trust root.
    return 'account', account_hub.configured_origin() + '#' + account_id


def local_owner_account():
    """Resolve the managed owner from an enrolled controller or the account hub.

    A fresh worker has no enrolled controller yet. Its authenticated account
    profile supplies the same stable identity, never a username or local alias.
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
    return next(iter(accounts)) if accounts else _binding_account(binding)


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
