"""The environment agent-driven subprocesses inherit from the host process.

A Bash command or a delegated CLI child runs model-chosen commands, so it must
not inherit the host's own secrets: an app admin password, a lifecycle token,
database connection settings or stray ``*_TOKEN`` / ``*_SECRET`` values. Those
are dropped from the inherited environment. Everything a caller layers on top
afterwards (settings ``env``, the provider keys a session injects on purpose)
is left alone, because that is a deliberate hand-over, not inheritance.

The list mirrors what the desktop shell already strips from the worker
(``desktop/src/main/worker.ts``). ``FLUID_CHILD_ENV_PASSTHROUGH`` names extra
variables (comma separated) that should still reach children.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

PASSTHROUGH_ENV = "FLUID_CHILD_ENV_PASSTHROUGH"
# Set by a caller in the environment it hands over (never in the host's own):
# comma-separated names to drop from what the child INHERITS, for a variable
# that is allowed in general but must not reach this particular child (an
# Anthropic key the operator set on the container, while the user chose to run
# Claude Code on their subscription). The marker itself never reaches the child.
UNSET_ENV = "FLUID_CHILD_ENV_UNSET"

# Host-only secrets, dropped by exact name (case-insensitive).
_DENY_NAMES = frozenset({
    "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "SHUFFLE_DESKTOP_ATTACH_TOKEN", "SHUFFLE_DESKTOP_LISTENER_TOKEN",
    "OWNER_PASSWORD", "LICENSE_KEY",
    "ADMIN_PASSWORD", "ADMIN_USERNAME", "SECRET_KEY", "CLUSTER_SECRET",
    "DATABASE_URL", "DSN", "JWT_SECRET", "SESSION_SECRET", "AUTH_SECRET",
})
# Database connection settings: libpq's PG* family, POSTGRES_* and DATABASE_*.
# PGID is the container's group id (PUID/PGID), not a libpq setting.
_DENY_PREFIX_RE = re.compile(r"^(?:PG(?!ID$)|POSTGRES_|DATABASE_)", re.IGNORECASE)
# Credential-shaped names.
_DENY_SUFFIX_RE = re.compile(
    r"(?:_SECRET|_TOKEN|_PASSWORD|_PASSWD|_API_KEY|_PRIVATE_KEY|_ACCESS_KEY|"
    r"_SECRET_KEY|_SECRET_ACCESS_KEY|_ACCESS_KEY_ID|_CREDENTIALS|_DSN|"
    r"_COOKIE|_SIGNING_KEY|_ENCRYPTION_KEY|_JWT_KEY)$",
    re.IGNORECASE,
)
# What tools legitimately use from the inherited environment even though the
# name looks like a credential: model-provider and fal.ai keys an operator set
# on the container, and the GitHub CLI token. Plain variables (PATH, HOME,
# LANG, TERM, proxies, ...) never match the deny rules in the first place.
ALLOWED_CREDENTIAL_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "FAL_KEY", "FAL_API_KEY",
    "GH_TOKEN", "GITHUB_TOKEN",
})


def _passthrough(environ: Mapping[str, str]) -> frozenset[str]:
    raw = environ.get(PASSTHROUGH_ENV) or ""
    return frozenset(p.strip().upper() for p in raw.split(",") if p.strip())


def is_host_secret(name: str, extra_allowed: frozenset[str] = frozenset()) -> bool:
    """True when ``name`` must not be inherited by an agent subprocess."""
    upper = name.upper()
    # A passthrough may allow a build/deploy token, never the app database or
    # authentication secrets. These remain private even with a broad override.
    if is_app_secret(upper):
        return True
    if upper in ALLOWED_CREDENTIAL_NAMES or upper in extra_allowed:
        return False
    return bool(_DENY_PREFIX_RE.match(upper) or _DENY_SUFFIX_RE.search(upper))


def is_app_secret(name: str) -> bool:
    """True for the host app's own secrets (admin password, lifecycle tokens,
    database settings): the named part of :func:`is_host_secret`, without the
    credential-suffix rule that also matches the owner's own ``*_TOKEN``s."""
    upper = name.upper()
    return upper in _DENY_NAMES or upper.endswith("_DSN") or bool(_DENY_PREFIX_RE.match(upper))


def scrub_child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``environ`` (default ``os.environ``) without host secrets."""
    source = os.environ if environ is None else environ
    keep = _passthrough(source)
    return {k: v for k, v in source.items()
            if k.upper() not in {PASSTHROUGH_ENV, UNSET_ENV} and not is_host_secret(k, keep)}


def _names(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def child_env_with_overlay(overlay: Mapping[str, str] | None,
                           environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The inherited environment (scrubbed), minus the names the overlay's
    ``FLUID_CHILD_ENV_UNSET`` lists, with the overlay on top."""
    env = scrub_child_env(environ)
    extra = dict(overlay or {})
    for name in _names(extra.pop(UNSET_ENV, None)):
        env.pop(name, None)
    env.update(extra)
    return env
