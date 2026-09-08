"""Cross-install cluster primitives: shared secret, peer auth, advertise URL.

TRANSITION WINDOW (worker network, 2026-09). Installs that have joined a
network no longer share a database: the runner directory comes from
``app.network.runners`` (``get_installs`` returns it when enrolled), pairing
codes are hashed under a local pairing key, and peers authenticate with
signed device tokens. What remains here serves installs that have not
enrolled yet and the legacy artifact redirect. Retire once every deployment
reports Ready in Settings > Devices. See docs/network/README.md.

When multiple AI Shuffle installs share one database they form a cluster. They
authenticate to each other with a single shared *cluster secret* (every install
on the shared DB converges on the same value) rather than per-peer API keys.
This module owns:

- ``get_cluster_secret()`` — the shared bearer secret (env override or
  auto-generated once in system_settings).
- ``require_cluster_auth`` — a FastAPI dependency that gates the peer-facing
  ``/api/v1/cluster`` endpoints.
- ``advertise_url()`` / ``register_self()`` — how this install publishes its
  identity + peer-reachable URL into the registry, plus a heartbeat helper.

The trust boundary is the shared database + shared secret: any install that can
read the DB and present the secret is trusted to drive any session. This is the
same boundary as sharing a Postgres today.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import socket
import time
from typing import Optional

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

CLUSTER_SECRET_SETTING = "cluster_secret"
# Legacy *global* advertise-URL key. Before per-install scoping, every install
# read/wrote this one key in the shared DB, so an override set on one install
# leaked onto all of them. Kept only for one-time migration (see
# ``_get_advertise_override``); new values are stored per-install under
# ``cluster_advertise_url:<instance_id>``.
CLUSTER_ADVERTISE_URL_SETTING = "cluster_advertise_url"

# Lifetime of a signed artifact-download redirect. Long enough for a browser to
# follow the 307, short enough that a leaked URL expires almost immediately.
ARTIFACT_TOKEN_TTL_SECONDS = 300


def get_cluster_secret() -> str:
    """Return the shared cluster secret.

    Priority: explicit ``CLUSTER_SECRET`` env var → else a value auto-generated
    once and persisted in ``system_settings`` (so every install on the shared
    database reads the same secret). Generation is race-safe across simultaneous
    boots via ``get_or_create_system_setting`` (INSERT … ON CONFLICT DO NOTHING).
    """
    if settings.cluster_secret and settings.cluster_secret.strip():
        return settings.cluster_secret.strip()
    return database.get_or_create_system_setting(
        CLUSTER_SECRET_SETTING,
        lambda: secrets.token_urlsafe(32),
    )


def regenerate_cluster_secret() -> str:
    """Rotate the auto-generated cluster secret (admin action).

    No-op semantics for an env-pinned secret: if ``CLUSTER_SECRET`` is set we
    cannot override it from the DB, so we just return the effective value.
    """
    if settings.cluster_secret and settings.cluster_secret.strip():
        return settings.cluster_secret.strip()
    new_secret = secrets.token_urlsafe(32)
    database.set_system_setting(CLUSTER_SECRET_SETTING, new_secret)
    return new_secret


def verify_cluster_secret(presented: Optional[str]) -> bool:
    """Constant-time comparison of a presented bearer against the cluster secret."""
    if not presented:
        return False
    return hmac.compare_digest(presented, get_cluster_secret())


def mint_artifact_token(hash: str) -> tuple[int, str]:
    """Sign a cross-install artifact-download redirect with the cluster secret.

    The session cookie is host-only, so a browser following a 307 to the holder
    install sends no credentials; this ``(exp, sig)`` pair travels in the
    redirect URL and lets the holder authorize that otherwise-cookieless request.
    """
    exp = int(time.time()) + ARTIFACT_TOKEN_TTL_SECONDS
    sig = hmac.new(
        get_cluster_secret().encode(),
        f"artifact-download:{hash}:{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return exp, sig


def verify_artifact_token(hash: str, exp: str, sig: str) -> bool:
    """Constant-time check of a signed artifact-download redirect (never raises).

    Returns False for anything malformed, expired, or signed for a different
    hash — the caller falls back to normal per-user auth.
    """
    if not isinstance(exp, str) or not exp.isdigit():
        return False
    if not isinstance(sig, str) or not sig:
        return False
    if int(exp) < time.time():
        return False
    expected = hmac.new(
        get_cluster_secret().encode(),
        f"artifact-download:{hash}:{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def require_cluster_auth(request: Request) -> str:
    """FastAPI dependency: authenticate a peer install via the cluster secret.

    Distinct from ``require_auth`` (per-API-user): peer endpoints are
    install-to-install, authenticated by the single shared secret. Returns the
    calling install's instance_id (from the ``X-Install-Id`` header) when known,
    for logging/diagnostics; access does not depend on it.
    """
    from app.api.auth import get_api_key

    presented = get_api_key(request)
    if not verify_cluster_secret(presented):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing cluster secret.",
        )
    return request.headers.get("X-Install-Id", "")


def cluster_headers() -> dict:
    """Auth headers an install presents when calling a peer's cluster API."""
    return {
        "Authorization": f"Bearer {get_cluster_secret()}",
        "X-Install-Id": settings.effective_instance_id,
    }


def _primary_lan_ip() -> Optional[str]:
    """The host's primary outbound LAN IP, or None.

    Opens a UDP socket "connected" to a public address — no packets are actually
    sent — and reads back the local address the OS would route through. This is
    the interface a peer on the same LAN is most likely able to reach, and it
    sidesteps the unresolvable-hostname trap (``getfqdn()`` can return a name
    that fails DNS on peers with ``[Errno -2]``).
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return None
    return ip


def _guess_advertise_url() -> Optional[str]:
    """Best-effort peer-reachable URL when none is configured.

    Prefers the primary LAN IP (routable from peers on the same network) over
    the host name, since a guessed FQDN often fails DNS on peers (``[Errno -2]``).
    Falls back to the host name only when no usable IP is found. A wrong guess
    simply surfaces later as an unreachable peer — recoverable by setting
    ``CLUSTER_ADVERTISE_URL`` or the advertise-URL override.
    """
    ip = _primary_lan_ip()
    if ip:
        return f"http://{ip}:{settings.port}"
    try:
        host = socket.getfqdn() or socket.gethostname()
    except OSError:
        host = None
    if not host or host in ("localhost", "localhost.localdomain"):
        return None
    return f"http://{host}:{settings.port}"


def _advertise_override_key(instance_id: Optional[str] = None) -> str:
    """DB key for *this* install's advertise-URL override.

    Scoped by ``instance_id`` so each install on the shared DB keeps its own
    override instead of clobbering a single global key (the old bug: an address
    entered on one install showed up as every install's address).
    """
    return f"{CLUSTER_ADVERTISE_URL_SETTING}:{instance_id or settings.effective_instance_id}"


def _get_advertise_override() -> Optional[str]:
    """Return this install's stored advertise-URL override (None when unset).

    Reads the per-install key. The first time a freshly-upgraded install finds
    no per-install value, it performs a one-time migration of the legacy global
    key — but *only* if this install is the one that originally published it
    (its registry ``base_url`` matches the legacy value). The owner claims the
    value into its per-install key and deletes the global key so it stops
    leaking onto peers; every other install simply ignores the stale global key.
    """
    key = _advertise_override_key()
    stored = database.get_system_setting(key)
    if stored is not None:
        # Explicitly set (possibly to "" meaning "cleared"); honour it verbatim.
        return stored.strip() or None

    legacy = (database.get_system_setting(CLUSTER_ADVERTISE_URL_SETTING) or "").strip()
    if not legacy:
        return None
    # Claim the legacy value only for the install that actually published it.
    try:
        me = database.get_install(settings.effective_instance_id)
    except Exception:  # noqa: BLE001 — never let migration block resolution
        me = None
    owner = me is not None and (me.get("base_url") or "").rstrip("/") == legacy.rstrip("/")
    if not owner:
        return None
    database.set_system_setting(key, legacy)
    database.delete_system_setting(CLUSTER_ADVERTISE_URL_SETTING)
    return legacy


def advertise_url() -> tuple[Optional[str], str]:
    """Resolve this install's peer-reachable base URL and where it came from.

    Returns ``(url, source)`` where source is ``"config"`` (env or admin
    override), ``"guess"`` (host-name fallback), or ``"none"``. A NULL url means
    this install is view-only: its sessions are visible/readable cluster-wide
    but cannot be controlled remotely.
    """
    explicit = (settings.cluster_advertise_url or "").strip()
    if not explicit:
        explicit = (_get_advertise_override() or "").strip()
    if explicit:
        return explicit.rstrip("/"), "config"
    guess = _guess_advertise_url()
    if guess:
        return guess.rstrip("/"), "guess"
    return None, "none"


def advertise_status() -> dict:
    """Snapshot of this install's advertised address for the Devices UI.

    ``env_pinned`` means ``CLUSTER_ADVERTISE_URL`` is set, so the admin override
    is ignored; ``override`` echoes the stored override (None when unset).
    """
    url, source = advertise_url()
    env_pinned = bool((settings.cluster_advertise_url or "").strip())
    override = _get_advertise_override()
    return {
        "url": url,
        "source": source,
        "env_pinned": env_pinned,
        "override": override,
        "instance_id": settings.effective_instance_id,
    }


def set_advertise_url_override(value: Optional[str]) -> dict:
    """Set (or clear, when blank) the admin advertise-URL override.

    Re-registers this install immediately so the new ``base_url`` is published to
    the registry without waiting for a restart. No-op semantics when env-pinned:
    the override is still stored but ``advertise_url()`` keeps preferring the env.
    """
    cleaned = (value or "").strip().rstrip("/")
    database.set_system_setting(_advertise_override_key(), cleaned)
    register_self()
    return advertise_status()


def default_device_name() -> str:
    """Human-friendly default name for this install (host name, else instance id)."""
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    return name or settings.effective_instance_id


def _build_capabilities(source: str) -> dict:
    """Registry ``capabilities`` blob: the advertise-URL source (existing key)
    merged with this runner's detected placement capabilities (V2 Phase 4).

    Detection is best-effort and never raises (``runner_capabilities``), so a
    detection miss degrades to the base blob and never blocks registration or a
    heartbeat.
    """
    caps: dict = {"advertise_url_source": source}
    try:
        from app.core.runner_capabilities import detect_capabilities
        caps.update(detect_capabilities())
    except Exception:  # noqa: BLE001 — capability detection must never block startup
        logger.debug("Capability detection failed; registering base caps only", exc_info=True)
    return caps


def _warn_if_stray_instance() -> None:
    """Warn when an UNREGISTERED instance boots against an already-populated
    registry — best-effort, never raises, never blocks registration.

    The classic footgun: an ad-hoc dev/debug instance started with a fresh
    instance_id but the PRODUCTION ``DATABASE_URL``. It is not just a viewer —
    its background workers act on SHARED credentials (the Codex token-refresh
    worker rotates the refresh token on a provider row every install sees), so
    the operator should know before it does anything.
    """
    try:
        me = database.get_install(settings.effective_instance_id)
        if me is not None:
            return  # known install — nothing unusual about this boot
        peers = database.get_installs()
        if not peers:
            return  # first install on a fresh database
        logger.warning(
            "instance_id %s is not yet registered in a shared database that "
            "already has %d install(s) — if this is an ad-hoc dev/debug instance "
            "pointed at a production DATABASE_URL, its background workers (Codex "
            "token refresh, etc.) will act on shared credentials.",
            settings.effective_instance_id,
            len(peers),
        )
    except Exception:  # noqa: BLE001 — a diagnostic must never block startup
        logger.debug("Stray-instance check failed", exc_info=True)


def register_self() -> bool:
    """Self-register and return whether this runner is approved to execute.

    The first install on a fresh database is trusted automatically. Any new
    identity joining an existing registry stays quarantined until an admin
    approves it from the Devices panel. Restarts preserve the existing state.
    """
    url, source = advertise_url()
    _warn_if_stray_instance()
    try:
        existing = database.get_install(settings.effective_instance_id)
        approve_new = existing is None and not database.get_installs()
        row = database.register_install(
            install_id=settings.effective_instance_id,
            device_name=default_device_name(),
            base_url=url,
            platform=("docker" if not settings.is_local_mode() else "local"),
            version=settings.version,
            capabilities=_build_capabilities(source),
            approve_new=approve_new,
        )
        approved = bool(row and row.get("approved"))
        logger.info(
            "Registered install %s in cluster registry (base_url=%s, source=%s, approved=%s)",
            settings.effective_instance_id,
            url or "<none/view-only>",
            source,
            approved,
        )
        if not approved:
            logger.warning(
                "Install %s is quarantined. Approve it from Settings > Devices "
                "on an existing admin device, then restart this install.",
                settings.effective_instance_id,
            )
        return approved
    except Exception:  # noqa: BLE001 — registration must never block startup
        logger.exception("Failed to register install in cluster registry")
        return False


def heartbeat_self() -> None:
    """Bump this install's last_seen AND refresh its capabilities (called
    periodically by the heartbeat task).

    Re-detecting on every beat means a runner that gains/loses Chrome, a
    display, or a Claude account is reflected in the registry within one
    heartbeat window (OD-8). Only last_seen + capabilities are touched;
    device_name/base_url are left to ``register_self`` so a user rename survives.
    """
    try:
        _, source = advertise_url()
        database.touch_install_heartbeat(
            settings.effective_instance_id, _build_capabilities(source)
        )
    except Exception:  # noqa: BLE001
        logger.debug("Cluster heartbeat failed", exc_info=True)
