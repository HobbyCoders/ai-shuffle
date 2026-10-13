"""Session rows as a client may see them. Pure dict helpers, no I/O."""

from datetime import datetime

from pydantic import TypeAdapter


_SESSION_TIMESTAMP = TypeAdapter(datetime)


def serialize_session_updated_at(value: object) -> str | None:
    """Match Session's datetime JSON precision and UTC suffix in untyped feeds."""
    if value is None:
        return None
    return _SESSION_TIMESTAMP.dump_python(
        _SESSION_TIMESTAMP.validate_python(value), mode="json",
    )


#: Session columns that travel between workers but never to a client.
SERVER_ONLY_SESSION_FIELDS = ('provenance_owner',)


def client_view(session):
    """A session row as a client may see it: without the account label.

    The label (``provenance_owner``) names who started the chat, an account
    or paired-device id; the image check uses it, and it syncs between
    workers, but it is not part of any session payload sent to a client.
    """
    if not isinstance(session, dict):
        return session
    return {k: v for k, v in session.items() if k not in SERVER_ONLY_SESSION_FIELDS}
