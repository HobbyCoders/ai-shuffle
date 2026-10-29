"""One-time cleanup of duplicate account phone entries on this worker.

Until the phone kept its installation identity across account sign-ins, every
sign-in (the account session lasts a week) enrolled the same phone again under
a new identity, leaving one approved entry per week under Phones and browsers.

Owner-approved for this cleanup only: within this worker's account binding,
entries are grouped by device name and platform. In each group the entry the
phone is using is kept and older copies are revoked through the normal revoke
path (account registration first, then the local paired device), exactly as
Revoke on the Devices page does. Runs until it completes once per account
binding, then never again.

Which entries are kept, per group:
- activity = latest of enrollment time and last use; the keeper is the entry
  with the latest activity (the one in use, or the newest enrollment when none
  has been used).
- Every entry whose activity is at or after the keeper's enrollment is kept
  too: something still used it after the keeper existed (a second install with
  the same name), or it was enrolled later and not used yet.
- Only entries last active before the keeper was enrolled are revoked: the
  phone had already moved on to the keeper's identity. Denied entries and
  entries with unknown times are left alone.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core import account_hub
from app.db import database
from app.db import mobile_hub as store

logger = logging.getLogger(__name__)
MARKER = 'mobile_hub_duplicate_cleanup_v1:'
TOTAL_TIMEOUT = 120


def _ts(value):
    if value is None or value == '':
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def stale_entries(rows):
    """The duplicate entries to revoke (see the module docstring)."""
    groups = defaultdict(list)
    for row in rows:
        if row.get('decision') == 'denied':
            continue
        name = ' '.join(str(row.get('device_name') or '').split()).casefold()
        if not name:
            continue
        groups[(row['server_id'], row['account_id'], name, row.get('platform'))].append(row)
    stale = []
    for entries in groups.values():
        if len(entries) < 2:
            continue
        enrolled = [_ts(e.get('created_at')) for e in entries]
        if any(t is None for t in enrolled):
            continue  # Unknown history: keep everything.
        activity = [max(t for t in (enrolled[i], _ts(e.get('client_seen')), _ts(e.get('device_seen'))) if t)
                    for i, e in enumerate(entries)]
        keeper = max(range(len(entries)), key=lambda i: (activity[i], enrolled[i]))
        stale.extend(e for i, e in enumerate(entries) if i != keeper and activity[i] < enrolled[keeper])
    return stale


def _revoke_on_account(binding, hub_device_id):
    try:
        result = account_hub.call(binding, 'DELETE', 'account/devices/' + account_hub.identifier(hub_device_id))
    except HTTPException as exc:
        if exc.status_code == 404:
            return  # Not (or no longer) a registration of this account.
        raise
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise RuntimeError('Account revocation was not confirmed')


def run_once():
    """Returns the number of revoked entries, or None when it did not run."""
    binding = store.get_binding()
    if not binding or not binding['active']:
        return None
    marker = MARKER + binding['server_id']
    if database.get_system_setting(marker):
        return None
    from app.api.ws_key_watch import close_paired_client_sockets
    revoked = failures = 0
    for entry in stale_entries(store.live_account_entries(binding['server_id'])):
        try:
            # Account authority first, so it cannot readmit the local entry.
            _revoke_on_account(binding, entry['hub_device_id'])
            if database.revoke_paired_client(entry['paired_client_id']):
                close_paired_client_sockets(entry['paired_client_id'])
                revoked += 1
        except Exception:  # noqa: BLE001 - retried on the next start
            failures += 1
    if revoked:
        account_hub.reset_verification_cache()
    if not failures:
        database.set_system_setting(marker, '1')
    logger.info('Duplicate phone cleanup: revoked=%d, failures=%d', revoked, failures)
    return revoked


async def cleanup_once() -> None:
    """Background startup task; never fails startup."""
    try:
        await asyncio.wait_for(asyncio.to_thread(run_once), timeout=TOTAL_TIMEOUT)
    except Exception:
        logger.warning('Duplicate phone cleanup did not finish; it runs again next start', exc_info=True)
