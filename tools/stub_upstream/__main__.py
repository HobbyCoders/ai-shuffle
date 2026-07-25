"""``python -m tools.stub_upstream`` entry point. TEST / DEV TOOLING ONLY."""

from __future__ import annotations

import os
import sys

# Allow ``python tools/stub_upstream/__main__.py`` as well as ``-m``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.stub_upstream.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
