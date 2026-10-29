"""A dev instance pairs only with the other dev instance its launcher started.

``python -m scripts.devloop up --pair`` starts two disposable dev instances
and pairs them through the normal invite flow (:mod:`app.network.enrollment`).
A disposable preview (:func:`app.preview_run.disposable_preview_data`) must
never join, or admit, anything else: not the owner's real workers, not an
instance from another launcher. So on such an instance every network join is
refused unless both sides prove they hold the pair key one launcher gave both
of them (``SHUFFLE_DEV_PAIR_KEY``), and the joiner dials only the peer
addresses that launcher named (``SHUFFLE_DEV_PAIR_PEERS``):

* joiner: the endpoint must be one of its peers (checked before the code is
  sent anywhere), its join request carries ``HMAC(key, "join", code)``, and
  the inviter's answer must carry ``HMAC(key, "admit", code, join_id)``;
* inviter: a join without the right ``join`` proof is refused before the code
  is consumed, and its answer carries the ``admit`` proof.

A real worker (no launcher marker) is unaffected: it neither sends nor checks
a proof, and a dev instance never reaches it (its address is not a peer).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

KEY_ENV = "SHUFFLE_DEV_PAIR_KEY"
PEERS_ENV = "SHUFFLE_DEV_PAIR_PEERS"
#: The launcher's key is 32 random bytes as hex; anything shorter is no key.
MIN_KEY_CHARS = 32
REFUSED = ("This is a disposable dev instance: it pairs only with the other dev instance "
           "its own launcher started (python -m scripts.devloop up --pair)")


class DevPairRefused(RuntimeError):
    """A join a dev instance must not make or accept."""


def guarded() -> bool:
    """Whether this process is a launcher-marked disposable preview (dev instance)."""
    from app.preview_run import disposable_preview_data

    try:
        return disposable_preview_data()
    except Exception:  # noqa: BLE001 — a preview we cannot classify stays guarded
        return os.environ.get("AI_SHUFFLE_PREVIEW") == "1"


def in_pair() -> bool:
    """Whether this is a dev instance its launcher started as one of a pair.

    Its network can hold only the launcher's other instance (every join is
    refused otherwise), which runs the same checkout: a peer that knows a
    forwarded turn's caller, never an older worker or a real one.
    """
    return guarded() and _key() is not None


def _key() -> Optional[bytes]:
    value = (os.environ.get(KEY_ENV) or "").strip()
    return value.encode("utf-8") if len(value) >= MIN_KEY_CHARS else None


def _proof(key: bytes, purpose: str, *parts: str) -> str:
    message = "\0".join((purpose, *parts)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _require_key() -> bytes:
    key = _key()
    if key is None:
        raise DevPairRefused(REFUSED + "; this one was started without a pair")
    return key


def _canonical(endpoint: str) -> str:
    return (endpoint or "").strip().rstrip("/").lower()


def peers() -> list[str]:
    raw = os.environ.get(PEERS_ENV) or ""
    return [_canonical(part) for part in raw.split(",") if part.strip()]


def check_join_target(endpoint: str) -> None:
    """Joiner, before anything is sent: only the launcher's other instance."""
    if not guarded():
        return
    _require_key()
    if _canonical(endpoint) not in peers():
        raise DevPairRefused(f"{REFUSED}; {endpoint} is not that instance")


def join_proof(code: str) -> Optional[str]:
    """Joiner: the proof its join request carries (None on a real worker)."""
    if not guarded():
        return None
    return _proof(_require_key(), "join", code)


def check_admission(code: str, join_id: str, presented: Optional[str]) -> None:
    """Joiner, on the inviter's answer: it must hold the same launcher's key."""
    if not guarded():
        return
    expected = _proof(_require_key(), "admit", code, join_id)
    if not presented or not hmac.compare_digest(expected, str(presented)):
        raise DevPairRefused(f"{REFUSED}; the worker that answered is not that instance")


def check_join(code: str, presented: Optional[str]) -> None:
    """Inviter, before the code is consumed: the joiner holds this launcher's key."""
    if not guarded():
        return
    expected = _proof(_require_key(), "join", code)
    if not presented or not hmac.compare_digest(expected, str(presented)):
        raise DevPairRefused(f"{REFUSED}; the joining worker is not that instance")


def admission_proof(code: str, join_id: str) -> Optional[str]:
    """Inviter: the proof its answer carries (None on a real worker)."""
    if not guarded():
        return None
    return _proof(_require_key(), "admit", code, join_id)
