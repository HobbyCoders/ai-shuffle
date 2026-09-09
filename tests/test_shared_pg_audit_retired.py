"""The obsolete shared-server audit must not redirect runtime configuration."""
import os
from pathlib import Path

import pytest

from scripts.verify_shared_pg import VerificationError, configure


def test_retired_audit_refuses_before_changing_environment_or_files(tmp_path):
    before = dict(os.environ)
    runtime = tmp_path / "audit"
    with pytest.raises(VerificationError, match="Shared-database verification is retired"):
        configure(Path("not-read.env"), runtime, "unused")
    assert dict(os.environ) == before
    assert not runtime.exists()
