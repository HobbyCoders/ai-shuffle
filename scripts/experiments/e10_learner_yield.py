"""E10: memory learner yield report (design-final 6.1). Offline; spends nothing.

Reads, for a date range:

* the fact stores of every replica: the local ``memories.json`` under the
  app's fluid skills root, plus each worker's replicated snapshot in Postgres
  (``network_memory_records`` rows ``memory-v2:<device>:<owner>``). Each
  snapshot also carries the peers' facts, so facts are de-duplicated by global
  id and credited to the replica that wrote them (``origin``);
* the learner's pass logs (``.synthesis-log.jsonl``), one per replica, given
  with ``--log LABEL=PATH``. A row that carries ``store_id`` is credited to
  that replica whatever file it came from;
* session transcripts in ``fluid_session_nodes``, to count later citations of
  learner facts and to check whether a user message supports each Remember
  fact.

Postgres is found the way the app finds it: ``DATA_DIR/managed-postgres.json``
through ``app.db.base``. ``--no-db`` skips it and reads files only.

Reports per replica per day: facts written by class (learner, Remember,
environment harvest), learner passes run and passes that wrote, the longest
silent streak, how many learner facts were later cited, and how many Remember
facts a user message in their source session supports.

The citation and support checks are lexical heuristics (shared 5-word runs,
distinctive identifiers, content-word overlap); read them as rough counts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.experiments._common import ensure_import_paths, read_jsonl  # noqa: E402

ensure_import_paths()

DECIDES = "Whether the rollout learner earns L; D1"
SILENT_DAYS_ALARM = 3  # design-final #14: yield alarm at 3 silent days
SUPPORT_OVERLAP = 0.5
CUE_RE = re.compile(r"\b(remember|from now on|going forward|always|never|don't|do not|note that|keep in mind)\b",
                    re.IGNORECASE)
_REMINDER_RE = re.compile(r"<system-reminder>.*?(</system-reminder>|\Z)", re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")
_STOPWORDS = frozenset(
    "that this with from have will when then than they them their there what which while "
    "into onto over under about after before because being been does done each more most "
    "must only other same should some such very were your yours also just like make made "
    "used uses using user users file files code".split()
)


# ─── data model ──────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """One replica's fact store document (memories.json or an export)."""

    label: str
    store_id: str
    doc: dict[str, Any]


@dataclass
class Fact:
    gid: str
    origin: str
    local_id: str
    text: str
    kind: str  # learner | remember | harvest | other
    created_at: float | None
    key: str | None = None
    source_session: str | None = None
    tombstoned: bool = False
    uses: int = 0


@dataclass
class Sources:
    snapshots: list[Snapshot] = field(default_factory=list)
    logs: list[tuple[str, Path]] = field(default_factory=list)
    # (session_id, ts, role, message dict)
    nodes: list[tuple[str, float, str, dict[str, Any]]] = field(default_factory=list)


def classify(entry: dict[str, Any]) -> str:
    """By the stamps each write path leaves (verify/learner-yield): Remember
    writes protected, keyed facts with a source session; the runtime harvest
    writes tool facts keyed ``env.*``; the learner writes unkeyed, unprotected
    facts with no source session."""
    key = entry.get("key") or ""
    if entry.get("protected") and entry.get("source_session"):
        return "remember"
    if entry.get("provenance") == "tool" and str(key).startswith("env."):
        return "harvest"
    if not entry.get("protected") and not key and not entry.get("source_session"):
        return "learner"
    return "other"


def collect_facts(snapshots: Iterable[Snapshot]) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    for snap in snapshots:
        doc = snap.doc
        for bucket in ("entries", "archived"):
            for e in doc.get(bucket) or []:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                origin = e.get("origin") or snap.store_id
                local = str(e["id"]).split(":")[-1]
                gid = f"{origin}:{local}"
                fact = Fact(gid=gid, origin=origin, local_id=local, text=str(e.get("text") or ""),
                            kind=classify(e), created_at=e.get("created_at"), key=e.get("key"),
                            source_session=e.get("source_session"), uses=int(e.get("uses") or 0))
                old = facts.get(gid)
                if old is None or (old.created_at is None and fact.created_at is not None):
                    facts[gid] = fact
                elif old is not None:
                    old.uses = max(old.uses, fact.uses)
        for t in doc.get("tombstones") or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            origin = t.get("origin") or snap.store_id
            local = str(t["id"]).split(":")[-1]
            gid = f"{origin}:{local}"
            if gid not in facts:
                kind = "learner" if not t.get("key") and not t.get("source_session") else "other"
                facts[gid] = Fact(gid=gid, origin=origin, local_id=local, text="", kind=kind,
                                  created_at=None, key=t.get("key"),
                                  source_session=t.get("source_session"), tombstoned=True)
    return facts


# ─── text matching ───────────────────────────────────────────────────────────


def strip_reminders(text: str) -> str:
    """Drop ``<system-reminder>`` blocks, including one cut off before its
    closing tag: the app appends the memory snapshot to every user turn."""
    return _REMINDER_RE.sub(" ", text)


def tokens(text: str) -> list[str]:
    """Lowercase words and identifiers; sentence punctuation is trimmed off
    the ends, so ``aih.theme.`` and ``aih.theme`` match."""
    out = []
    for t in _TOKEN_RE.findall(text.lower()):
        t = t.strip(".:/-")
        if t:
            out.append(t)
    return out


def content_words(text: str) -> set[str]:
    return {t for t in tokens(text) if len(t) >= 4 and t not in _STOPWORDS and not t.isdigit()}


def _identifiers(text: str) -> set[str]:
    return {t for t in tokens(text) if len(t) >= 6 and re.search(r"[_./]|\d", t)
            and not t.replace(".", "").isdigit()}


def cites(fact_text: str, message_text: str, key: str | None = None) -> bool:
    """A later message cites the fact when it repeats a run of five words
    from it, one of its distinctive identifiers, or its key."""
    msg_tokens = tokens(message_text)
    if not msg_tokens:
        return False
    if key and key.lower() in message_text.lower():
        return True
    ft = tokens(fact_text)
    joined = " " + " ".join(msg_tokens) + " "
    for i in range(0, max(0, len(ft) - 4)):
        if " " + " ".join(ft[i:i + 5]) + " " in joined:
            return True
    ids = _identifiers(fact_text)
    return bool(ids and ids & set(msg_tokens))


def support(fact_text: str, user_text: str) -> tuple[float, bool]:
    """Share of the fact's content words the user message contains, and
    whether the message has an explicit memory cue."""
    words = content_words(fact_text)
    if not words:
        return 0.0, False
    have = content_words(user_text) | set(tokens(user_text))
    return len(words & have) / len(words), bool(CUE_RE.search(user_text))


def message_text(message: dict[str, Any], *, role: str) -> str:
    """Plain text of a stored node. For user nodes only typed text counts:
    tool results are not the user speaking, and reminders are stripped."""
    parts: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif role == "assistant" and kind == "tooluseblock":
            parts.append(json.dumps(block.get("input") or {}, ensure_ascii=False))
    text = "\n".join(parts)
    if role == "user":
        text = strip_reminders(text)
        if text.lstrip().startswith("<task-notification>"):
            return ""
    return text


# ─── report ──────────────────────────────────────────────────────────────────


def _day(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else None


def _wrote(changed: Iterable[str]) -> list[str]:
    return [c for c in changed or [] if isinstance(c, str) and c.startswith("memory:")]


def build_report(sources: Sources, *, since: float, until: float,
                 now: float | None = None) -> dict[str, Any]:
    labels = {s.store_id: s.label for s in sources.snapshots}

    def replica(store_id: str | None, fallback: str) -> str:
        return labels.get(store_id or "", store_id or fallback) if store_id else fallback

    facts = collect_facts(sources.snapshots)
    in_range = [f for f in facts.values() if f.created_at is not None and since <= f.created_at < until]

    written: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for f in in_range:
        written[replica(f.origin, f.origin)][_day(f.created_at)][f.kind] += 1
    tombstoned_learner = defaultdict(int)
    for f in facts.values():
        if f.tombstoned and f.kind == "learner":
            tombstoned_learner[replica(f.origin, f.origin)] += 1

    passes: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    timeline: dict[str, list[tuple[float, bool, str]]] = defaultdict(list)
    for label, path in sources.logs:
        for row in read_jsonl([path]):
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or not since <= ts < until:
                continue
            who = replica(row.get("store_id"), label)
            day = _day(ts)
            kind = str(row.get("kind") or "synthesis")
            wrote = _wrote(row.get("changed"))
            bucket = passes[who][day]
            bucket[f"{kind}_passes"] += 1
            bucket["passes"] += 1
            if wrote:
                bucket["passes_that_wrote"] += 1
                bucket["memory_writes"] += len(wrote)
            if row.get("error"):
                bucket["errors"] += 1
            timeline[who].append((ts, bool(wrote), kind))

    silence = {}
    for who, events in timeline.items():
        events.sort()
        streak = longest = 0
        last_write = None
        for ts, wrote, kind in events:
            if kind != "synthesis":
                continue
            if wrote:
                streak = 0
                last_write = ts
            else:
                streak += 1
                longest = max(longest, streak)
        end = min(until, now if now is not None else time.time())
        silent_days = max(0.0, (end - (last_write or since)) / 86400)
        silence[who] = {
            "longest_silent_synthesis_streak": longest,
            "last_write": _day(last_write) if last_write else None,
            "silent_days_at_end": round(silent_days, 1),
            "alarm": silent_days >= SILENT_DAYS_ALARM,
        }

    assistant_msgs = [(sid, ts, message_text(m, role="assistant"))
                      for sid, ts, role, m in sources.nodes if role == "assistant"]
    user_by_session: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for sid, ts, role, m in sources.nodes:
        if role == "user":
            text = message_text(m, role="user")
            if text.strip():
                user_by_session[sid].append((ts, text))

    def citations(fact: Fact) -> int:
        return sum(1 for _, ts, text in assistant_msgs
                   if ts > (fact.created_at or 0) and cites(fact.text, text, fact.key))

    cited: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cited_examples = []
    for f in in_range:
        if f.kind not in ("learner", "remember"):
            continue
        n = citations(f)
        who = replica(f.origin, f.origin)
        cited[who][f"{f.kind}_facts"] += 1
        if n:
            cited[who][f"{f.kind}_cited"] += 1
            cited_examples.append({"fact": f.gid, "kind": f.kind, "citations": n})

    remember = {"total": 0, "supported": 0, "cue_only": 0, "unsupported": 0, "session_missing": 0,
                "facts": []}
    for f in facts.values():
        if f.kind != "remember":
            continue
        remember["total"] += 1
        msgs = [text for ts, text in user_by_session.get(f.source_session or "", [])
                if f.created_at is None or ts <= f.created_at + 60]
        if not msgs:
            remember["session_missing"] += 1
            verdict = "session_missing"
        else:
            best = max((support(f.text, t) for t in msgs), key=lambda s: (s[0], s[1]))
            any_cue = any(support(f.text, t)[1] for t in msgs)
            if best[0] >= SUPPORT_OVERLAP:
                verdict = "supported"
            elif any_cue:
                verdict = "cue_only"
            else:
                verdict = "unsupported"
            remember[verdict] += 1
        remember["facts"].append({"fact": f.gid, "verdict": verdict})

    return {
        "decides": DECIDES,
        "range": {"since": _day(since), "until": _day(until - 1)},
        "replicas": sorted({s.label for s in sources.snapshots} | set(passes) | set(written)),
        "facts_written": {w: {d: dict(k) for d, k in days.items()} for w, days in written.items()},
        "learner_facts_tombstoned": dict(tombstoned_learner),
        "passes": {w: {d: dict(k) for d, k in days.items()} for w, days in passes.items()},
        "silence": silence,
        "citations": {w: dict(v) for w, v in cited.items()},
        "cited_facts": cited_examples,
        "remember_support": remember,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [f"E10 learner yield, {report['range']['since']} to {report['range']['until']}",
             f"Decides: {report['decides']}", ""]
    replicas = sorted(set(report["facts_written"]) | set(report["passes"]))
    for who in replicas:
        lines.append(f"Replica {who}:")
        days = sorted(set(report["facts_written"].get(who, {})) | set(report["passes"].get(who, {})))
        for day in days:
            w = report["facts_written"].get(who, {}).get(day, {})
            p = report["passes"].get(who, {}).get(day, {})
            lines.append(
                f"  {day}: learner facts {w.get('learner', 0)}, Remember {w.get('remember', 0)}, "
                f"harvest {w.get('harvest', 0)}; passes {p.get('passes', 0)} "
                f"(wrote {p.get('passes_that_wrote', 0)}, errors {p.get('errors', 0)})")
        s = report["silence"].get(who)
        if s:
            lines.append(f"  longest silent synthesis streak {s['longest_silent_synthesis_streak']}, "
                         f"last write {s['last_write']}, silent days at end {s['silent_days_at_end']}"
                         + ("  ALARM" if s["alarm"] else ""))
        c = report["citations"].get(who, {})
        if c:
            lines.append(f"  cited later: learner {c.get('learner_cited', 0)} of {c.get('learner_facts', 0)}, "
                         f"Remember {c.get('remember_cited', 0)} of {c.get('remember_facts', 0)}")
        t = report["learner_facts_tombstoned"].get(who)
        if t:
            lines.append(f"  learner facts since deleted (creation day unknown): {t}")
    r = report["remember_support"]
    lines += ["", f"Remember facts a user message supports: {r['supported']} of {r['total']} "
              f"(cue only {r['cue_only']}, unsupported {r['unsupported']}, "
              f"source session not found {r['session_missing']})"]
    return "\n".join(lines)


# ─── loading ─────────────────────────────────────────────────────────────────


def load_store_file(path: Path, label: str | None = None) -> Snapshot | None:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    store_id = str(doc.get("store_id") or label or path)
    return Snapshot(label=label or store_id, store_id=store_id, doc=doc)


def load_db_snapshots(*, get_db: Callable | None = None) -> list[Snapshot]:
    """Every worker's replicated fact snapshot, through the app's database access."""
    if get_db is None:
        from app.db.base import get_db
    snapshots: list[Snapshot] = []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, content FROM network_memory_records "
                    "WHERE id LIKE %s AND deleted = FALSE", ("memory-v2:%",))
        for row in cur.fetchall():
            try:
                doc = json.loads(row["content"] or "{}")
            except ValueError:
                continue
            parts = str(row["id"]).split(":")
            device = parts[1] if len(parts) >= 3 else str(row["id"])
            store_id = str(doc.get("store_id") or device)
            snapshots.append(Snapshot(label=f"device:{device}", store_id=store_id, doc=doc))
    return snapshots


def load_db_nodes(since: float, sessions: Iterable[str], *,
                  get_db: Callable | None = None) -> list[tuple[str, float, str, dict[str, Any]]]:
    """Session nodes written since ``since`` (for citations) plus every node of
    the named sessions (the Remember facts' source sessions, however old)."""
    if get_db is None:
        from app.db.base import get_db
    rows: dict[tuple[str, str], tuple[str, float, Any]] = {}
    sessions = sorted(set(s for s in sessions if s))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT session_id, node_id, ts, message FROM fluid_session_nodes WHERE ts >= %s",
                    (since,))
        for row in cur.fetchall():
            rows[(row["session_id"], row["node_id"])] = (row["session_id"], row["ts"], row["message"])
        for i in range(0, len(sessions), 200):
            chunk = sessions[i:i + 200]
            cur.execute("SELECT session_id, node_id, ts, message FROM fluid_session_nodes "
                        "WHERE session_id IN (" + ",".join(["%s"] * len(chunk)) + ")", tuple(chunk))
            for row in cur.fetchall():
                rows[(row["session_id"], row["node_id"])] = (row["session_id"], row["ts"], row["message"])
    nodes = []
    for session_id, ts, message in rows.values():
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except ValueError:
                continue
        if isinstance(message, dict):
            nodes.append((session_id, float(ts), str(message.get("role", "")), message))
    return nodes


def gather(*, since: float, until: float, store_files: list[tuple[str | None, Path]],
           logs: list[tuple[str, Path]], use_db: bool, get_db: Callable | None = None,
           local_root: Path | None = None) -> Sources:
    """Everything the report reads. Local files first; a database snapshot of
    the same store (same ``store_id``) keeps the local file's label."""
    sources = Sources(logs=list(logs))
    if local_root is not None:
        snap = load_store_file(local_root / "memories.json", "local")
        if snap:
            sources.snapshots.append(snap)
        log = local_root / ".synthesis-log.jsonl"
        if log.exists() and not any(p == log for _, p in sources.logs):
            sources.logs.append(("local", log))
    for label, path in store_files:
        snap = load_store_file(path, label)
        if snap:
            sources.snapshots.append(snap)
    if use_db:
        known = {s.store_id for s in sources.snapshots}
        sources.snapshots += [s for s in load_db_snapshots(get_db=get_db) if s.store_id not in known]
        remember_sessions = [f.source_session for f in collect_facts(sources.snapshots).values()
                             if f.kind == "remember" and f.source_session]
        sources.nodes = load_db_nodes(since, remember_sessions, get_db=get_db)
    return sources


def _parse_date(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _labelled(values: list[str]) -> list[tuple[str | None, Path]]:
    out = []
    for v in values:
        label, sep, path = v.partition("=")
        out.append((label, Path(path)) if sep else (None, Path(v)))
    return out


def main(argv: list[str] | None = None, *, emit: Callable[[str], None] = print,
         get_db: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", help="start date, local (default: 7 days ago)")
    parser.add_argument("--until", help="end date, exclusive, local (default: tomorrow)")
    parser.add_argument("--store", action="append", default=[],
                        help="extra memories.json, as PATH or LABEL=PATH (repeatable)")
    parser.add_argument("--log", action="append", default=[],
                        help=".synthesis-log.jsonl, as LABEL=PATH (repeatable)")
    parser.add_argument("--skills-root", help="local fluid skills root (default: the app's)")
    parser.add_argument("--no-local", action="store_true", help="do not read the local skills root")
    parser.add_argument("--no-db", action="store_true", help="files only; skip Postgres")
    parser.add_argument("--json", help="also write the report as JSON here")
    args = parser.parse_args(argv)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = _parse_date(args.since) if args.since else (today - timedelta(days=7)).timestamp()
    until = _parse_date(args.until) if args.until else (today + timedelta(days=1)).timestamp()
    local_root = None
    if not args.no_local:
        if args.skills_root:
            local_root = Path(args.skills_root)
        else:
            from app.core.fluid_memory import skills_root

            local_root = skills_root()
    logs = [(label or path.stem, path) for label, path in _labelled(args.log)]
    pool = False
    if not args.no_db and get_db is None:
        from app.db.base import init_pool

        init_pool(min_conn=1, max_conn=2)
        pool = True
    try:
        sources = gather(since=since, until=until, store_files=_labelled(args.store), logs=logs,
                         use_db=not args.no_db, get_db=get_db, local_root=local_root)
    finally:
        if pool:
            from app.db.base import close_pool

            close_pool()
    report = build_report(sources, since=since, until=until)
    emit(format_report(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
