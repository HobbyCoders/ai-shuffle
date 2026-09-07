"""Serialize enrollment of an identity, including when no row exists yet."""
import hashlib


def lock_device_identity(cursor, device_id: str) -> None:
    # A stable, namespaced signed bigint works across processes and hosts.
    # Collisions only serialize unrelated devices; they cannot grant access.
    key = int.from_bytes(hashlib.sha256(
        ('shuffle-device-identity:' + device_id).encode('utf-8')
    ).digest()[:8], 'big', signed=True)
    cursor.execute('SELECT pg_advisory_xact_lock(%s)', (key,))
