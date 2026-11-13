"""Which network members can run a capability-gated ``model_providers`` row.

One rule, used everywhere (#1519): ``model_providers`` rows sync to every
member, so a row type an older build cannot run is gated by a capability the
worker reports in its signed presence. A member counts as capable only when
its presence reports that capability as ``1``. A member that reports nothing
(an older build, or presence not seen yet) counts as NOT capable. Membership
defines the set, not online state, because an offline member syncs the row
later. Revoked members cannot sync and are ignored. This worker always
counts as capable: it runs this build.

Nothing here is cached as the answer. Every call reads current membership
and presence, so a member admitted after a gated row exists, or one whose
presence changes, is judged again the next time anyone asks:

* creating or converting a gated row (``app/api/model_providers.py``,
  ``app/api/antigravity.py``) refuses while any member is missing;
* the provider list reports, per row, which workers need an update;
* anything that picks a worker to run a session or turn on a gated row
  (hand-off, remote StartSession, remote Task, a forwarded turn) refuses
  that worker with :func:`refusal`, instead of failing later on it.

:func:`recheck` is called when membership changes (admission, a synced
membership record) and when presence arrives, and logs which gated rows a
member gained or lost, so the change is visible in the worker's log too.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)


def capability_for(provider_type: Optional[str]) -> Optional[str]:
    """The presence capability a row of ``provider_type`` needs, or None (ungated)."""
    from app.core import antigravity, gemini_provider, native_providers

    ptype = (provider_type or "").strip().lower()
    if ptype in native_providers.PROVIDERS:
        return native_providers.CAPABILITY
    if ptype == gemini_provider.PROVIDER:
        return gemini_provider.CAPABILITY
    if ptype == antigravity.PROVIDER_TYPE:
        return antigravity.CAPABILITY
    return None


def _reports(presence: Optional[Mapping[str, Any]], capability: str) -> bool:
    return ((presence or {}).get("capabilities") or {}).get(capability) == 1


def _member_name(member: Mapping[str, Any]) -> str:
    return member.get("name") or member["device_id"]


def members_lacking(capability: str) -> List[Dict[str, str]]:
    """Non-revoked members other than this worker that do not report ``capability``.

    ``[{"device_id", "name"}]``; empty outside a network.
    """
    from app.db import network as store
    from app.network import engine

    ctx = engine.context()
    if ctx is None:
        return []
    presence = engine.all_presence()
    return [{"device_id": m["device_id"], "name": _member_name(m)}
            for m in store.list_members(include_revoked=False)
            if m["device_id"] != ctx.device_id
            and not _reports(presence.get(m["device_id"]), capability)]


def workers_requiring_update(capability: str) -> List[str]:
    """Names of the members that cannot run a row gated by ``capability`` yet."""
    return [m["name"] for m in members_lacking(capability)]


def workers_requiring_update_for_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """``{row id: [worker names]}`` for the gated rows some member cannot run.

    Reads membership and presence once for the whole list.
    """
    by_capability: Dict[str, List[str]] = {}
    out: Dict[str, List[str]] = {}
    for row in rows:
        capability = capability_for(row.get("provider_type"))
        if capability is None:
            continue
        if capability not in by_capability:
            by_capability[capability] = workers_requiring_update(capability)
        if by_capability[capability]:
            out[row["id"]] = by_capability[capability]
    return out


def needs_update_message(names: List[str], what: str = "this") -> str:
    """'Worker X needs an update to use this.' (plural: 'X and Y need …')."""
    if len(names) == 1:
        return f"{names[0]} needs an update to use {what}."
    return f"{', '.join(names[:-1])} and {names[-1]} need an update to use {what}."


def _other_worker(device_id: Optional[str]) -> Optional[str]:
    """``device_id`` as a member device id (a legacy instance id is resolved),
    or None outside a network, for no target and for this worker."""
    if not device_id:
        return None
    from app.network import engine, runners

    ctx = engine.context()
    if ctx is None:
        return None
    device_id = runners.resolve_runner_id(device_id) or device_id
    return None if device_id == ctx.device_id else device_id


def refusal(device_id: Optional[str], provider_id: Optional[str] = None, *,
            row: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Why ``device_id`` must not run ``provider_id`` (or ``row``), or None.

    None when there is no target, the target is this worker, the row is
    unknown or ungated, or the target reports the row's capability.
    ``device_id`` may be a device id or a legacy instance id.
    """
    if row is None and not provider_id:
        return None
    device_id = _other_worker(device_id)
    if device_id is None:
        return None
    from app.network import engine

    if row is None:
        from app.db import model_providers as provider_db

        row = provider_db.get_model_provider(provider_id)
        if row is None:
            return None
    capability = capability_for(row.get("provider_type"))
    if capability is None or _reports(engine.presence_of(device_id), capability):
        return None
    from app.db import network as store

    member = store.get_member(device_id)
    name = _member_name(member) if member else device_id
    label = row.get("name") or row.get("provider_type") or "this provider"
    return needs_update_message([name], label)


def provider_of_model(value: Any) -> Optional[str]:
    """The provider row id a ``prov:<id>:<model>`` model value names, or None."""
    from app.core.query_options import decode_provider_model

    decoded = decode_provider_model(value)
    return decoded[0] if decoded else None


def session_refusal(device_id: Optional[str], session_id: Optional[str],
                    overrides: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Why ``device_id`` must not be sent work that can run a turn of
    ``session_id`` (with ``overrides``), or None: :func:`picks_refusal` over
    the session's provider, model and launched model and the turn's
    ``provider`` and ``model``. Reads nothing for this worker or outside a
    network. Blocking: call off the event loop."""
    if _other_worker(device_id) is None:
        return None
    session: Mapping[str, Any] = {}
    if session_id:
        from app.db import sessions

        session = sessions.get_session(session_id) or {}
    overrides = overrides or {}
    return picks_refusal(device_id, [session.get("provider_id"), session.get("model"),
                                     session.get("launched_model"), overrides.get("provider"),
                                     overrides.get("model")],
                         profile_id=session.get("profile_id"))


def picks_refusal(device_id: Optional[str], picks: Iterable[Any], *,
                  profile_id: Optional[str] = None) -> Optional[str]:
    """Why ``device_id`` must not run work that names ``picks`` (provider row
    ids and model values), or None.

    Conservative on purpose: the worker refused is an older build, whose own
    binding rule differs (v1.2.1 guesses the provider from the model name),
    so nothing here predicts which row a turn would bind. Refused when ANY
    pick names a gated row that worker cannot run: the row id, a ``prov:``
    value, or a bare model id the row lists. With no pick at all, the
    agent's (``profile_id``) provider and model and the default run model
    count instead. Refusing too much costs an update of that worker;
    refusing too little runs gated work on it. Never refuses this worker,
    outside a network, or with no gated row. Blocking.
    """
    if _other_worker(device_id) is None:
        return None
    from app.db import model_providers as provider_db

    gated = [r for r in provider_db.get_all_model_providers() if capability_for(r.get("provider_type"))]
    if not gated:
        return None
    picks = list(picks)
    if not any(picks):
        picks = _unbound_picks(profile_id)
    values = {v for v in picks if isinstance(v, str) and v}
    named = {provider_of_model(v) or v for v in values}
    from app.core.provider_agent import curated_model_ids

    for row in gated:
        if row["id"] in named or values & set(curated_model_ids(row)):
            reason = refusal(device_id, row=row)
            if reason:
                return reason
    return None


def _unbound_picks(profile_id: Optional[str]) -> List[Optional[str]]:
    """What work bound to nothing may still run on: its agent's provider and
    model and the default run model (both ways it is picked)."""
    from app.core.credential_gate import default_run_model
    from app.db import database

    profile = database.get_profile(profile_id) if profile_id else None
    config = (profile or {}).get("config") or {}
    picks: List[Optional[str]] = [config.get("provider"), config.get("model")]
    for most_used in (True, False):
        picks.extend(default_run_model(probe=False, prefer_most_used=most_used))
    return picks


def automation_providers(automation: Mapping[str, Any]) -> List[str]:
    """The provider rows ``automation``'s runs use: its prompt's and each
    ``send_prompt`` step's, as a run resolves them
    (``automation_service.resolve_automation_model``). Blocking."""
    from app.core.automation_service import resolve_automation_model

    picks = []
    if (automation.get("prompt") or "").strip():
        picks.append(resolve_automation_model(dict(automation)))
    for step in automation.get("action_steps") or []:
        if isinstance(step, dict) and step.get("type") == "send_prompt":
            picks.append(resolve_automation_model(dict(automation), step))
    return list(dict.fromkeys(p["provider_id"] for p in picks if p.get("provider_id")))


def automation_refusal(device_id: Optional[str], automation: Mapping[str, Any], *,
                       besides: Iterable[str] = ()) -> Optional[str]:
    """Why ``device_id`` must not run ``automation``, or None. ``besides``:
    rows not judged (an edit judges only the rows it adds). Blocking."""
    if _other_worker(device_id) is None:
        return None
    skip = set(besides)
    for provider_id in automation_providers(automation):
        reason = None if provider_id in skip else refusal(device_id, provider_id)
        if reason:
            return reason
    return None


# ---------------------------------------------------------------------------
# Re-check on membership and presence changes
# ---------------------------------------------------------------------------

_last_gaps: Dict[str, frozenset] = {}
_last_lock = threading.Lock()


def recheck(device_ids: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
    """Judge the gated rows again for ``device_ids`` (all members when None).

    Returns ``{device_id: [row ids it cannot run]}`` for members with a gap
    and logs each change since the last check. Never raises.
    """
    try:
        return _recheck(device_ids)
    except Exception:  # noqa: BLE001 — a log aid; readers judge live anyway
        logger.debug("Provider capability re-check failed", exc_info=True)
        return {}


def _recheck(device_ids: Optional[Iterable[str]]) -> Dict[str, List[str]]:
    from app.db import model_providers as provider_db
    from app.db import network as store
    from app.network import engine

    ctx = engine.context()
    if ctx is None:
        return {}
    rows = [r for r in provider_db.get_all_model_providers() if capability_for(r.get("provider_type"))]
    members = {m["device_id"]: m for m in store.list_members(include_revoked=False)
               if m["device_id"] != ctx.device_id}
    presence = engine.all_presence()
    gaps: Dict[str, List[str]] = {}
    changed = False
    with _last_lock:
        # A full re-check also visits the gaps already recorded, so a member
        # that was revoked or left loses its gap (and open pages refresh).
        wanted = (set(members) | set(_last_gaps) if device_ids is None
                  else {d for d in device_ids if d})
        for device_id in wanted:
            member = members.get(device_id)
            missing = frozenset(
                r["id"] for r in rows
                if member is not None
                and not _reports(presence.get(device_id), capability_for(r["provider_type"])))
            before = _last_gaps.get(device_id, frozenset())
            changed = changed or missing != before
            if missing:
                gaps[device_id] = sorted(missing)
                _last_gaps[device_id] = missing
            else:
                _last_gaps.pop(device_id, None)
            if missing - before:
                names = [r.get("name") or r["id"] for r in rows if r["id"] in missing - before]
                logger.warning("%s needs an update to use these providers: %s",
                               _member_name(member), ", ".join(names))
            if before and not missing and member is not None:
                logger.info("%s can now use every provider on this network", _member_name(member))
    if changed:
        _refresh_open_pages()
    return gaps


def _refresh_open_pages() -> None:
    """An open Providers page refetches, so its note appears or clears now.

    Local only: every worker runs its own re-check when the change syncs to it.
    """
    try:
        from app.core.global_events import global_events
        global_events.resources_changed("settings", relay=False)
    except Exception:  # noqa: BLE001 — the next page load shows it anyway
        logger.debug("Could not tell open pages about a provider gap", exc_info=True)


def _reset_for_tests() -> None:
    with _last_lock:
        _last_gaps.clear()
