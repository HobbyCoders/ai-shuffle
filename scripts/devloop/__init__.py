"""One-command dev instance: ``python -m scripts.devloop up`` (see docs/internal/dev-loop.md).

* :mod:`scripts.devloop.fake_wire` — fake provider wire builders and server
  (shared with the native-leg tests).
* :mod:`scripts.devloop.fake_ai` — the scripted fake AI and its scenarios.
* :mod:`scripts.devloop.fake_claude` — a scripted ``claude`` CLI on the
  instance's PATH, so Claude Code chats run too.
* :mod:`scripts.devloop.seed` — seed data, written inside the app process.
* :mod:`scripts.devloop.__main__` — ``up`` / ``down`` / ``status``.
"""
