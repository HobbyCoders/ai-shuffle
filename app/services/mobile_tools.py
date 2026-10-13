"""Validated Android commands, with server-owned session authorization.

The concrete identity resolver receives the runtime AI session id and rechecks
durable authenticated provenance and live account membership. The callback is
replaceable for isolated tests; None explicitly disables device access. Mobile
poll/result routes independently enforce the current aim_ credential and scope.
"""
import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

from app.db import mobile_tools as queue
from app.db import mobile
from app.db import pairing
from app.core.device_keys import key_expiry

# Trusted application integration only; not a tool argument or an HTTP endpoint.
from app.core.mobile_tool_identity import authorized_device_ids
device_authorizer: Callable[[str], list[str]] | None = authorized_device_ids
MAX_ARGUMENT_BYTES = 8192
MAX_RESULT_BYTES = 65536
MAX_WAIT_SECONDS = 25

# Required and optional fields. Extra fields (including confirmed=true) are rejected.
FIELDS = {
    'open_app': ({'package_name': str}, {}),
    'open_url': ({'url': str}, {}),
    'navigate_to': ({'destination': str}, {}),
    'set_alarm': ({'hour': int, 'minute': int}, {'label': str}),
    'set_timer': ({'seconds': int}, {'label': str}),
    'create_calendar_event': ({'title': str, 'start_time': str}, {'end_time': str, 'description': str, 'location': str}),
    'get_location': ({}, {}),
    'read_screen': ({}, {}),
    'tap_node': ({'ref': str}, {}),
    'scroll': ({'ref': str, 'direction': str}, {}),
    'go_back': ({'ref': str}, {}),
}
TOOLS = frozenset(FIELDS)
HANDOFFS = frozenset({'open_app', 'open_url', 'navigate_to', 'set_alarm', 'set_timer', 'create_calendar_event'})


class CommandError(ValueError):
    pass


def bounded_json(value, limit):
    if not isinstance(value, dict):
        raise CommandError('object_required')
    def check(item, depth=0):
        if depth > 12:
            raise CommandError('payload_too_deep')
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise CommandError('invalid_json')
            for v in item.values():
                check(v, depth + 1)
        elif isinstance(item, list):
            for v in item:
                check(v, depth + 1)
        elif not (item is None or type(item) in (str, bool, int, float)):
            raise CommandError('invalid_json')
    check(value)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise CommandError('invalid_json') from None
    if len(encoded.encode('utf-8')) > limit:
        raise CommandError('payload_too_large')
    return encoded


def validate_arguments(name, arguments):
    encoded = bounded_json(arguments, MAX_ARGUMENT_BYTES)
    if name not in TOOLS:
        raise CommandError('unknown_tool')
    required, optional = FIELDS[name]
    fields = required | optional
    if not required.keys() <= arguments.keys() or arguments.keys() - fields.keys():
        raise CommandError('invalid_arguments')
    for key, value in arguments.items():
        if type(value) is not fields[key] or (isinstance(value, str) and (not value.strip() or len(value) > 2048)):
            raise CommandError('invalid_arguments')
    if name == 'open_url':
        # urlsplit raises ValueError (not CommandError) on an unterminated IPv6
        # host, and .port on a non-numeric or out-of-range port; execute()
        # only converts CommandError, so either used to escape the tool call.
        try:
            url = urlsplit(arguments['url'])
            _ = url.port
        except ValueError:
            raise CommandError('invalid_url') from None
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            raise CommandError('invalid_url')
    if name == 'open_app':
        import re
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', arguments['package_name']):
            raise CommandError('invalid_package')
    if name == 'set_alarm' and not (0 <= arguments['hour'] <= 23 and 0 <= arguments['minute'] <= 59):
        raise CommandError('invalid_alarm')
    if name == 'set_timer' and not 1 <= arguments['seconds'] <= 86400:
        raise CommandError('invalid_timer')
    if name == 'scroll' and arguments['direction'] not in ('up', 'down', 'left', 'right'):
        raise CommandError('invalid_direction')
    if name == 'create_calendar_event':
        try:
            start = datetime.fromisoformat(arguments['start_time'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(arguments.get('end_time', arguments['start_time']).replace('Z', '+00:00'))
            if start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError()
        except ValueError:
            raise CommandError('invalid_calendar_time') from None
    return encoded


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value


def authorized_devices(session_id):
    if not session_id or device_authorizer is None:
        raise CommandError('controller_context_missing: wire authenticated session/controller ownership resolver')
    ids = device_authorizer(session_id)
    if not isinstance(ids, (list, tuple, set)) or any(not isinstance(i, str) for i in ids):
        raise CommandError('controller_context_invalid')
    devices = []
    at = datetime.now(timezone.utc)
    for device_id in set(ids):
        device = mobile.get_mobile_device(device_id)
        if not device or device.get('revoked_at') or 'mobile:actions' not in device.get('scopes', []):
            continue
        # The canonical rule (explicit expiry, else created_at + the legacy
        # lifetime, else expired), always UTC-aware: a naive stored timestamp
        # used to raise TypeError against the aware ``at``.
        if key_expiry(device) <= at:
            continue
        if device.get('paired_client_id'):
            client = pairing.get_paired_client(device['paired_client_id'])
            if not client or client.get('revoked_at'):
                continue
        devices.append(device)
    return devices


def list_devices(session_id):
    result = []
    for device in authorized_devices(session_id):
        state = queue.state(device['device_id']) or {}
        heartbeat = _date(state.get('heartbeat_at'))
        result.append({'device_id': device['device_id'], 'name': device['device_name'],
                       'enabled': bool(state.get('enabled')), 'tools': json.loads(state.get('tools', '[]')),
                       'last_seen': heartbeat.isoformat() if heartbeat else None,
                       'online': bool(heartbeat and (datetime.now(timezone.utc) - heartbeat).total_seconds() <= 30)})
    return {'devices': result, **({'error': 'no_devices'} if not result else {})}


async def execute(session_id, device_id, name, arguments, timeout=MAX_WAIT_SECONDS):
    command_id = None
    try:
        if not isinstance(device_id, str) or not device_id or len(device_id) > 256:
            raise CommandError('explicit_device_id_required')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_WAIT_SECONDS:
            raise CommandError('invalid_timeout')
        encoded = validate_arguments(name, arguments)
        devices = await asyncio.to_thread(authorized_devices, session_id)
        if not any(d['device_id'] == device_id for d in devices):
            raise CommandError('device_not_authorized')
        queued = await asyncio.to_thread(queue.enqueue, device_id, session_id, name, encoded, timeout)
        if 'error' in queued:
            return queued
        command_id = queued['id']
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            # Recheck controller/membership/revocation during the wait, not only at enqueue.
            devices = await asyncio.to_thread(authorized_devices, session_id)
            if not any(d['device_id'] == device_id for d in devices):
                raise CommandError('device_not_authorized')
            row = await asyncio.to_thread(queue.outcome, command_id, session_id)
            if row and row['status'] == 'completed':
                result = json.loads(row['result'])
                # OS intents only launch confirmation; they cannot prove an external action completed.
                if name in HANDOFFS and not result.get('error') and (result.get('success') is True or result.get('status') in ('completed', 'launched', 'launched_confirmation')):
                    result = dict(result, status='launched_confirmation')
                return {'command_id': command_id, 'result': result}
            if not row or row['status'] in ('expired', 'cancelled'):
                return {'error': 'command_expired_or_cancelled'}
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        return {'error': 'device_timeout'}
    except CommandError as exc:
        return {'error': str(exc)}
    finally:
        if command_id:
            # Cancels undelivered work; already claimed side effects cannot be undone.
            await asyncio.shield(asyncio.to_thread(queue.cancel, command_id, session_id))
