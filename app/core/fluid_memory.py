"""Shared user-memory routing for native, delegated and network consumers."""
from __future__ import annotations

import hashlib
from pathlib import Path


def skills_root() -> Path:
    from app.core.config import settings
    return Path(settings.fluid_skills_dir) if settings.fluid_skills_dir else settings.effective_data_dir / "fluid" / "skills"


def owner_root(owner: str) -> Path:
    if owner == "local":
        return skills_root()  # existing installation-owner store; never an API user's store
    if not owner.startswith("api:") or not owner[4:]:
        raise ValueError("Unknown memory owner")
    return skills_root() / ".users" / hashlib.sha256(owner.encode()).hexdigest()


def store_for_owner(owner: str):
    from fluid.codemode.skills import SkillStore
    return SkillStore(owner_root(owner), owner=owner)


def session_memory(session_id: str | None, cwd: str | None = None):
    from app.core.config import settings
    from fluid.codemode.skills import project_key_for
    if not settings.managed_database_available():
        # Standalone/isolated tests have no authenticated multi-user database.
        return store_for_owner("local"), project_key_for(cwd)
    from app.db.sessions import get_session, get_session_by_sdk_id
    if not session_id:
        raise ValueError("Cannot resolve memory ownership without a session")
    session = get_session(session_id) or get_session_by_sdk_id(session_id)
    if not session:
        raise ValueError("Memory session does not exist")
    owner = f"api:{session['api_user_id']}" if session.get("api_user_id") else "local"
    project = session.get("project_v2_id") or session.get("project_id")
    return store_for_owner(owner), f"project:{project}" if project else project_key_for(cwd)


def stores():
    """Existing stores only; no user-controlled path is accepted."""
    from fluid.codemode.skills import SkillStore
    import json
    root = skills_root()
    if (root / "memories.json").exists():
        yield store_for_owner("local")
    for path in (root / ".users").glob("*/memories.json"):
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))["owner"]
            if owner_root(owner) == path.parent:
                yield SkillStore(path.parent, owner=owner)
        except (OSError, ValueError, KeyError):
            continue


# Diagnostics are opt-in and bounded independently of journal retention. Do not
# instantiate SkillStore here: observing a missing store must not create it.
JOURNAL_DIAGNOSTIC_STORES = 32
JOURNAL_DIAGNOSTIC_BYTES = 8 * 1024 * 1024
JOURNAL_DIAGNOSTIC_META_BYTES = 256 * 1024


def journal_diagnostics() -> dict:
    """Content-free, best-effort coverage of this worker's configured stores.

    Count newline bytes, never parse journal entries or return paths/identities.
    Concurrent rewrites, caps and unreadable files are explicit, not zero loss.
    Process counters are separate from retained filesystem coverage.
    """
    import json
    import os
    import stat
    import time
    from fluid.codemode.skills import (
        JOURNAL_LOSS_EVENTS, JOURNAL_LOSS_FILE, JOURNAL_MAX_BYTES, LEARNER_SILENCE_ALARM_SECONDS,
        journal_metrics, learner_silence_seconds, read_journal_loss,
    )

    started = time.perf_counter()
    root = skills_root()
    candidates = [root]
    discovery_incomplete = False
    users = root / ".users"
    try:
        if users.is_symlink():
            discovery_incomplete = True
        elif users.exists():
            with os.scandir(users) as entries:
                for index, entry in enumerate(entries):
                    if index >= JOURNAL_DIAGNOSTIC_STORES - 1:
                        discovery_incomplete = True
                        break
                    if (len(entry.name) == 64 and all(c in "0123456789abcdef" for c in entry.name)
                            and entry.is_dir(follow_symlinks=False)):
                        candidates.append(Path(entry.path))
                    else:
                        discovery_incomplete = True
    except OSError:
        discovery_incomplete = True

    result = {
        "scope": "configured_worker_stores", "snapshot_atomic": False,
        "stores_examined": 0, "journals_found": 0, "journals_incomplete": 0,
        "discovery_incomplete": discovery_incomplete, "retained_bytes": 0,
        "retained_entries_known": 0, "consumed_entries_known": 0,
        "pending_entries_known": 0, "journals_at_byte_cap": 0,
        "bytes_scanned": 0, "scan_budget_bytes": JOURNAL_DIAGNOSTIC_BYTES,
        "per_journal_byte_cap": JOURNAL_MAX_BYTES,
        # Stores whose learner has run passes for the alarm window without
        # writing a fact (meta.json "learner_yield"; see Synthesizer._track_yield).
        "learner_silent_stores": 0,
        "learner_silence_alarm_seconds": LEARNER_SILENCE_ALARM_SECONDS,
    }
    # Persisted per-store loss records (journal_loss.json): counts and times
    # only, surviving restarts and shared by every worker over the store.
    history = {
        "scope": "persisted_per_store", "stores_with_history": 0,
        **{name: 0 for name in JOURNAL_LOSS_EVENTS},
        "first_at": None, "last_at": None, "last_quota_at": None,
    }
    result["loss_history"] = history

    def fold_history(directory):
        record = read_journal_loss(directory / JOURNAL_LOSS_FILE)
        if not record:
            return
        history["stores_with_history"] += 1
        for name in JOURNAL_LOSS_EVENTS:
            history[name] += record[name]
        for name, pick in (("first_at", min), ("last_at", max), ("last_quota_at", max)):
            if name in record:
                current = history[name]
                history[name] = record[name] if current is None else pick(current, record[name])

    def read_regular(path, limit):
        # Never follow links or block on FIFOs/devices placed in a store. The
        # fstat check also covers a replacement between lstat and open.
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("Not a regular file")
            data = stream.read(limit)
        after = path.stat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        return data, opened.st_size, identity(before) == identity(opened) == identity(after)

    for directory in candidates:
        result["stores_examined"] += 1
        try:
            if directory.is_symlink():
                raise ValueError("Linked store")
            fold_history(directory)
            budget = max(0, JOURNAL_DIAGNOSTIC_BYTES - result["bytes_scanned"])
            data, size, stable = read_regular(directory / "journal.jsonl", min(JOURNAL_MAX_BYTES, budget))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            result["journals_incomplete"] += 1
            continue
        result["journals_found"] += 1
        result["retained_bytes"] += size
        result["bytes_scanned"] += len(data)
        result["journals_at_byte_cap"] += int(size >= JOURNAL_MAX_BYTES)
        if not stable or len(data) != size:
            result["journals_incomplete"] += 1
            continue
        entries = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
        result["retained_entries_known"] += entries
        try:
            meta, meta_size, meta_stable = read_regular(directory / "meta.json", JOURNAL_DIAGNOSTIC_META_BYTES)
            if not meta_stable or len(meta) != meta_size:
                raise ValueError("Incomplete metadata")
            meta_doc = json.loads(meta)
            cursor = meta_doc.get("synth_cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("Invalid cursor")
            silence = learner_silence_seconds(meta_doc.get("learner_yield"), time.time())
            result["learner_silent_stores"] += int(silence >= LEARNER_SILENCE_ALARM_SECONDS)
        except FileNotFoundError:
            cursor = 0
        except (OSError, ValueError, AttributeError):
            result["journals_incomplete"] += 1
            continue
        consumed = min(entries, cursor)
        result["consumed_entries_known"] += consumed
        result["pending_entries_known"] += entries - consumed
    result["coverage_complete"] = not result["discovery_incomplete"] and result["journals_incomplete"] == 0
    result["process_events"] = journal_metrics()
    now = time.time()
    last_quota = history["last_quota_at"]
    history["last_quota_age_seconds"] = None if last_quota is None else max(0.0, now - last_quota)
    # Records are written at most once per warning window, so they can trail
    # the newest loss; "recent" therefore uses an hour, not the 60s window.
    history["saturation_observed_last_hour"] = last_quota is not None and now - last_quota < 3600
    result["collection_ms"] = (time.perf_counter() - started) * 1000
    return result


async def maintain_memories():
    """Finite maintenance batches, independent of chat or a learning model."""
    import asyncio
    import logging
    while True:
        try:
            def sweep():
                for store in stores():
                    try:
                        store.reap()
                        store.purge_forgotten_evidence()
                    except Exception:
                        logging.getLogger(__name__).exception("One memory store could not be maintained")
            await asyncio.to_thread(sweep)
        except Exception:
            logging.getLogger(__name__).exception("Memory maintenance failed")
        await asyncio.sleep(300)
