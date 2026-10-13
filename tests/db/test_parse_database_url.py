"""Unit tests for app.db.base.parse_database_url — DSN decomposition.

Pure-function tests: no database. The parser must tolerate passwords with
reserved characters (literal ``@``) and percent-encoding, because libpq cannot
parse either from a raw DSN string (observed in production: a literal ``@`` in
the password made the event-gateway listener resolve the host as
``"Xyz@192.0.2.10"`` and crash-loop).
"""

from app.db.base import parse_database_url


def test_plain_url():
    parsed = parse_database_url("postgresql://alice:secret@db.example.com:5433/appdb")
    assert parsed == {
        "host": "db.example.com",
        "port": 5433,
        "dbname": "appdb",
        "user": "alice",
        "password": "secret",
    }


def test_percent_encoded_password_is_decoded():
    parsed = parse_database_url("postgresql://alice:p%40ss@192.0.2.10:5432/appdb")
    assert parsed["host"] == "192.0.2.10"
    assert parsed["password"] == "p@ss"


def test_literal_at_in_password_keeps_real_host():
    # urlparse splits userinfo at the LAST '@', unlike libpq.
    parsed = parse_database_url("postgresql://alice:p@ssXyz@192.0.2.10:5432/appdb")
    assert parsed["host"] == "192.0.2.10"
    assert parsed["port"] == 5432
    assert parsed["user"] == "alice"
    assert parsed["password"] == "p@ssXyz"


def test_scheme_prefix_is_optional():
    parsed = parse_database_url("alice:secret@localhost/appdb")
    assert parsed["host"] == "localhost"
    assert parsed["dbname"] == "appdb"


def test_port_defaults_to_5432():
    assert parse_database_url("postgresql://a:b@h/db")["port"] == 5432


def test_missing_credentials_default_to_empty():
    parsed = parse_database_url("postgresql://localhost/db")
    assert parsed["user"] == ""
    assert parsed["password"] == ""
