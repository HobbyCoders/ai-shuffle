"""Plain helper functions copied across test files in several directories.

Import what you need explicitly. Fixtures live in tests/shared_fixtures.py.
"""
from app.core import encryption
from app.db import database as db


def unlock_encryption(mock_db):
    """Set the test encryption key; ``mock_db`` is the caller's test DB fixture."""
    encryption.set_encryption_key("test-password", db)


def sent_frames(handler, ftype):
    """Frames of type ``ftype`` that ``handler.send_json`` (a mock) was called with."""
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == ftype
    ]


def fluid_message(role, blocks):
    """A Fluid transcript message with ``blocks`` as its content."""
    return {"role": role, "content": blocks, "provider_meta": {}}
