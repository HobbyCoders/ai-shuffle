"""Every file the app writes a credential or token to is sensitive to Fluid.

Fluid's Bash risk classifier, its Read/Grep/Glob ask-rules and the delegated
tool policy all key off ``fluid.tools.hardening.is_sensitive_path``. These
tests build each token path from the app's own constants and helpers, so
renaming a key file without updating the sensitive lists fails here. The
module census fails when a new module starts writing private files, so the
reviewer adds its path below before it ships.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from fluid.tools.builtins.bash_risk import classify
from fluid.tools.hardening import is_sensitive_path

_REPO = Path(__file__).resolve().parents[2]

# Modules that write private files (protect_private_path / service_files).
# Each entry names what it writes and whether that is a credential.
_REVIEWED_PRIVATE_WRITERS = {
    "app/core/application_updates.py",  # update control/status json, no secret
    "app/core/config.py",  # managed-postgres.json (database password)
    "app/core/credential_keys.py",  # .credential-keys/<db>.key
    "app/core/managed_credentials.py",  # .managed-connection/<id>.key
    "app/core/preview_access.py",  # preview-access.key
    "app/core/private_storage.py",  # the helpers themselves
    "app/core/process_metrics.py",  # metrics samples, no secret
    "app/db/history_indexing.py",  # index state, no secret
    "app/maintenance/session_recall_cache.py",  # recall cache, no secret
    "app/network/identity.py",  # .credential-keys/device-identity.key
}


def _assert_sensitive(path: Path) -> None:
    text = str(path)
    assert is_sensitive_path(text), text
    assert classify({"command": f"cat '{text}'"}) == "write", text


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def test_app_token_files_are_sensitive(data_dir: Path) -> None:
    from app.core import claude_cli_home, credential_keys, managed_credentials, preview_access
    from app.network import identity

    _assert_sensitive(preview_access._key_path())
    _assert_sensitive(credential_keys.key_path(str(uuid.uuid4()), create=True))
    _assert_sensitive(managed_credentials._key_path())
    _assert_sensitive(data_dir / ".credential-keys" / identity.IDENTITY_FILE)
    account = claude_cli_home.account_dir("0123456789abcdef")
    _assert_sensitive(account / ".credentials.json")
    _assert_sensitive(claude_cli_home.root() / "any" / "settings.json")
    _assert_sensitive(data_dir / "managed-postgres.json")


def test_identity_key_lives_in_the_credential_key_directory() -> None:
    source = (_REPO / "app/network/identity.py").read_text(encoding="utf-8")
    assert re.search(r"effective_data_dir\s*/\s*[\"']\.credential-keys[\"']", source)


def test_private_file_writers_are_reviewed() -> None:
    pattern = re.compile(r"\b(?:protect_private_path|service_files)\b")
    writers = {
        path.relative_to(_REPO).as_posix()
        for path in (_REPO / "app").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    }
    assert writers == _REVIEWED_PRIVATE_WRITERS, (
        "A module started or stopped writing private files. If it stores a "
        "credential, add its path to fluid/fluid/tools/hardening.py and to "
        "test_app_token_files_are_sensitive, then update this set."
    )
