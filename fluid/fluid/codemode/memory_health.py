"""Memory health: model-free checks and safe repairs over one fact store.

The host runs :func:`run` on a schedule and after curation. It reads the
store (facts, archive, loss counters, meta.json, the journal and the pass
log), repairs what is safe to repair, and records the outcome in
``memory-health.json`` beside the store. That file is local to this worker
and never replicated.

Repairs only retire unprotected facts into the archive, where Recall still
finds them and a later save brings them back. They never touch a fact the
user asked to keep, and never decide what a fact means: exact duplicates
and unconfirmed harvests nobody used are mechanical; near-duplicates are
only reported.

Every signal is ``healthy`` or ``attention`` with one plain sentence. The
thresholds below are first estimates, not tuned values.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fluid.codemode import facts
from fluid.codemode.continual import MIN_NEW_ENTRIES
from fluid.codemode.skills import MAX_MEMORIES, atomic_write_text

HEALTH_FILE = "memory-health.json"
DAY = 86400.0

# Learner: activity in this window with no fact written for LEARNER_QUIET.
ACTIVITY_WINDOW = 7 * DAY
ACTIVE_MIN_USER_ENTRIES = 5
LEARNER_QUIET = 14 * DAY
FAILED_PASS_SHARE = 0.5
MIN_PASSES_TO_JUDGE = 4
# Usage: share of settled facts (older than USAGE_GRACE) never recalled and
# not in any current snapshot.
USAGE_GRACE = 7 * DAY
USAGE_MIN_FACTS = 20
UNUSED_SHARE = 0.5
# Harvested (tool-provenance) facts.
CHURN_MIN_REVISIONS = 10
CHURN_PER_WEEK = 3.0
# A fact not rewritten for this long has stopped churning (#1814: two legacy
# shared environment facts kept the signal raised long after their rewrites
# stopped, because lifetime revisions only fall below the rate with age).
CHURN_QUIET = 7 * DAY
UNCONFIRMED_PILE = 15
STALE_UNCONFIRMED_AGE = 30 * DAY
STALE_UNCONFIRMED_QUIET = 14 * DAY
# Near-duplicates: shared content words.
NEAR_DUPLICATE_SIMILARITY = 0.8
NEAR_DUPLICATE_MIN_WORDS = 4
NEAR_DUPLICATE_GROUPS = 5
# Capacity.
FULL_SHARE = 0.9
DOCUMENT_FULL_SHARE = 0.8
# Snapshot: bounded by design, Recall finds the rest. Leaving facts out is
# reported, not counted as needing attention (#1814); a snapshot that shows
# nothing although facts are in scope is.
# Curation and protected facts.
CURATION_WINDOW = 14 * DAY
CURATION_FAILED_STREAK = 3
PROTECTED_WINDOW = 14 * DAY
# Saves.
SAVE_WINDOW = 7 * DAY
# State retention.
REPAIR_KEEP = 30 * DAY
RENOTIFY_AFTER = DAY

LABELS = {
    "learner": "Learning",
    "usage": "Facts in use",
    "unconfirmed": "Unconfirmed facts",
    "duplicates": "Duplicates",
    "capacity": "Capacity",
    "snapshot": "Session snapshot",
    "curation": "Curation",
    "protected": "Your saved facts",
    "saves": "Saving",
}

_WORD = re.compile(r"[a-z0-9]{4,}")


def _signal(sid: str, attention: bool, reason: str, **detail) -> dict:
    return {"id": sid, "label": LABELS[sid], "state": "attention" if attention else "healthy",
            "reason": reason, "detail": detail}


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def load_state(store) -> dict:
    """The last recorded health state, or {} when none (or unreadable)."""
    try:
        doc = json.loads((Path(store.root) / HEALTH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_state(store, state: dict) -> None:
    atomic_write_text(Path(store.root) / HEALTH_FILE, json.dumps(state, ensure_ascii=False, indent=1))


def _log_rows(store, since: float) -> list[dict]:
    """Learner pass rows newer than ``since`` (the log is already bounded)."""
    rows = []
    try:
        lines = (Path(store.root) / ".synthesis-log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and float(row.get("ts") or 0) >= since:
            rows.append(row)
    return rows


def _oldest_log_ts(store) -> float | None:
    rows = _log_rows(store, 0.0)
    stamps = [float(r.get("ts") or 0) for r in rows if r.get("ts")]
    return min(stamps) if stamps else None


def _counts_as_failure(row: dict) -> bool | None:
    """True for a failed pass, False for one that ran, None for neither
    (a pass skipped for a held lock or a spent daily budget)."""
    error = str(row.get("error") or "")
    if error.startswith("skipped:") or error.startswith("InferenceBudgetExhausted"):
        return None
    if error and not row.get("consumed"):
        return True
    return False


def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall((text or "").lower()))


def _scopes(entries: list[dict]) -> list[str]:
    scopes = sorted({e.get("scope", "global") for e in entries} - {"global"})
    return scopes or ["global"]


def _snapshot_plans(store, entries) -> dict[str, dict]:
    """Snapshot plan per scope a session can see: each project scope (with
    global facts), or global alone when no fact is project-scoped."""
    models = [store._facts._entry(e) for e in entries]
    return {scope: store.memory_snapshot_plan(scope, entries=models) for scope in _scopes(entries)}


def _exact_duplicate_groups(entries: list[dict]) -> list[list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        if not e.get("conflicted"):
            groups.setdefault((e.get("scope", "global"), facts.fingerprint(e["text"])), []).append(e)
    return [g for g in groups.values() if len(g) > 1]


def _near_duplicate_groups(entries: list[dict]) -> list[list[str]]:
    """Clusters of different facts in one scope that share most content
    words. Reported only: whether they mean the same thing is a judgment."""
    items = [(e["id"], e.get("scope", "global"), _words(e["text"])) for e in entries
             if not e.get("conflicted")]
    items = [i for i in items if len(i[2]) >= NEAR_DUPLICATE_MIN_WORDS]
    parent = {i[0]: i[0] for i in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for n, (a, scope_a, words_a) in enumerate(items):
        for b, scope_b, words_b in items[n + 1:]:
            if scope_a != scope_b:
                continue
            union = len(words_a | words_b)
            if union and len(words_a & words_b) / union >= NEAR_DUPLICATE_SIMILARITY:
                parent[find(a)] = find(b)
    clusters: dict[str, list[str]] = {}
    for item_id in parent:
        clusters.setdefault(find(item_id), []).append(item_id)
    return [sorted(c) for c in clusters.values() if len(c) > 1]


def _local(doc: dict, bucket: str = "entries") -> list[dict]:
    """Facts that apply on this worker. Another worker's environment facts
    replicate here but are judged and repaired only on their own worker."""
    return [e for e in doc[bucket] if facts.visible_on(e, doc.get("store_id"))]


def _keeper_order(e: dict):
    # Replicated fields only, so every worker keeps the same copy and the
    # retirements cannot cross (each side retiring the other's keeper).
    return (not e.get("protected"), not e.get("durable"), float(e.get("created_at") or 0), facts.global_id(e))


# ─── repairs ────────────────────────────────────────────────────────────────

def repair(store, *, now: float | None = None) -> list[dict]:
    """Safe, reversible repairs. Returns ``[{"kind", "count", "at", "ids"}]``
    for what changed. Every repair is a retirement into the archive."""
    now = time.time() if now is None else now
    done: list[dict] = []
    doc = store._facts.snapshot()
    merged = []
    for group in _exact_duplicate_groups(_local(doc)):
        _keeper, *rest = sorted(group, key=_keeper_order)
        for e in rest:
            if e.get("protected"):
                continue  # two user-saved copies stay; the user decides
            try:
                if store.retire_memory(e["id"], reason="duplicate", expected_revision=e.get("revision", 1)):
                    merged.append(e["id"])
            except ValueError:
                continue  # changed since it was read; the next run looks again
    if merged:
        done.append({"kind": "duplicates", "count": len(merged), "at": now, "ids": merged})
    doc = store._facts.snapshot()
    live = [e for e in _local(doc) if not e.get("conflicted")]
    shown = set()
    for plan in _snapshot_plans(store, live).values():
        shown.update(plan["shown_ids"])
    stale = []
    for e in live:
        if (e.get("provenance") == "tool" and not e.get("protected") and not e.get("durable")
                and not int(e.get("uses") or 0) and not float(e.get("last_recalled_at") or 0)
                and now - float(e.get("created_at") or now) >= STALE_UNCONFIRMED_AGE
                and now - float(e.get("updated_at") or now) >= STALE_UNCONFIRMED_QUIET
                and e["id"] not in shown):
            try:
                if store.retire_memory(e["id"], reason="unconfirmed", expected_revision=e.get("revision", 1)):
                    stale.append(e["id"])
            except ValueError:
                continue
    if stale:
        done.append({"kind": "unconfirmed", "count": len(stale), "at": now, "ids": stale})
    return done


def describe_repair(event: dict) -> str:
    n = int(event.get("count") or 0)
    if event.get("kind") == "duplicates":
        return f"Archived {_plural(n, 'exact duplicate')}."
    if event.get("kind") == "unconfirmed":
        return f"Archived {_plural(n, 'unconfirmed fact')} nobody used."
    return f"Repaired {n}."


# ─── signals ────────────────────────────────────────────────────────────────

def check(store, *, now: float | None = None, learning_enabled: bool = True,
          previous: dict | None = None) -> dict:
    """Compute every signal. Read-only; the caller decides what to persist."""
    now = time.time() if now is None else now
    previous = previous or {}
    doc = store._facts.snapshot()
    live = _local(doc)
    plans = _snapshot_plans(store, [e for e in live if not e.get("conflicted")])
    signals = [
        _learner_signal(store, now, learning_enabled),
        _usage_signal(live, plans, now),
        _unconfirmed_signal(live, now),
        _duplicates_signal(live),
        # Capacity is the whole document, every worker's facts included.
        _capacity_signal(store, doc["entries"], doc["archived"]),
        _snapshot_signal(plans),
        _curation_signal(store, now),
    ]
    protected_signal, protected_ids, lost = _protected_signal(doc, previous, now)
    signals.append(protected_signal)
    signals.append(_saves_signal(store, now))
    attention = [s for s in signals if s["state"] == "attention"]
    return {
        "state": "attention" if attention else "healthy",
        "checked_at": now,
        "signals": signals,
        "protected_ids": protected_ids,
        "lost_protected": lost,
    }


def _learner_signal(store, now, learning_enabled) -> dict:
    if not learning_enabled:
        return _signal("learner", False, "Background learning is off.")
    try:
        recent_user = sum(
            1 for e in store.journal_entries()
            if e.get("kind") in ("user", "remember") and float(e.get("ts") or 0) >= now - ACTIVITY_WINDOW
        )
        pending = max(0, store.journal_count() - store.synth_cursor())
    except Exception:  # noqa: BLE001 — an unreadable journal is not activity
        recent_user, pending = 0, 0
    outcomes = [_counts_as_failure(r) for r in _log_rows(store, now - ACTIVITY_WINDOW)
                if r.get("kind") == "synthesis"]
    outcomes = [o for o in outcomes if o is not None]
    passes, failed = len(outcomes), sum(outcomes)
    detail = {"recent_user_entries": recent_user, "pending_entries": pending,
              "passes_7d": passes, "failed_passes_7d": failed}
    if recent_user < ACTIVE_MIN_USER_ENTRIES and not passes:
        return _signal("learner", False, "Too little recent activity to judge.", **detail)
    if not passes and pending >= MIN_NEW_ENTRIES:
        return _signal("learner", True, "Learning has not run despite recent sessions.", **detail)
    if passes >= MIN_PASSES_TO_JUDGE and failed / passes >= FAILED_PASS_SHARE:
        return _signal("learner", True, f"{failed} of {passes} learning passes this week failed.", **detail)
    last_write = float(store.learner_yield().get("last_write_at") or 0)
    detail["last_write_at"] = last_write or None
    oldest = _oldest_log_ts(store)
    running_long_enough = oldest is not None and now - oldest >= LEARNER_QUIET
    if running_long_enough and now - last_write >= LEARNER_QUIET:
        return _signal("learner", True, "The learner saved no fact in 14 days of sessions.", **detail)
    if last_write and now - last_write < LEARNER_QUIET:
        return _signal("learner", False, "The learner is saving facts.", **detail)
    return _signal("learner", False, "The learner is running.", **detail)


def _usage_signal(live, plans, now) -> dict:
    shown = set()
    for plan in plans.values():
        shown.update(plan["shown_ids"])
    settled = [e for e in live if now - float(e.get("created_at") or now) >= USAGE_GRACE]
    unused = [e for e in settled if not int(e.get("uses") or 0) and e["id"] not in shown]
    detail = {"settled": len(settled), "never_used": len(unused)}
    if len(settled) < USAGE_MIN_FACTS:
        return _signal("usage", False, "Too few settled facts to judge.", **detail)
    if len(unused) / len(settled) >= UNUSED_SHARE:
        return _signal("usage", True,
                       f"{len(unused)} of {len(settled)} facts were never recalled or shown.", **detail)
    return _signal("usage", False, f"{len(settled) - len(unused)} of {len(settled)} facts are in use.", **detail)


def _unconfirmed_signal(live, now) -> dict:
    tool = [e for e in live if e.get("provenance") == "tool" and not e.get("protected")]
    churny = []
    for e in tool:
        revisions = int(e.get("revision") or 1)
        weeks = max(1.0, (now - float(e.get("created_at") or now)) / (7 * DAY))
        rewritten_lately = now - float(e.get("updated_at") or now) < CHURN_QUIET
        if (revisions >= CHURN_MIN_REVISIONS and (revisions - 1) / weeks >= CHURN_PER_WEEK
                and rewritten_lately):
            churny.append(e["id"])
    detail = {"unconfirmed": len(tool), "rewritten_often": len(churny),
              "max_revision": max((int(e.get("revision") or 1) for e in tool), default=0)}
    if churny:
        verb = "keeps" if len(churny) == 1 else "keep"
        return _signal("unconfirmed", True,
                       f"{_plural(len(churny), 'harvested fact')} {verb} getting rewritten.", **detail)
    if len(tool) >= UNCONFIRMED_PILE:
        return _signal("unconfirmed", True, f"{len(tool)} unconfirmed facts are piling up.", **detail)
    return _signal("unconfirmed", False, f"{_plural(len(tool), 'unconfirmed fact')}.", **detail)


def _duplicates_signal(live) -> dict:
    exact = _exact_duplicate_groups(live)
    near = _near_duplicate_groups(live)
    detail = {"exact_groups": len(exact), "near_groups": len(near)}
    if len(near) >= NEAR_DUPLICATE_GROUPS:
        return _signal("duplicates", True, f"{len(near)} groups of facts say nearly the same thing.", **detail)
    if exact:
        return _signal("duplicates", False, f"{_plural(len(exact), 'fact')} saved more than once.", **detail)
    return _signal("duplicates", False, "No duplicates to merge.", **detail)


def _capacity_signal(store, live, archived) -> dict:
    tiers = {name: sum(1 for e in live if facts.tier(e) == name) for name in ("explicit", "durable", "inferred")}
    archived_explicit = sum(1 for e in archived if e.get("protected"))
    try:
        size = (Path(store.root) / "memories.json").stat().st_size
    except OSError:
        size = 0
    user_saved = tiers["explicit"] + archived_explicit
    detail = {**tiers, "explicit_cap": facts.MAX_EXPLICIT, "durable_cap": facts.MAX_DURABLE,
              "inferred_cap": MAX_MEMORIES, "archived": len(archived),
              "archived_explicit": archived_explicit, "user_saved": user_saved,
              "bytes": size, "bytes_cap": facts.MAX_DOCUMENT_BYTES}
    if size >= DOCUMENT_FULL_SHARE * facts.MAX_DOCUMENT_BYTES:
        share = round(100 * size / facts.MAX_DOCUMENT_BYTES)
        return _signal("capacity", True, f"The memory file is {share}% full; saves fail at the limit.", **detail)
    if user_saved >= facts.MAX_EXPLICIT:
        return _signal("capacity", True,
                       f"Memory is full: {user_saved} user-saved facts; new ones are refused "
                       "until one is forgotten or merged.", **detail)
    if user_saved >= FULL_SHARE * facts.MAX_EXPLICIT:
        return _signal("capacity", True,
                       f"User-saved facts are at {user_saved} of {facts.MAX_EXPLICIT}; "
                       "at the limit new ones are refused.", **detail)
    return _signal("capacity", False, f"{_plural(len(live), 'live fact')}; room to spare.", **detail)


def _snapshot_signal(plans) -> dict:
    scopes = [{"scope": scope, "in_scope": p["in_scope"], "shown": len(p["shown_ids"]),
               "omitted": p["over_budget"]} for scope, p in plans.items()]
    worst = max(scopes, key=lambda s: (s["omitted"], s["in_scope"]), default=None)
    detail = {"scopes": scopes}
    if not worst or not worst["in_scope"]:
        return _signal("snapshot", False, "No facts to show yet.", **detail)
    empty = next((s for s in scopes if s["omitted"] and not s["shown"]), None)
    if empty:
        return _signal("snapshot", True,
                       f"Sessions see none of {empty['in_scope']} facts; none fit the snapshot.", **detail)
    if worst["omitted"]:
        where = "in one project" if worst["scope"] != "global" else "overall"
        return _signal("snapshot", False,
                       f"Sessions see {worst['shown']} of {worst['in_scope']} facts {where}; "
                       "Recall finds the rest.", **detail)
    return _signal("snapshot", False, f"Sessions see {worst['shown']} of {worst['in_scope']} facts.", **detail)


def _curation_signal(store, now) -> dict:
    rows = [r for r in _log_rows(store, now - CURATION_WINDOW) if r.get("kind") == "consolidation"]
    outcomes = [o for o in (_counts_as_failure(r) for r in rows) if o is not None]
    streak = 0
    for failed in reversed(outcomes):
        if not failed:
            break
        streak += 1
    detail = {"passes_14d": len(outcomes), "failed_14d": sum(outcomes), "failed_streak": streak}
    if streak >= CURATION_FAILED_STREAK:
        return _signal("curation", True, f"The last {streak} curation passes failed.", **detail)
    if not outcomes:
        return _signal("curation", False, "No curation pass in the last two weeks.", **detail)
    return _signal("curation", False, "Curation passes are succeeding.", **detail)


def _protected_signal(doc, previous, now) -> tuple[dict, list[str], list[dict]]:
    current = sorted({facts.global_id(e) for e in doc["entries"] + doc["archived"]
                      if e.get("protected") and e.get("origin")})
    before = previous.get("protected_ids")
    vanished = []
    if isinstance(before, list):
        forgotten = doc.get("forgotten", "")
        vanished = [gid for gid in set(before) - set(current)
                    if not facts.barrier_contains(forgotten, "id:" + gid)]
    lost = [e for e in previous.get("lost_protected", []) if isinstance(e, dict)
            and now - float(e.get("at") or 0) < PROTECTED_WINDOW]
    if vanished:
        lost.append({"at": now, "count": len(vanished)})
    losses = doc.get("losses") if isinstance(doc.get("losses"), dict) else {}
    dropped_at = float(losses.get("last_explicit_dropped_at") or 0)
    missing = sum(int(e.get("count") or 0) for e in lost)
    detail = {"kept": len(current), "missing_14d": missing,
              "dropped_for_space": int(losses.get("explicit_dropped") or 0)}
    if missing:
        signal = _signal("protected", True,
                         f"{_plural(missing, 'user-saved fact')} disappeared without a Forget.", **detail)
    elif dropped_at and now - dropped_at < PROTECTED_WINDOW:
        signal = _signal("protected", True,
                         f"{_plural(detail['dropped_for_space'], 'user-saved fact')} "
                         f"{'was' if detail['dropped_for_space'] == 1 else 'were'} dropped for space.", **detail)
    else:
        kept = ("No user-saved facts yet." if not current else "The one user-saved fact is kept."
                if len(current) == 1 else f"All {len(current)} user-saved facts are kept.")
        signal = _signal("protected", False, kept, **detail)
    return signal, current, lost


def _saves_signal(store, now) -> dict:
    record = store.memory_save_failures()
    recent = [t for t in record.get("storage_times", []) if isinstance(t, (int, float)) and now - t < SAVE_WINDOW]
    detail = {"failed_7d": len(recent), "rejected_total": int(record.get("rejected") or 0)}
    if recent:
        return _signal("saves", True, f"{_plural(len(recent), 'save')} failed to write this week.", **detail)
    return _signal("saves", False, "Saves are writing normally.", **detail)


# ─── the scheduled run ─────────────────────────────────────────────────────

def run(store, *, now: float | None = None, learning_enabled: bool = True) -> dict:
    """Repair, check and record. Returns the recorded state plus ``notify``:
    True once per stretch of ``attention`` (and at most once a day), so the
    host raises one notification, not one per check. Call
    :func:`mark_notified` after the notification is delivered."""
    now = time.time() if now is None else now
    previous = load_state(store)
    repairs = repair(store, now=now)
    result = check(store, now=now, learning_enabled=learning_enabled, previous=previous)
    kept = [r for r in previous.get("repairs", []) if isinstance(r, dict)
            and now - float(r.get("at") or 0) < REPAIR_KEEP]
    events = kept + [{k: v for k, v in r.items() if k != "ids"} for r in repairs]
    attention_since = previous.get("attention_since") if previous.get("state") == "attention" else None
    if result["state"] == "attention":
        attention_since = attention_since or now
    else:
        attention_since = None
    state = {
        "schema": 1,
        **result,
        "repairs": events[-50:],
        "attention_since": attention_since,
        "notified_for": previous.get("notified_for"),
        "notified_at": previous.get("notified_at"),
    }
    _save_state(store, state)
    notified_at = float(state.get("notified_at") or 0)
    state["notify"] = bool(
        attention_since and state.get("notified_for") != attention_since
        and now - notified_at >= RENOTIFY_AFTER
    )
    return state


def mark_notified(store, *, now: float | None = None) -> None:
    """Record that the current stretch of ``attention`` was announced."""
    now = time.time() if now is None else now
    state = load_state(store)
    if not state.get("attention_since"):
        return
    state.update(notified_for=state["attention_since"], notified_at=now)
    _save_state(store, state)


def summary(state: dict) -> dict:
    """The owner-facing view: overall state, the signals that need
    attention first, and recent repairs in plain words."""
    signals = [s for s in state.get("signals", []) if isinstance(s, dict)]
    signals.sort(key=lambda s: s.get("state") != "attention")
    repairs = [r for r in state.get("repairs", []) if isinstance(r, dict)]
    return {
        "state": state.get("state", "healthy"),
        "checked_at": state.get("checked_at"),
        "signals": [{k: s.get(k) for k in ("id", "label", "state", "reason", "detail")} for s in signals],
        "repairs": [{"kind": r.get("kind"), "count": r.get("count"), "at": r.get("at"),
                     "summary": describe_repair(r)} for r in sorted(repairs, key=lambda r: -(r.get("at") or 0))],
    }
