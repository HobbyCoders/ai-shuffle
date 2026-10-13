"""Plain helper functions copied across several tests/core files.

Import what you need explicitly.
"""
import uuid

from app.db import database


def make_profile(profile_id, config):
    """Create a profile named after its id in the test DB."""
    return database.create_profile(
        profile_id=profile_id,
        name=profile_id,
        description=None,
        config=config,
    )


def new_session(profile_id="default"):
    """Create a session with a random ``sess-`` id and return the id."""
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id=profile_id)
    return session_id


def query_profile(config=None):
    """An in-memory profile dict for ``build_options``-style calls."""
    cfg = {"model": "opus", "tools_preset": "claude_code"}
    if config:
        cfg.update(config)
    return {"id": "t", "config": cfg}


def make_pump(events):
    """An async-generator factory that yields ``events`` and ignores its kwargs."""
    async def _impl(**kwargs):
        for e in events:
            yield e
    return _impl
