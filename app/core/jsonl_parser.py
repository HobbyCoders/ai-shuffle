"""
JSONL History Parser

Parses Claude Code session history files (*.jsonl) to reconstruct chat messages
in the same format as live streaming, ensuring visual consistency between
resumed and live sessions.

JSONL Format (from Claude Code ~/.claude/projects/[project]/[session_id].jsonl):
- User messages: {"type":"user", "message":{"role":"user","content":"..."}, "uuid":"...", "timestamp":"..."}
- Assistant messages: {"type":"assistant", "message":{"role":"assistant","content":[blocks]}, "uuid":"...", "timestamp":"..."}
- Tool results: {"type":"user", "message":{"role":"user","content":[{"tool_use_id":"...","type":"tool_result","content":"..."}]}, "toolUseResult":{...}}
- Queue operations: {"type":"queue-operation", ...} - ignored
- File snapshots: {"type":"file-history-snapshot", ...} - ignored

Key fields that affect parsing:
- isMeta: Boolean - meta messages like slash commands, system prompts (should be skipped for display)
- isSidechain: Boolean - messages in alternate conversation branches (should be skipped)
- parentUuid: String - points to parent message in conversation tree
"""

import base64
import binascii
import json
import logging
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import List, Dict, Any, Optional, Generator, Tuple
from datetime import datetime, timezone

from app.core.config import settings
from app.core.query_streaming import extract_mcp_image
from app.core.session_title import derive_session_title
from app.core.system_message_filter import is_hidden_system_subtype

logger = logging.getLogger(__name__)

# ─── Parse Result Cache ─────────────────────────────────────────────────────
# Cache parsed JSONL results to avoid re-reading files on every poll/load.
# Validated by file mtime + size - if file changes, cache entry is invalidated.
#
# Both caches are :class:`ByteBoundedLRU`: they hold the transformed message
# list itself (a hit costs one shallow copy, no deserialization) and are bounded
# by ESTIMATED BYTES as well as by count, since a transcript entry is unbounded
# in size (inline base64 screenshots, untruncated tool inputs).
_PARSE_CACHE_MAX_SIZE = 50

#: Byte budget per cache namespace. Screenshot-heavy transcripts are what make
#: entries large, so they are what this bounds.
_PARSE_CACHE_MAX_BYTES = 32 * 1024 * 1024

#: A single entry bigger than this is not cached at all. Without it one
#: screenshot-heavy session would evict every other session on each insert and
#: still be evicted itself before being read back — pure churn.
_PARSE_CACHE_MAX_ENTRY_BYTES = 8 * 1024 * 1024


def estimate_size(obj: Any, _depth: int = 0) -> int:
    """Estimated retained size of a parsed-transcript structure, in bytes.

    Walks the (acyclic, JSON-shaped) message structure summing
    ``sys.getsizeof`` for scalars and containers. Approximate on purpose: it
    only has to be proportional to the real footprint so the LRU's budget is
    meaningful, and it must be cheap enough to run once per cache insert (the
    caller has just parsed the whole transcript anyway).
    """
    if _depth > 12:  # transcripts are shallow; stop pathological nesting
        return sys.getsizeof(obj)
    if isinstance(obj, str):
        return sys.getsizeof(obj)
    if isinstance(obj, (bytes, bytearray)):
        return sys.getsizeof(obj)
    if isinstance(obj, dict):
        total = sys.getsizeof(obj)
        for k, v in obj.items():
            total += estimate_size(k, _depth + 1) + estimate_size(v, _depth + 1)
        return total
    if isinstance(obj, (list, tuple, set)):
        total = sys.getsizeof(obj)
        for item in obj:
            total += estimate_size(item, _depth + 1)
        return total
    return sys.getsizeof(obj)


class ByteBoundedLRU:
    """A small, thread-safe LRU bounded by BOTH entry count and estimated bytes.

    Deliberately not an ad-hoc dict: the eviction rule has to hold on every
    write path (there are four), and "50 entries" is not a memory bound when a
    single entry can be tens of megabytes.

    * ``__setitem__`` measures the value with :func:`estimate_size`, refuses
      anything above ``max_entry_bytes`` (evicting any previous value for that
      key, so a stale entry is never left behind), and evicts least-recently-
      used entries until both budgets are satisfied.
    * ``get`` / ``__getitem__`` / ``move_to_end`` refresh recency.
    * ``lock`` is a re-entrant lock the module also exposes, so callers can
      hold it across a read-then-write sequence without deadlocking against
      the cache's own locking.
    """

    def __init__(
        self,
        max_entries: int,
        max_bytes: int,
        max_entry_bytes: Optional[int] = None,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = (
            max_entry_bytes if max_entry_bytes is not None else max_bytes
        )
        self.lock = threading.RLock()
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._sizes: Dict[Any, int] = {}
        self._bytes = 0

    # -- introspection ---------------------------------------------------
    @property
    def current_bytes(self) -> int:
        with self.lock:
            return self._bytes

    def __len__(self) -> int:
        with self.lock:
            return len(self._data)

    def __contains__(self, key) -> bool:
        with self.lock:
            return key in self._data

    def keys(self):
        with self.lock:
            return list(self._data.keys())

    # -- reads -----------------------------------------------------------
    def get(self, key, default=None):
        with self.lock:
            if key not in self._data:
                return default
            self._data.move_to_end(key)
            return self._data[key]

    def __getitem__(self, key):
        with self.lock:
            value = self._data[key]  # KeyError propagates, dict semantics
            self._data.move_to_end(key)
            return value

    def move_to_end(self, key) -> None:
        with self.lock:
            if key in self._data:
                self._data.move_to_end(key)

    # -- writes ----------------------------------------------------------
    def __setitem__(self, key, value) -> None:
        self.put(key, value)

    def put(self, key, value, size: Optional[int] = None) -> bool:
        """Insert ``value``; returns False when it was too big to cache."""
        if size is None:
            size = estimate_size(value)
        with self.lock:
            self._discard(key)
            if size > self.max_entry_bytes:
                logger.debug(
                    "Parse cache: entry %r (%d bytes) exceeds the per-entry "
                    "budget (%d) — not cached",
                    key, size, self.max_entry_bytes,
                )
                return False
            self._data[key] = value
            self._sizes[key] = size
            self._bytes += size
            self._evict()
            return True

    def pop(self, key, default=None):
        with self.lock:
            if key not in self._data:
                return default
            value = self._data[key]
            self._discard(key)
            return value

    def __delitem__(self, key) -> None:
        with self.lock:
            if key not in self._data:
                raise KeyError(key)
            self._discard(key)

    def clear(self) -> None:
        with self.lock:
            self._data.clear()
            self._sizes.clear()
            self._bytes = 0

    # -- internals (call with the lock held) -----------------------------
    def _discard(self, key) -> None:
        if key in self._data:
            self._bytes -= self._sizes.pop(key, 0)
            del self._data[key]

    def _evict(self) -> None:
        while self._data and (
            len(self._data) > self.max_entries or self._bytes > self.max_bytes
        ):
            oldest, _ = next(iter(self._data.items()))
            self._discard(oldest)


def _copy_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a caller-owned shallow copy of a cached message list.

    Cached values are treated as IMMUTABLE snapshots. Callers routinely
    re-key/extend the top level of each message (the session API builds its own
    dicts from them; the cost-metadata pass writes into them), so the top level
    is copied; nested values (``toolInput``, ``metadata``, screenshots …) are
    shared and must not be mutated in place — the same contract every consumer
    in the codebase already follows. Copying one dict per message costs
    microseconds; the JSON round-trip this replaces cost tens of milliseconds
    and re-allocated the entire transcript on every cache HIT.
    """
    return [dict(m) for m in messages]


# Cache: sdk_session_id -> (file_mtime, file_size, messages, usage_data)
_parse_cache = ByteBoundedLRU(
    _PARSE_CACHE_MAX_SIZE, _PARSE_CACHE_MAX_BYTES, _PARSE_CACHE_MAX_ENTRY_BYTES
)
_parse_cache_lock = _parse_cache.lock

# Separate cache namespace for the store-backed parse path. Keyed by
# sdk_session_id, value is (messages, usage_data, store_version). Kept ENTIRELY
# separate from _parse_cache so a store read can never serve a JSONL-derived
# result, or vice-versa. There is no on-disk mtime to validate a store result
# against, so entries carry a (count, max_id) store version stamp and are
# invalidated alongside the JSONL cache via invalidate_parse_cache().
_store_parse_cache = ByteBoundedLRU(
    _PARSE_CACHE_MAX_SIZE, _PARSE_CACHE_MAX_BYTES, _PARSE_CACHE_MAX_ENTRY_BYTES
)
_store_parse_cache_lock = _store_parse_cache.lock

# Cache: sdk_session_id -> (dir_mtime, agent_files_dict)
# Count-bounded only: entries are a handful of Paths, not transcript payloads.
_agent_paths_cache: OrderedDict = OrderedDict()
_agent_paths_cache_lock = threading.Lock()

# ─── Negative memo for "this session has no local JSONL" ────────────────────
# get_session_jsonl_path falls back to iterating EVERY Claude project directory
# with a stat() each. Under postgres_only most sessions have no local JSONL at
# all (the transcript lives in Postgres, and the CLI writes to a tmpfs config
# dir), and pg_session_store's lazy backfill probes this on every store miss —
# i.e. on every open/poll of such a session. Without a negative memo that is a
# full directory scan per request. Mirrors the ``_empty_memo``
# pattern in app/core/history_backfill.py.
_JSONL_MISS_TTL_SECONDS = 30.0
#: (sdk_session_id, working_dir, projects_root) -> monotonic time of the miss.
#: The projects root is part of the key so a reconfigured CLAUDE_PROJECTS_DIR
#: never serves a memo taken against the old root.
_jsonl_miss_memo: Dict[Tuple[str, str, str], float] = {}
_jsonl_miss_lock = threading.Lock()


#: Bound on the negative memo. Entries are tiny, but a long-lived process should
#: not accumulate one per session forever — sweep the expired ones when the dict
#: grows past this.
_JSONL_MISS_SOFT_CAP = 1024


def _memoize_jsonl_miss(sdk_session_id: str, working_dir: str, root: str) -> None:
    now = time.monotonic()
    with _jsonl_miss_lock:
        if len(_jsonl_miss_memo) > _JSONL_MISS_SOFT_CAP:
            for k in [
                k for k, at in _jsonl_miss_memo.items()
                if now - at >= _JSONL_MISS_TTL_SECONDS
            ]:
                _jsonl_miss_memo.pop(k, None)
        _jsonl_miss_memo[(sdk_session_id, working_dir, root)] = now


def _jsonl_miss_is_fresh(sdk_session_id: str, working_dir: str, root: str) -> bool:
    key = (sdk_session_id, working_dir, root)
    with _jsonl_miss_lock:
        at = _jsonl_miss_memo.get(key)
        if at is None:
            return False
        if time.monotonic() - at >= _JSONL_MISS_TTL_SECONDS:
            _jsonl_miss_memo.pop(key, None)
            return False
        return True


def _forget_jsonl_miss(sdk_session_id: Optional[str] = None) -> None:
    """Drop negative-lookup memos (one session, or all when ``None``).

    Called whenever a session's cached parse is invalidated: that is exactly
    when a transcript may have just appeared on disk.
    """
    with _jsonl_miss_lock:
        if sdk_session_id is None:
            _jsonl_miss_memo.clear()
            return
        for k in [k for k in _jsonl_miss_memo if k[0] == sdk_session_id]:
            _jsonl_miss_memo.pop(k, None)

# Maximum display size for tool output content (matches streaming behavior)
MAX_DISPLAY_OUTPUT_SIZE = 2000


def _is_background_ack_result(tool_msg: Dict[str, Any], output: str) -> bool:
    """True if a tool_result is merely the acknowledgement for a *backgrounded*
    command that is still executing — not a real completion.

    Keeps the persisted/reload path showing the same RUNNING status the live
    render showed. Without this, a reload from JSONL would flip a still-running
    backgrounded Bash to "complete" — the reported bug.

    Backgrounded Bash (``run_in_background: true``) returns control to the model
    immediately with a "Command running in the background ... ID: bash_xxx"
    acknowledgement while the shell keeps working; the real output later arrives
    via a separate BashOutput/TaskOutput tool, not a second result for this id.
    """
    if tool_msg.get("toolName") not in ("Bash", "PowerShell"):
        return False

    tool_input = tool_msg.get("toolInput") or {}
    if isinstance(tool_input, dict) and (
        tool_input.get("run_in_background") or tool_input.get("runInBackground")
    ):
        return True

    # Fallback: sniff the standard CLI acknowledgement when the input flag
    # wasn't recorded (e.g. the CLI auto-backgrounded a long command).
    text = (output or "").strip().lower()
    if not text:
        return False
    return (
        "running in the background" in text
        or "running in background" in text
        or "background with id" in text
    )


# Phrases a backgrounded sub-agent's launch-ACK tool_result carries while the
# agent keeps running. The run_in_background flag (captured on the Task tool_use)
# is the primary signal; this is the fallback when the flag wasn't recorded.
_SUBAGENT_BG_ACK_PHRASES = (
    "launched successfully",
    "working in the background",
    "running in the background",
    "background with id",
    "notified automatically",
)


def _is_background_subagent_ack(task_info: Dict[str, Any], output: str) -> bool:
    """True if a Task/Agent tool_result is only the launch-ACK of a *backgrounded*
    sub-agent that is still running — not its real completion.

    A backgrounded sub-agent (``run_in_background: true``) returns control to the
    parent immediately with a launch acknowledgement; its real outcome arrives
    later as a terminal ``<task-notification>`` (handled separately), NOT a second
    tool_result for this id. Without this guard a reload flips a still-running
    backgrounded sub-agent to "completed" — the premature-✓ bug (mirrors the live
    path's run_in_background skip in query_engine, and ``_is_background_ack_result``
    for backgrounded Bash).
    """
    if task_info.get("run_in_background"):
        return True
    text = (output or "").strip().lower()
    if not text:
        return False
    return any(p in text for p in _SUBAGENT_BG_ACK_PHRASES)


def _is_task_launch_rejection(output: Any) -> bool:
    """True if a Task tool_result is a dispatcher rejection (``{"ok": false, ...}``).

    The launch died before a worker existed, so no terminal <task-notification>
    will ever arrive — the card must replay as 'error', not 'completed'. Mirrors
    ``_task_launch_failure_error`` in query_engine.py (the two files deliberately
    mirror sniffers rather than import each other — see the ACK-sniff mirror).
    """
    text = output if isinstance(output, str) else str(output or "")
    s = text.strip()
    if not s.startswith("{"):
        return False
    try:
        payload = json.loads(s)
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("ok") is False


def _extract_task_notification_field(content: str, tag: str) -> Optional[str]:
    """Pull a single ``<tag>value</tag>`` field out of a task-notification body."""
    import re
    m = re.search(rf"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
    return m.group(1).strip() if m else None


def _apply_task_notification_status(
    content: str,
    messages: List[Dict[str, Any]],
    messages_by_tool_id: Dict[str, int],
) -> None:
    """Flip the sub-agent a terminal ``<task-notification>`` refers to.

    A backgrounded sub-agent's launch-ACK result leaves its card RUNNING (see
    ``_is_background_subagent_ack``); its real completion arrives only as this
    SDK-internal notification, keyed by the launch ``tool-use-id``. Reflect that
    terminal state on reload instead of leaving the card stuck "running". Scoped
    to sub-agents — backgrounded Bash keeps its existing handling.
    """
    tool_use_id = _extract_task_notification_field(content, "tool-use-id")
    if not tool_use_id:
        return
    idx = messages_by_tool_id.get(tool_use_id)
    if idx is None or idx >= len(messages):
        return
    msg = messages[idx]
    if msg.get("type") != "subagent":
        return
    status = (_extract_task_notification_field(content, "status") or "").lower()
    msg["agentStatus"] = "error" if status in ("failed", "stopped", "error") else "completed"

    # Recover usage stats from the persisted <usage> sub-fields so the reloaded
    # card shows token/tool/duration stats instead of a bare checkmark. The unpark
    # prompt writes subagent_tokens/tool_uses/duration_ms; map subagent_tokens →
    # total_tokens to match the live card's agentUsage shape ({total_tokens,
    # tool_uses, duration_ms}). Absent/blank fields are simply omitted.
    def _usage_int(tag: str) -> Optional[int]:
        raw = _extract_task_notification_field(content, tag)
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    usage: Dict[str, Any] = {}
    total_tokens = _usage_int("subagent_tokens")
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    tool_uses = _usage_int("tool_uses")
    if tool_uses is not None:
        usage["tool_uses"] = tool_uses
    duration_ms = _usage_int("duration_ms")
    if duration_ms is not None:
        usage["duration_ms"] = duration_ms
    if usage:
        msg["agentUsage"] = usage

    # Recover the result text so the card body shows the outcome instead of the
    # launch ACK. A card carrying a persisted terminal notification is always a
    # backgrounded sub-agent whose content is only the launch ACK, so overwriting
    # is safe. Truncate to 2000 chars to match the sync path (msg["content"] =
    # output[:2000]). Prefer <result>; fall back to <summary>.
    result_text = _extract_task_notification_field(content, "result")
    if not result_text:
        result_text = _extract_task_notification_field(content, "summary")
    if result_text:
        msg["content"] = result_text[:2000]


def _truncate_for_display(content: str, max_size: int = MAX_DISPLAY_OUTPUT_SIZE) -> str:
    """
    Truncate content for display purposes.

    Large content like base64-encoded images or file contents should be truncated
    early in the parsing process to avoid memory issues when sending via WebSocket.
    """
    if not content or len(content) <= max_size:
        return content

    # Check if it looks like base64 image data
    is_likely_base64 = (
        'data:image/' in content[:100] or
        content[:10].startswith('iVBOR') or  # PNG base64 signature
        content[:10].startswith('/9j/') or   # JPEG base64 signature
        content[:10].startswith('R0lGOD') or # GIF base64 signature
        (len(content) > 10000 and sum(c.isalnum() or c in '+/=' for c in content[:1000]) > 950)
    )

    if is_likely_base64:
        original_size_kb = int(len(content) * 0.75 / 1024)
        return f"[Image/binary content - {original_size_kb}KB - truncated for display]"

    return f"{content[:max_size]}\n\n[... truncated - {len(content) // 1024}KB total]"


def get_project_dir_name(working_dir: str) -> str:
    """
    Convert a working directory path to Claude's project directory name.

    Claude converts paths like /workspace/github-projects to -workspace-github-projects
    The leading dash is kept as that's how Claude encodes absolute paths.

    On Windows, backslashes and colons are also replaced with dashes, e.g.:
    D:\\Projects\\ai-shuffle -> D--Projects-ai-shuffle

    The colon after drive letter (D:) becomes a dash, and each backslash becomes a dash,
    resulting in D-- at the start for D:\\ paths.

    Spaces in the path are ALSO replaced with dashes, matching the Claude CLI's
    encoding — e.g. D:\\Projects\\new folder -> D--Projects-new-folder. Without
    this, a cwd containing a space would resolve to a "ghost" project dir name that
    differs from where the CLI actually stored the transcripts.
    """
    import os
    # Replace Windows drive colon (D: -> D-), path separators (\, /), and spaces, all
    # with dashes. os.sep alone is platform-dependent (it is "/" on POSIX), so a
    # Windows-style path processed on Linux would keep its backslashes; replace "\"
    # explicitly as well so the encoding is identical on every platform. Order among
    # distinct characters is irrelevant.
    return (
        working_dir.replace(os.sep, "-")
        .replace("\\", "-")
        .replace("/", "-")
        .replace(":", "-")
        .replace(" ", "-")
    )


def get_session_jsonl_path(sdk_session_id: str, working_dir: str = "/workspace") -> Optional[Path]:
    """
    Get the path to a session's JSONL history file.

    Args:
        sdk_session_id: The Claude session ID (UUID)
        working_dir: The working directory used when the session was created

    Returns:
        Path to the JSONL file, or None if not found
    """
    # A recent miss short-circuits the whole probe: the fallback
    # below stats EVERY project directory, and this is called on every store
    # miss — i.e. on every open/poll of a session whose transcript lives only in
    # Postgres. The memo is negative-only and TTL'd, so a transcript that
    # appears on disk is picked up within _JSONL_MISS_TTL_SECONDS (immediately
    # when the session's parse cache is invalidated, which is what happens when
    # its transcript changes).
    # Get Claude projects directory from config (supports custom paths via CLAUDE_PROJECTS_DIR env var)
    claude_projects_dir = settings.get_claude_projects_dir

    if _jsonl_miss_is_fresh(sdk_session_id, working_dir, str(claude_projects_dir)):
        return None

    logger.debug(f"Looking for JSONL in claude_projects_dir: {claude_projects_dir}")

    # Try to find the project directory
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    logger.debug(f"Checking project dir: {project_dir}")

    if project_dir.exists():
        jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
        logger.debug(f"Checking JSONL path: {jsonl_path}")
        if jsonl_path.exists():
            _forget_jsonl_miss(sdk_session_id)
            return jsonl_path

    # Try to search all project directories for this session
    if claude_projects_dir.exists():
        for proj_dir in claude_projects_dir.iterdir():
            if proj_dir.is_dir():
                jsonl_path = proj_dir / f"{sdk_session_id}.jsonl"
                if jsonl_path.exists():
                    logger.debug(f"Found JSONL in alternate project dir: {jsonl_path}")
                    _forget_jsonl_miss(sdk_session_id)
                    return jsonl_path

    logger.warning(f"JSONL file not found for session {sdk_session_id} in {claude_projects_dir}")
    _memoize_jsonl_miss(sdk_session_id, working_dir, str(claude_projects_dir))
    return None


def get_hooks_jsonl_path(sdk_session_id: str, working_dir: str) -> Path:
    """
    Get the path to a session's hooks JSONL file.

    This file stores hook stderr output (plugin warnings) that aren't captured
    in the main session JSONL.

    Args:
        sdk_session_id: The Claude session ID (UUID)
        working_dir: The working directory for the project

    Returns:
        Path to the hooks JSONL file (may not exist)

    Pure path computation. It used to ``mkdir`` the project directory, and its
    only caller is the READ path (every history parse) — so reading a session
    whose transcript was found in a different project dir created an empty
    "ghost" project dir for the computed name (see get_agent_jsonl_paths).
    """
    claude_projects_dir = settings.get_claude_projects_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    return project_dir / f"{sdk_session_id}.hooks.jsonl"


def write_system_message(
    sdk_session_id: str,
    working_dir: str,
    subtype: str,
    content: str,
    data: Optional[Dict[str, Any]] = None
) -> None:
    """
    Append a system message to the session's main JSONL file.

    This writes messages in Claude Code's JSONL format for system entries.
    These messages are displayed in chat history and persist across session loads.

    Args:
        sdk_session_id: The Claude session ID (UUID)
        working_dir: The working directory for the project
        subtype: Message subtype (e.g., 'worktree_context', 'plugin_warning')
        content: Human-readable message content
        data: Optional structured data for the message
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        logger.warning(f"Cannot write system message - JSONL not found for session {sdk_session_id}")
        return

    entry = {
        "type": "system",
        "subtype": subtype,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "uuid": f"sys-{uuid.uuid4().hex[:12]}"
    }

    if data:
        entry["data"] = data

    try:
        # A transcript whose last line was torn (writer died mid-line, no
        # trailing newline) would otherwise swallow this entry into that
        # invalid line, and both would be skipped on every read.
        needs_newline = False
        with open(jsonl_path, 'rb') as rf:
            if rf.seek(0, 2) > 0:
                rf.seek(-1, 2)
                needs_newline = rf.read(1) != b'\n'
        with open(jsonl_path, 'a', encoding='utf-8') as f:
            f.write(('\n' if needs_newline else '') + json.dumps(entry) + '\n')
        logger.debug(f"Wrote system message ({subtype}) to {jsonl_path}")
    except Exception as e:
        logger.error(f"Failed to write system message to {jsonl_path}: {e}")


# ─── Inline screenshots → artifact URLs, at PARSE time ──────────────────────
# A transcript's screenshots are the single biggest thing in it: a Chrome/
# computer-use tool result carries a whole PNG as base64, and a session can hold
# dozens. The parser used to reconstruct every one of them as a
# ``data:<mime>;base64,<megabytes>`` string, hand that to the caller, AND keep it
# in the parse cache — after which the API layer rewrote it to
# ``/api/v1/artifacts/<sha256>`` whenever the bytes were already in the artifact
# store, and threw the big string away.
#
# The hash is knowable right here (the bytes are in the entry), so the
# substitution moves to parse time and the multi-megabyte string is never built
# or retained at all.
#
# Behaviour is unchanged: the URL is used ONLY when the artifact store already
# holds those exact bytes. A screenshot the store does not have still becomes
# the inline data URI, exactly as before, so nothing can vanish from a
# transcript. (``compact_messages_for_display`` still runs on the API side and
# is a no-op on an already-rewritten value — it only touches ``data:`` URIs.)


def _image_block_payload(content: Any) -> Optional[tuple]:
    """``(base64_data, mime)`` for the first image block — no URI built.

    Mirrors the block shapes ``app.core.query_streaming.extract_mcp_image``
    accepts (Anthropic API ``source`` dicts, flat MCP wire blocks, and the
    Pydantic content objects of both), but stops one step short: it returns the
    raw payload instead of concatenating it into a data URI, so a caller that
    can resolve the bytes to an artifact never materialises the big string.

    Returns ``None`` when there is no image block, or when the block is a shape
    this does not recognise — the caller then falls back to ``extract_mcp_image``
    so an unfamiliar shape degrades to the old behaviour rather than to a lost
    screenshot.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        # dict-based blocks (Anthropic API format / MCP wire / parsed JSONL)
        if isinstance(block, dict):
            if block.get("type") != "image":
                continue
            source = block.get("source")
            if isinstance(source, dict) and source.get("data"):
                mime = (
                    source.get("media_type") or source.get("mimeType") or "image/png"
                )
                return source["data"], mime
            if block.get("data"):
                mime = block.get("mimeType") or block.get("mime_type") or "image/png"
                return block["data"], mime
        # Pydantic content-block objects (e.g. ImageContent from the SDK)
        elif getattr(block, "type", None) == "image":
            source = getattr(block, "source", None)
            if source is not None:
                data = (
                    source.get("data") if isinstance(source, dict)
                    else getattr(source, "data", None)
                )
                if data:
                    mime = (
                        source.get("media_type") if isinstance(source, dict)
                        else getattr(source, "media_type", None)
                    ) or "image/png"
                    return data, mime
            data = getattr(block, "data", None)
            if data:
                return data, (getattr(block, "mimeType", None) or "image/png")
    return None


def _stored_artifact_url(b64_data: Any, mime: Any) -> Optional[str]:
    """``/api/v1/artifacts/<sha256>`` iff those exact bytes are already stored.

    The mime is validated against the SAME shape the API-side rewrite requires
    (``data:<mime>;base64,<payload>`` with a mime free of ``;`` and ``,``), so a
    payload the old path would have LEFT inline is still left inline here.
    Returns ``None`` on anything unusual — undecodable base64, empty bytes, an
    absent artifact, or an unavailable artifact store — and never raises: a
    screenshot lookup must not be able to break a transcript read.
    """
    if not isinstance(b64_data, str) or not b64_data:
        return None
    if not isinstance(mime, str) or not mime or ";" in mime or "," in mime:
        return None
    try:
        from app.core.artifact_ingest import content_hash
        from app.core.artifact_store import artifact_path

        try:
            raw = base64.b64decode(b64_data, validate=False)
        except (binascii.Error, ValueError):
            return None
        if not raw:
            return None
        digest = content_hash(raw)
        if artifact_path(digest) is None:
            return None
        return f"/api/v1/artifacts/{digest}"
    except Exception:  # noqa: BLE001 — never break a transcript read
        logger.debug("screenshot artifact lookup failed", exc_info=True)
        return None


def extract_screenshot_ref(content: Any) -> Optional[str]:
    """The renderable reference for the first image block in ``content``.

    An artifact URL when the bytes are already content-addressed on disk,
    otherwise the same inline ``data:`` URI :func:`extract_mcp_image` has always
    returned. ``None`` when there is no image.
    """
    payload = _image_block_payload(content)
    if payload is None:
        # Unrecognised shape: defer to the original extractor verbatim.
        return extract_mcp_image(content)
    data, mime = payload
    return _stored_artifact_url(data, mime) or f"data:{mime};base64,{data}"


def parse_hooks_jsonl(sdk_session_id: str, working_dir: str) -> List[Dict[str, Any]]:
    """
    Parse a session's hooks JSONL file and return plugin warning messages.

    Args:
        sdk_session_id: The Claude session ID
        working_dir: The working directory for the project

    Returns:
        List of plugin warning messages with: id, role, content, type, metadata
    """
    hooks_path = get_hooks_jsonl_path(sdk_session_id, working_dir)

    if not hooks_path.exists():
        return []

    messages = []
    for entry in parse_jsonl_file(hooks_path):
        if entry.get("type") == "plugin_warning":
            plugin = entry.get("plugin", "Plugin")
            message_content = entry.get("message", "")
            severity = entry.get("severity", "warning")
            timestamp = entry.get("timestamp", "")

            messages.append({
                "id": f"plugin-{timestamp}",
                "role": "system",
                "content": message_content,
                "type": "system",
                "subtype": "plugin_warning",
                "systemSubtype": "plugin_warning",
                "systemData": {
                    "plugin": plugin,
                    "message": message_content,
                    "severity": severity
                },
                "metadata": {
                    "plugin": plugin,
                    "severity": severity,
                    "timestamp": timestamp
                },
                "streaming": False
            })

    logger.debug(f"Parsed {len(messages)} hook messages from {hooks_path}")
    return messages


def parse_jsonl_file(jsonl_path: Path) -> Generator[Dict[str, Any], None, None]:
    """
    Parse a JSONL file line by line.

    Yields each parsed JSON object. A line that is not valid JSON, or is valid
    JSON but not an object (``null``, a bare number, an array — what a torn or
    interleaved write leaves behind), is skipped with a warning; parsing
    continues with the next line.

    Bytes that are not valid UTF-8 are decoded as U+FFFD instead of raising:
    with a strict decoder ONE bad byte raised out of the read loop and dropped
    every line from that point — for a small transcript the whole history.
    """
    try:
        with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse line {line_num} in {jsonl_path}: {e}")
                    continue
                if not isinstance(entry, dict):
                    logger.warning(
                        f"Skipping non-object line {line_num} in {jsonl_path} "
                        f"({type(entry).__name__})"
                    )
                    continue
                yield entry
    except Exception as e:
        logger.error(f"Failed to read JSONL file {jsonl_path}: {e}")


def extract_text_from_content(content: Any) -> str:
    """
    Extract text content from message content which can be string or list of blocks.
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        return "".join(text_parts)

    return ""


def _is_system_content(content: str) -> bool:
    """
    Check if content is empty or whitespace-only.

    Previously this filtered out various system messages, but we now display
    all content to the user. Slash commands are handled by _parse_slash_command(),
    and other system content is shown as-is.
    """
    if not content or not content.strip():
        return True
    return False


def _parse_slash_command(content: str) -> Optional[Dict[str, Any]]:
    """
    Parse slash command XML tags into structured data.

    Slash commands from Claude Code have this format:
    <command-message>plugin:command</command-message>
    <command-name>/plugin:command</command-name>
    <command-args>arguments here</command-args>

    Returns:
        Dict with command_id, command_name, command_args, or None if not a slash command
    """
    import re

    if not content or not content.strip().startswith("<command-"):
        return None

    result = {}

    # Extract command-message (the internal command identifier)
    msg_match = re.search(r'<command-message>(.*?)</command-message>', content, re.DOTALL)
    if msg_match:
        result["command_id"] = msg_match.group(1).strip()

    # Extract command-name (the display name with slash prefix)
    name_match = re.search(r'<command-name>(.*?)</command-name>', content, re.DOTALL)
    if name_match:
        result["command_name"] = name_match.group(1).strip()

    # Extract command-args (the arguments passed to the command)
    args_match = re.search(r'<command-args>(.*?)</command-args>', content, re.DOTALL)
    if args_match:
        result["command_args"] = args_match.group(1).strip()

    # Must have at least command_name to be valid
    if "command_name" in result:
        return result

    return None


def _is_task_notification(content: str) -> bool:
    """
    Check if content is a Claude Code task-notification message.

    These are internal async completion callbacks from the Task/subagent system.
    The CLI injects them as user-role messages when a background agent finishes,
    but they're only meant for Claude's internal context — the subagent's work
    is already displayed via the tool_use/tool_result pair in SubagentMessage.
    """
    if not content:
        return False
    return "<task-notification>" in content


def _extract_local_command_output(content: str) -> Optional[str]:
    """
    Extract content from <local-command-stdout> tags if present.

    Returns the extracted content, or None if no tags found.
    """
    import re
    match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def get_agent_jsonl_paths(sdk_session_id: str, working_dir: str = "/workspace") -> Dict[str, Path]:
    """
    Find all agent JSONL files for a session.

    Agent files are named agent-{agent_id}.jsonl and contain entries with
    matching sessionId field.

    Discovers BOTH layouts:
      - Legacy flat:   <project_dir>/agent-*.jsonl
      - Current nested (CLI 2.1.x): <project_dir>/<sdk_session_id>/subagents/agent-*.jsonl

    Returns:
        Dict mapping agent_id to Path
    """
    claude_projects_dir = settings.get_claude_projects_dir
    agent_files: Dict[str, Path] = {}

    # First try the specified working_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    # Resolve the project dir robustly, mirroring get_session_jsonl_path: only trust
    # the computed project_dir when it actually contains the session's main file. A
    # computed dir can EXIST yet be the wrong one — e.g. a "ghost" dir created from a
    # mis-encoded path (space-preserving vs CLI's space->dash) that holds no
    # transcripts. Merely existing must not short-circuit the fallback, or the real
    # dir (with the nested <sid>/subagents/agent-*.jsonl) is never searched.
    project_dirs_to_search = []
    if (project_dir / f"{sdk_session_id}.jsonl").exists():
        project_dirs_to_search.append(project_dir)
    else:
        # Fallback: search all project directories for the one holding this session.
        if claude_projects_dir.exists():
            for proj_dir in claude_projects_dir.iterdir():
                if proj_dir.is_dir():
                    # Check if this project dir has the session file
                    session_file = proj_dir / f"{sdk_session_id}.jsonl"
                    if session_file.exists():
                        project_dirs_to_search.append(proj_dir)
                        break

    def _cache_mtime(search_dir: Path) -> float:
        """Cache-validity key for ``search_dir``.

        A new nested ``<sid>/subagents/agent-*.jsonl`` file lives in a
        subdirectory whose creation may not bump the top-level project dir's
        mtime, which would otherwise serve a stale (empty) cached result.
        Base the key on the MAX of the project dir's mtime and (if it exists)
        the nested ``<sid>/subagents`` dir's mtime.
        """
        mtime = search_dir.stat().st_mtime
        subagents_dir = search_dir / sdk_session_id / "subagents"
        try:
            if subagents_dir.exists():
                mtime = max(mtime, subagents_dir.stat().st_mtime)
        except OSError:
            pass
        return mtime

    def _scan_dir(scan_dir: Path) -> None:
        """Glob agent-*.jsonl in ``scan_dir`` and record matching subagents."""
        for agent_file in scan_dir.glob("agent-*.jsonl"):
            # Extract agent_id from filename (agent-{id}.jsonl)
            agent_id = agent_file.stem.replace("agent-", "")

            # Verify this agent file belongs to our session by checking first entry
            try:
                with open(agent_file, 'r', encoding='utf-8') as f:
                    first_line = f.readline().strip()
                    if first_line:
                        entry = json.loads(first_line)
                        if entry.get("sessionId") == sdk_session_id:
                            agent_files[agent_id] = agent_file
                            logger.debug(f"Found agent file {agent_id} for session {sdk_session_id}")
            except Exception as e:
                logger.warning(f"Failed to check agent file {agent_file}: {e}")

    # Check cache - validated by project directory mtime (incl. nested subagents dir)
    for search_dir in project_dirs_to_search:
        try:
            dir_mtime = _cache_mtime(search_dir)
            with _agent_paths_cache_lock:
                cached = _agent_paths_cache.get(sdk_session_id)
                if cached and cached[0] == dir_mtime:
                    _agent_paths_cache.move_to_end(sdk_session_id)
                    logger.debug(f"Agent paths cache hit for session {sdk_session_id}")
                    return dict(cached[1])  # Return copy to prevent mutation of cached data
        except OSError:
            pass

    for search_dir in project_dirs_to_search:
        # Legacy flat layout: <project_dir>/agent-*.jsonl
        _scan_dir(search_dir)

        # Current nested layout (CLI 2.1.x):
        #   <project_dir>/<sdk_session_id>/subagents/agent-*.jsonl
        # dict key dedup means an agent_id present in both layouts is not
        # double-counted; the nested scan runs second so it wins on collision.
        nested_dir = search_dir / sdk_session_id / "subagents"
        if nested_dir.exists():
            _scan_dir(nested_dir)

    # Cache the results (with LRU eviction)
    if project_dirs_to_search:
        try:
            dir_mtime = _cache_mtime(project_dirs_to_search[0])
            with _agent_paths_cache_lock:
                _agent_paths_cache[sdk_session_id] = (dir_mtime, agent_files)
                while len(_agent_paths_cache) > _PARSE_CACHE_MAX_SIZE:
                    _agent_paths_cache.popitem(last=False)
        except OSError:
            pass

    return agent_files


def parse_agent_history(agent_path: Path) -> List[Dict[str, Any]]:
    """
    Parse an agent's JSONL file and return child messages.

    Returns list of child messages with: id, type, content, toolName, toolId, toolInput, toolResult, toolStatus, timestamp
    Tool results are grouped with their corresponding tool_use entries.

    This is a thin wrapper that reads entries from the JSONL file then delegates
    to :func:`transform_agent_entries`. In ``postgres_only`` mode the same
    transform is fed entries pulled from the session store instead.
    """
    return transform_agent_entries(parse_jsonl_file(agent_path))


def transform_agent_entries(entries) -> List[Dict[str, Any]]:
    """Transform agent transcript entries into child messages.

    Source-agnostic core of :func:`parse_agent_history`: ``entries`` is any
    iterable of raw transcript dicts (from a JSONL file OR the Postgres session
    store). The transform is byte-identical regardless of source.
    """
    children: List[Dict[str, Any]] = []
    tool_names_by_id: Dict[str, str] = {}

    for entry in entries:
        entry_type = entry.get("type")
        message_data = entry.get("message", {})
        role = message_data.get("role")
        content = message_data.get("content")
        timestamp = entry.get("timestamp")
        uuid = entry.get("uuid", "")

        if entry_type == "assistant" and role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "")
                            if text:
                                children.append({
                                    "id": f"agent-text-{uuid}",
                                    "type": "text",
                                    "content": text,
                                    "timestamp": timestamp
                                })

                        elif block_type == "tool_use":
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name

                            children.append({
                                "id": f"agent-tool-{tool_id}",
                                "type": "tool_use",
                                "content": "",
                                "toolName": tool_name,
                                "toolId": tool_id,
                                "toolInput": block.get("input", {}),
                                "toolStatus": "running",  # Will be updated when result arrives
                                "timestamp": timestamp
                            })

        elif entry_type == "user" and role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        raw_content = block.get("content", "")
                        is_error = block.get("is_error", False)

                        # Extract text from content
                        if isinstance(raw_content, list):
                            output = extract_text_from_content(raw_content)
                        else:
                            output = raw_content if isinstance(raw_content, str) else ""

                        # Truncate for display
                        output = output[:2000] if output else ""

                        # Inline screenshot (browser/computer-use image block) —
                        # rendered below the child tool card, same as top-level.
                        # Resolves to the stored artifact URL when we have the
                        # bytes, else the inline data URI (see
                        # :func:`extract_screenshot_ref`).
                        screenshot = extract_screenshot_ref(raw_content)

                        # Group tool result with its corresponding tool_use
                        found_tool_use = False
                        for child in children:
                            if child.get("type") == "tool_use" and child.get("toolId") == tool_use_id:
                                child["toolResult"] = output
                                child["toolStatus"] = "error" if is_error else "complete"
                                if screenshot:
                                    child["toolScreenshot"] = screenshot
                                found_tool_use = True
                                break

                        # Only create separate tool_result if no matching tool_use found
                        if not found_tool_use:
                            children.append({
                                "id": f"agent-result-{tool_use_id}",
                                "type": "tool_result",
                                "content": output,
                                "toolName": tool_names_by_id.get(tool_use_id),
                                "toolId": tool_use_id,
                                **({"toolScreenshot": screenshot} if screenshot else {}),
                                "timestamp": timestamp
                            })

    return children


def parse_session_history(
    sdk_session_id: str,
    working_dir: str = "/workspace"
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Parse a session's JSONL history file and return messages + usage data.

    Results are cached in memory and validated by file mtime + size.
    Cache is invalidated when the file changes on disk.
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        logger.warning(f"JSONL file not found for session {sdk_session_id}")
        return [], {}

    # Check file stats for cache validation
    try:
        stat = jsonl_path.stat()
        file_mtime = stat.st_mtime
        file_size = stat.st_size
    except OSError:
        return [], {}

    # Check cache. A hit returns a shallow copy of the cached message list —
    # no deserialization at all.
    cached = _parse_cache.get(sdk_session_id)
    if cached is not None:
        c_mtime, c_size, c_messages, c_usage = cached
        if c_mtime == file_mtime and c_size == file_size:
            logger.debug(f"Parse cache hit for session {sdk_session_id}")
            return _copy_messages(c_messages), dict(c_usage)

    # Cache miss - do full parse
    logger.debug(f"Parse cache miss for session {sdk_session_id}, parsing JSONL")
    messages, usage_data = _parse_session_history_uncached(sdk_session_id, working_dir, jsonl_path)

    # Cache the transformed structure itself, byte-bounded. An oversized
    # transcript (inline base64 screenshots) is simply not cached rather than
    # evicting every other session on insert.
    _parse_cache[sdk_session_id] = (
        file_mtime, file_size, _copy_messages(messages), dict(usage_data),
    )

    return messages, usage_data


def _resolve_store_version(
    sdk_session_id: str,
    store_version_loader=None,
):
    """Fetch the cheap ``(count, max_id)`` store version stamp for a session.

    Used to validate ``_store_parse_cache`` entries on read and to stamp them on
    write. ``session_store_entries`` is an append-only log, so this pair changes
    on ANY insert (a new turn) and ANY delete (a rewind) — the exact mutations
    that must invalidate a cached postgres_only transcript parse, INCLUDING a
    rewind performed on a DIFFERENT install sharing the same Postgres.

    ``store_version_loader`` (``loader(session_id) -> (count, max_id)``) is
    injected by callers/tests; when absent we lazily import the sync DB helper
    ``_store_version_by_session_id_sync``. Returns ``None`` if no version can be
    resolved (loader raised, or the DB is unavailable) — the caller then declines
    to trust/write a stamp so a fresh parse is served, which is the safe default.
    """
    try:
        if store_version_loader is not None:
            version = store_version_loader(sdk_session_id)
        else:
            from app.core.pg_session_store import (
                _store_version_by_session_id_sync,
            )

            version = _store_version_by_session_id_sync(sdk_session_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "Store version probe failed for %s: %s; skipping cache validation",
            sdk_session_id,
            e,
        )
        return None
    if version is None:
        return None
    # Normalize to a plain hashable tuple so equality is by value.
    return (int(version[0]), int(version[1]))


def invalidate_parse_cache(sdk_session_id: str = None) -> None:
    """
    Invalidate cached parse results.

    Args:
        sdk_session_id: If provided, invalidate only this session. If None, clear all.
    """
    # A transcript that just changed may also have just APPEARED on disk, so
    # the negative JSONL memo has to go with it.
    _forget_jsonl_miss(sdk_session_id or None)

    with _parse_cache_lock:
        if sdk_session_id:
            _parse_cache.pop(sdk_session_id, None)
            logger.debug(f"Invalidated parse cache for session {sdk_session_id}")
        else:
            _parse_cache.clear()
            logger.debug("Cleared entire parse cache")

    with _agent_paths_cache_lock:
        if sdk_session_id:
            _agent_paths_cache.pop(sdk_session_id, None)
        else:
            _agent_paths_cache.clear()

    with _store_parse_cache_lock:
        if sdk_session_id:
            _store_parse_cache.pop(sdk_session_id, None)
            # Windowed pages live under composite keys in the same cache; the
            # pop above cannot reach them, and a page that survived an
            # invalidation would be a stale slice of a transcript we just
            # declared untrustworthy.
            _drop_window_cache_entries(sdk_session_id)
        else:
            _store_parse_cache.clear()


def pre_seed_parse_cache(
    sdk_session_id: str,
    messages: List[Dict[str, Any]],
    usage_data: Dict[str, Any],
    working_dir: str = "/workspace",
    store_version_loader=None,
    expected_store_version=None,
) -> bool:
    """
    Pre-seed the parse cache with an in-memory transcript snapshot.

    Historically called at turn end to avoid JSONL re-parsing on the next
    load. Instead of invalidating the cache (forcing an expensive JSONL
    re-parse), we update it with the messages already in memory.

    The cache entry is stored with the current JSONL file's mtime/size so it
    will be treated as valid on the next read. If the JSONL file changes again
    (e.g., another query starts), the mtime check will detect the change and
    the cache will be re-populated normally.

    Args:
        sdk_session_id: The Claude session ID
        messages: Final messages for the completed turn
        usage_data: Usage data dict
        working_dir: Working directory for finding the JSONL file
        store_version_loader: Optional ``loader(session_id) -> (count, max_id)``
            override for the store version probe (tests / callers with a handle)
        expected_store_version: The ``(count, max_id)`` store version resolved
            by the caller BEFORE it captured ``messages``. When provided and the
            store has since moved on (new turn appended, rewind deleted rows),
            the snapshot is already stale — the store entry is dropped instead
            of written, closing the window where an old snapshot got stamped
            with the store's NEW version and was then served verbatim.

    Returns:
        True if cache was pre-seeded, False on failure (falls back to invalidation)
    """
    # Snapshot once and reuse for both cache namespaces. The caller keeps
    # ownership of its own list, so copy the top level (see _copy_messages);
    # from here on the cached snapshot is immutable.
    try:
        cached_messages = _copy_messages(messages)
        cached_usage = dict(usage_data or {})
    except Exception as e:
        logger.error(f"Failed to snapshot messages for pre-seed {sdk_session_id}: {e}")
        invalidate_parse_cache(sdk_session_id)
        return False

    # Refresh the store-backed cache namespace (the postgres_only READ path).
    # Unlike _parse_cache, _store_parse_cache has NO on-disk mtime to
    # self-invalidate — once primed it is served verbatim until an explicit
    # invalidate or LRU eviction. The run_query live-store seed reads the
    # transcript through this SAME cache at stream START (before the new turn
    # exists), so without refreshing it here the just-completed exchange
    # "disappears" on the next session open (the newest turn missing) until the
    # process restarts. Overwrite it with the post-turn transcript so store
    # reads stay coherent after every turn. Done independently of JSONL presence
    # — postgres_only machines may have no local JSONL file at all.
    #
    # Stamp the entry with the store's current (count, max_id) version so the
    # READ path can validate it — matching the write in
    # parse_session_history_from_store. If the version can't be resolved we must
    # NOT leave an unstamped/stale entry behind (it would be served verbatim by a
    # pre-stamp reader on other installs, or dropped-then-reparsed here); DROP any
    # existing entry instead so the next read re-parses cleanly.
    store_version = _resolve_store_version(sdk_session_id, store_version_loader)
    if store_version is not None and expected_store_version is not None:
        expected = (int(expected_store_version[0]), int(expected_store_version[1]))
        if store_version != expected:
            # The store mutated between the caller's snapshot and now (next
            # turn started, or a rewind landed). Writing would stamp the OLD
            # messages with the NEW version — a poisoned entry that read-path
            # validation could never catch. Drop instead; the next read
            # re-parses the store fresh.
            logger.debug(
                "Pre-seed store version moved for %s (expected %s, now %s); "
                "dropping cache entry instead of seeding a stale snapshot",
                sdk_session_id,
                expected,
                store_version,
            )
            store_version = None
    with _store_parse_cache_lock:
        if store_version is not None:
            _store_parse_cache[sdk_session_id] = (
                cached_messages,
                cached_usage,
                store_version,
            )
        else:
            _store_parse_cache.pop(sdk_session_id, None)
        # The turn that just ended appended entries, so every cached WINDOW of
        # this session describes a shorter transcript: its seq cursors point at
        # the wrong rows now. The version stamp would catch that on read, but
        # dropping them here keeps "the pre-seed leaves this session's cache
        # coherent" true for both namespaces rather than one.
        _drop_window_cache_entries(sdk_session_id)

    # JSONL cache namespace (legacy / non-postgres_only reads): validated by the
    # JSONL file's mtime + size, so it needs a real file to stamp the entry.
    try:
        # Turn end is exactly when a transcript can have APPEARED on disk for a
        # session that had none, so re-probe instead of trusting the negative
        # memo. Once per turn, not once per poll.
        _forget_jsonl_miss(sdk_session_id)
        jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
        if not jsonl_path:
            # No JSONL on this machine (expected under postgres_only). The store
            # cache above is already fresh; there is nothing to seed for JSONL.
            logger.debug(
                f"Pre-seeded store parse cache for {sdk_session_id} "
                f"with {len(messages)} messages (no JSONL file present)"
            )
            return True

        stat = jsonl_path.stat()
        with _parse_cache_lock:
            _parse_cache[sdk_session_id] = (
                stat.st_mtime,
                stat.st_size,
                cached_messages,
                cached_usage,
            )

        logger.debug(f"Pre-seeded parse cache for session {sdk_session_id} with {len(messages)} messages")
        return True

    except Exception as e:
        # The store cache is already refreshed and correct; only the JSONL
        # mtime probe failed. Leave the store seed in place (do NOT invalidate,
        # which would clobber it) — non-postgres_only reads simply re-parse JSONL.
        logger.error(f"Failed to pre-seed JSONL parse cache for {sdk_session_id}: {e}")
        return False


def _track_entry_boundaries(entries, messages: list, out: list):
    """Yield ``entries`` unchanged, recording the message count before each one.

    ``out[i]`` ends up being the number of messages produced by ``entries[:i]``
    — i.e. the index in ``messages`` where entry ``i``'s output begins. The
    generator is pulled once per loop iteration, AFTER the previous entry's body
    has fully run (``continue`` included), so this measures real per-entry
    output without touching the transform body at all.

    That mapping is what lets a WINDOWED parse hand back exactly the messages a
    given range of entries produced, dropping the boundary context it had to
    transform in order to merge correctly.
    """
    for entry in entries:
        out.append(len(messages))
        yield entry


def _transform_entries(
    entries,
    sdk_session_id: str,
    working_dir: str,
    agent_children_loader=None,
    *,
    entry_message_boundaries: Optional[list] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Transform raw transcript entries into chat messages + usage data.

    This is the PURE, source-agnostic core shared by the JSONL file path and
    the Postgres session-store path. ``entries`` is any iterable of raw
    transcript dicts; the transform is byte-identical regardless of where the
    entries came from. Subagent child lookup is delegated to
    ``agent_children_loader`` so each source can resolve children its own way.

    Args:
        entries: Iterable of raw transcript dicts (JSONL lines or store rows).
        sdk_session_id: The Claude session ID (used for ids/logging).
        working_dir: The working directory (passed through for context).
        agent_children_loader: Optional ``callable(agent_id) -> list[dict] | None``
            returning the parsed child messages for a subagent. Returns ``None``
            when the agent's transcript can't be found. To match the original
            consume-once semantics (an agent's children attach to at most one
            subagent message), the loader is expected to return the children on
            the first call for an agent_id and ``[]`` (or already-consumed) on
            subsequent calls. When ``None``, subagent messages carry no children.
        entry_message_boundaries: Optional list, filled in place with one int
            per entry: the index in the returned ``messages`` where that entry's
            output starts (see :func:`_track_entry_boundaries`). Purely
            observational — passing it does not change a single produced
            message — and used by the windowed store read to map an entry range
            back to the messages it produced.

    Returns:
        Tuple of (messages, usage_data) — identical shape to the legacy parser.
    """
    if agent_children_loader is None:
        def agent_children_loader(_agent_id):  # noqa: ANN001
            return None

    messages: List[Dict[str, Any]] = []
    msg_counter = 0

    if entry_message_boundaries is not None:
        entries = _track_entry_boundaries(entries, messages, entry_message_boundaries)

    # Track tool names by ID for matching tool_result to tool_use
    tool_names_by_id: Dict[str, str] = {}

    # Dict-based index: toolId → index in messages list for O(1) lookup
    messages_by_tool_id: Dict[str, int] = {}

    # Track Task tool uses that have been converted to subagent messages
    # Maps tool_use_id to agent info for matching with tool_result
    task_tool_uses: Dict[str, Dict[str, Any]] = {}

    # Usage accumulation (replaces separate get_session_cost_from_jsonl call)
    total_input_tokens = 0
    total_output_tokens = 0
    usage_model = None
    last_usage = {}
    # API message id -> the usage last counted for it (see _count_usage_once).
    usage_by_message_id: Dict[str, Dict[str, Any]] = {}

    for entry in entries:
        entry_type = entry.get("type")

        # Skip non-message entries
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue

        # Accumulate usage data from all assistant entries
        if entry_type == "assistant":
            message_data_for_usage = entry.get("message") or {}
            usage = message_data_for_usage.get("usage") or {}
            if not usage_model:
                usage_model = message_data_for_usage.get("model")
            d_in, d_out = _count_usage_once(
                message_data_for_usage.get("id"), usage, usage_by_message_id
            )
            total_input_tokens += d_in
            total_output_tokens += d_out
            # Only track last_usage from main conversation turns (not subagents/sidechain).
            # last_usage represents the current context window size, so it must come from
            # the main conversation — subagent usage reflects a different context window.
            if usage and not entry.get("isMeta") and not entry.get("isSidechain"):
                last_usage = usage

        # Skip meta messages (slash commands, system prompts, etc.)
        if entry.get("isMeta"):
            continue

        # Skip sidechain messages (alternate conversation branches)
        if entry.get("isSidechain"):
            continue

        message_data = entry.get("message", {})
        role = message_data.get("role")
        content = message_data.get("content")
        timestamp = entry.get("timestamp")
        # ``or`` rather than a .get default: an explicit ``"uuid": null`` must
        # fall back too, or every such entry gets the SAME id (None).
        uuid = entry.get("uuid") or f"msg-{msg_counter}"

        # Handle system messages (e.g., compact_boundary, worktree_context, plugin_warning)
        if entry_type == "system":
            subtype = entry.get("subtype")
            # Drop internal/transient system subtypes that live streaming routes
            # into other UI elements (thinking counter, subagent progress) and
            # never renders as a card. Filtering on reload matches live behavior.
            if is_hidden_system_subtype(subtype):
                continue
            system_content = entry.get("content", "")
            # Claude system messages store fields at the top level of the entry,
            # not under a nested "data" key. Pass the entire entry as systemData
            # so the frontend has access to all fields (e.g., compactMetadata,
            # durationMs, permissionMode, etc.)
            system_data = entry.get("data") or entry
            msg_counter += 1

            # Normalize field names: JSONL uses camelCase (compactMetadata),
            # but the frontend expects snake_case (compact_metadata)
            if subtype == "compact_boundary" and "compactMetadata" in system_data:
                cm = system_data["compactMetadata"]
                system_data = dict(system_data)
                system_data["compact_metadata"] = {
                    "trigger": cm.get("trigger"),
                    "pre_tokens": cm.get("preTokens")
                }
            elif subtype == "microcompact_boundary" and "microcompactMetadata" in system_data:
                mm = system_data["microcompactMetadata"]
                system_data = dict(system_data)
                system_data["microcompactMetadata"] = {
                    "trigger": mm.get("trigger"),
                    "tokensSaved": mm.get("tokensSaved"),
                    "preTokens": mm.get("preTokens")
                }

            messages.append({
                "id": uuid,
                "uuid": uuid,  # Raw JSONL UUID for rewind
                "role": "system",
                "content": system_content,
                "type": "system",
                "subtype": subtype,
                "systemSubtype": subtype,  # Frontend compatibility
                "systemData": system_data,  # Pass through structured data
                "metadata": {
                    "timestamp": timestamp,
                    "compactMetadata": entry.get("compactMetadata")
                },
                "streaming": False
            })
            continue

        # Explicit hook_event mapping — keeps field names aligned with the
        # live stream contract so replay is visually identical to live events.
        if entry_type == "hook_event":
            msg_counter += 1
            messages.append({
                "id": uuid,
                "uuid": uuid,
                "role": "system",
                "content": "",
                "type": "system",
                "subtype": "hook_event",
                "systemSubtype": "hook_event",
                "systemData": {
                    "hook_event_name": entry.get("hook_event_name"),
                    "matcher": entry.get("matcher"),
                    "decision": entry.get("decision"),
                    "reason": entry.get("reason"),
                    "system_message": entry.get("system_message"),
                    "tool_name": entry.get("tool_name"),
                    "session_id": entry.get("session_id"),
                    "timestamp": entry.get("timestamp") or timestamp,
                },
                "metadata": {"timestamp": timestamp},
                "streaming": False,
            })
            continue

        if entry_type == "user" and role == "user":
            # User message - can be plain text, tool results, or array with text blocks
            if isinstance(content, str):
                # Check for slash command (e.g., /commit, /ralph-loop)
                slash_cmd = _parse_slash_command(content)
                if slash_cmd is not None:
                    msg_counter += 1
                    # Format display content: "/command args" or just "/command"
                    display_content = slash_cmd.get("command_name", "")
                    if slash_cmd.get("command_args"):
                        display_content = f"{display_content} {slash_cmd['command_args']}"
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "user",
                        "content": display_content,
                        "type": "command",
                        "commandId": slash_cmd.get("command_id"),
                        "commandName": slash_cmd.get("command_name"),
                        "commandArgs": slash_cmd.get("command_args"),
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                    continue

                # Check for local command output (e.g., /context, /compact)
                local_output = _extract_local_command_output(content)
                if local_output is not None:
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "system",
                        "content": local_output,
                        "type": "system",
                        "subtype": "local_command",
                        "systemSubtype": "local_command",  # Frontend compatibility
                        "systemData": {"content": local_output},  # Frontend uses data.content for rendering
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                    continue

                # A terminal task-notification marks a backgrounded sub-agent done.
                # Flip the matching sub-agent card (by launch tool-use-id) to its
                # terminal status so a reload reflects reality instead of a stuck
                # "running"; otherwise these SDK-internal callbacks aren't shown.
                if _is_task_notification(content):
                    _apply_task_notification_status(content, messages, messages_by_tool_id)
                    continue

                # Plain user message - skip empty content and system/command-related messages
                if content and not _is_system_content(content):
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "user",
                        "content": content,
                        "type": None,
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
            elif isinstance(content, list):
                # Array content - could be tool results or text blocks
                has_text_content = False
                text_parts = []

                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            msg_counter += 1
                            tool_result = entry.get("toolUseResult")
                            raw_content = block.get("content", "")
                            is_error = block.get("is_error", False)
                            tool_use_id = block.get("tool_use_id")

                            # Reconstruct any inline screenshot from MCP image
                            # content blocks so reloaded sessions show the
                            # screenshot below the tool call, just like live. The
                            # image lives in the tool_result content blocks (and,
                            # for some tools, in toolUseResult.content).
                            #
                            # Bytes already in the artifact store resolve to
                            # ``/api/v1/artifacts/<hash>`` here rather than to a
                            # multi-megabyte data URI that the API layer would
                            # rewrite and discard; anything the store does not
                            # have still comes back inline (see
                            # :func:`extract_screenshot_ref`).
                            screenshot = extract_screenshot_ref(raw_content)
                            if not screenshot and isinstance(tool_result, dict):
                                screenshot = extract_screenshot_ref(tool_result.get("content"))

                            # Handle content that can be string or list of content blocks
                            # Some tool results have content as [{'type': 'text', 'text': '...'}]
                            if isinstance(raw_content, list):
                                output = extract_text_from_content(raw_content)
                            else:
                                output = raw_content if isinstance(raw_content, str) else ""

                            # Get output from toolUseResult if available
                            # toolUseResult can have different formats depending on the tool:
                            # - Bash: {"stdout": "...", "stderr": "...", "is_error": bool}
                            # - Read: {"type": "text", "file": {"filePath": "...", "content": "..."}}
                            # - Other tools: may be a string directly
                            if tool_result and isinstance(tool_result, dict):
                                # Handle Bash-style results with stdout/stderr
                                if "stdout" in tool_result or "stderr" in tool_result:
                                    stdout = tool_result.get("stdout", "")
                                    stderr = tool_result.get("stderr", "")
                                    output = stdout
                                    if stderr:
                                        output = f"{stdout}\n{stderr}" if stdout else stderr
                                    is_error = is_error or tool_result.get("is_error", False)
                                # Handle Read-style results with file content
                                elif tool_result.get("type") == "text" and "file" in tool_result:
                                    file_info = tool_result.get("file", {})
                                    file_content = file_info.get("content", "")
                                    file_path = file_info.get("filePath", "")
                                    # Truncate file content early - images/large files can be huge
                                    file_content = _truncate_for_display(file_content)
                                    # Format like the streaming version does
                                    if file_path and file_content:
                                        output = f"File: {file_path}\n{file_content}"
                                    elif file_content:
                                        output = file_content
                                # Handle other dict-based results
                                elif tool_result.get("content"):
                                    tr_content = tool_result.get("content", "")
                                    # Content can be string or list of content blocks
                                    if isinstance(tr_content, list):
                                        output = _truncate_for_display(extract_text_from_content(tr_content))
                                    elif isinstance(tr_content, str):
                                        output = _truncate_for_display(tr_content)
                                    else:
                                        output = _truncate_for_display(str(tr_content))
                                elif tool_result.get("result"):
                                    output = _truncate_for_display(str(tool_result.get("result", "")))
                            elif tool_result and isinstance(tool_result, str):
                                # Sometimes toolUseResult is just a string (error messages)
                                output = _truncate_for_display(tool_result)

                            # Check if this is a result for a Task tool (subagent)
                            if tool_use_id in task_tool_uses:
                                # This is a subagent result - update the existing subagent message
                                _task_info = task_tool_uses[tool_use_id]

                                # Extract agentId from toolUseResult if available
                                tool_use_result = entry.get("toolUseResult", {})
                                agent_id = tool_use_result.get("agentId") if isinstance(tool_use_result, dict) else None

                                # O(1) lookup for the subagent message
                                msg_idx = messages_by_tool_id.get(tool_use_id)
                                if msg_idx is not None and msg_idx < len(messages):
                                    msg = messages[msg_idx]
                                    msg["content"] = output[:2000] if output else ""
                                    if is_error:
                                        msg["agentStatus"] = "error"
                                    elif _is_task_launch_rejection(output):
                                        # Dispatcher rejected the launch before spawning
                                        # a worker — terminal, never an ACK.
                                        msg["agentStatus"] = "error"
                                    elif _is_background_subagent_ack(_task_info, output):
                                        # Backgrounded sub-agent still running — this
                                        # result is only the launch-ACK. Keep RUNNING so
                                        # reload doesn't claim premature completion; the
                                        # terminal <task-notification> flips it to done.
                                        msg["agentStatus"] = "running"
                                    else:
                                        msg["agentStatus"] = "completed"

                                    # Lazy-load agent children via the injected
                                    # loader (file- or store-backed). The loader
                                    # owns the consume-once semantics so an
                                    # agent's children attach to one subagent.
                                    if agent_id:
                                        children = agent_children_loader(agent_id)
                                        if children is not None:
                                            msg["agentId"] = agent_id
                                            msg["agentChildren"] = children

                                # Don't add a separate tool_result message for Task tools
                                continue

                            # Group tool result with its corresponding tool_use message
                            # O(1) lookup instead of linear scan
                            found_tool_use = False
                            msg_idx = messages_by_tool_id.get(tool_use_id)
                            if msg_idx is not None and msg_idx < len(messages):
                                msg = messages[msg_idx]
                                if msg.get("type") == "tool_use":
                                    msg["toolResult"] = output[:2000] if output else ""
                                    if screenshot:
                                        msg["toolScreenshot"] = screenshot
                                    if is_error:
                                        msg["toolStatus"] = "error"
                                    elif _is_background_ack_result(msg, output):
                                        # Backgrounded command still executing —
                                        # keep it RUNNING so reload doesn't claim
                                        # premature completion (matches live store).
                                        msg["toolStatus"] = "running"
                                    else:
                                        msg["toolStatus"] = "complete"
                                    found_tool_use = True

                            # Only create separate tool_result if no matching tool_use found
                            if not found_tool_use:
                                messages.append({
                                    "id": f"result-{uuid}-{tool_use_id}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",  # Display as assistant for UI consistency
                                    "content": output[:2000] if output else "",  # Truncate like streaming
                                    "type": "tool_result",
                                    "toolId": tool_use_id,
                                    "toolName": tool_names_by_id.get(tool_use_id),  # Match to tool_use
                                    "metadata": {
                                        "timestamp": timestamp,
                                        "is_error": is_error
                                    },
                                    "streaming": False
                                })
                        elif block.get("type") == "text":
                            # Text block in user content array
                            text = block.get("text", "")
                            if text and not _is_system_content(text) and not _is_task_notification(text):
                                has_text_content = True
                                text_parts.append(text)

                # If we collected text blocks, create a single user message
                if has_text_content and text_parts:
                    combined_text = "\n".join(text_parts)
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "user",
                        "content": combined_text,
                        "type": None,
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })

        elif entry_type == "assistant" and role == "assistant":
            # Check for API error messages (synthetic messages from the CLI)
            # These have isApiErrorMessage: true and model: "<synthetic>"
            if entry.get("isApiErrorMessage"):
                # Extract the error text from the content
                error_text = ""
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            error_text = block.get("text", "")
                            break
                elif isinstance(content, str):
                    error_text = content

                if error_text:
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "system",
                        "content": error_text,
                        "type": "system",
                        "subtype": "api_error",
                        "systemSubtype": "api_error",  # Frontend compatibility
                        "systemData": {"content": error_text},
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                continue

            # Assistant message - contains thinking, text blocks, and tool use blocks
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")

                        if block_type == "thinking":
                            # Extended thinking block
                            thinking_text = block.get("thinking", "")
                            if thinking_text:  # Only add non-empty thinking blocks
                                msg_counter += 1
                                messages.append({
                                    "id": f"thinking-{uuid}-{msg_counter}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": thinking_text,
                                    "type": "thinking",
                                    "metadata": {
                                        "timestamp": timestamp,
                                        "model": message_data.get("model")
                                    },
                                    "streaming": False
                                })

                        elif block_type == "text":
                            text = block.get("text", "")
                            if text and text.strip():  # Only add non-empty, non-whitespace text blocks
                                msg_counter += 1
                                messages.append({
                                    "id": f"text-{uuid}-{msg_counter}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": text,
                                    "type": "text",
                                    "metadata": {
                                        "timestamp": timestamp,
                                        "model": message_data.get("model")
                                    },
                                    "streaming": False
                                })

                        elif block_type == "tool_use":
                            msg_counter += 1
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})

                            # Track tool name by ID for matching tool results
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name

                            # Check if this is a subagent tool - create subagent message instead
                            # SDK ≤0.1.30 uses "Task", SDK ≥0.1.48 uses "Agent"
                            if tool_name in ("Task", "Agent"):
                                agent_type = (
                                    tool_input.get("subagent_type")
                                    or tool_input.get("description")
                                    or "Agent"
                                )
                                description = tool_input.get("description", "")
                                prompt = tool_input.get("prompt", "")

                                # Track this Task tool use for later matching with result
                                task_tool_uses[tool_id] = {
                                    "agent_type": agent_type,
                                    "description": description,
                                    "prompt": prompt,
                                    # Backgrounded sub-agents (run_in_background) return
                                    # only a launch-ACK as their tool_result; capture the
                                    # flag so the result branch keeps the card RUNNING
                                    # instead of marking premature completion.
                                    "run_in_background": bool(
                                        isinstance(tool_input, dict)
                                        and (
                                            tool_input.get("run_in_background")
                                            or tool_input.get("runInBackground")
                                        )
                                    ),
                                }

                                # For now, we don't have the agent_id from tool_use alone
                                # We'll need to match it from tool_result or find a pattern
                                # Create a placeholder subagent message
                                messages.append({
                                    "id": f"subagent-{tool_id}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": "",
                                    "type": "subagent",
                                    "toolId": tool_id,
                                    "toolInput": tool_input,
                                    "agentType": agent_type,
                                    "agentDescription": description,
                                    "agentPrompt": prompt,
                                    "agentStatus": "pending",  # Will be updated when we find the result
                                    "agentChildren": [],
                                    "metadata": {"timestamp": timestamp},
                                    "streaming": False
                                })
                                if tool_id:
                                    messages_by_tool_id[tool_id] = len(messages) - 1
                            else:
                                messages.append({
                                    # Use the raw tool_id (matching the live
                                    # store's `tool-{tool_id}`) so a tool card
                                    # keeps a STABLE id across the streaming →
                                    # persisted handoff and the frontend dedup
                                    # doesn't drop/duplicate it. Fall back to the
                                    # per-message counter only when the SDK
                                    # omitted a tool id. The rewind `uuid` field
                                    # below is separate and unchanged.
                                    "id": f"tool-{tool_id}" if tool_id else f"tool-{uuid}-{msg_counter}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": "",
                                    "type": "tool_use",
                                    "toolName": tool_name,
                                    "toolId": tool_id,
                                    "toolInput": tool_input,
                                    "toolStatus": "running",  # Will be updated when result arrives
                                    "metadata": {"timestamp": timestamp},
                                    "streaming": False
                                })
                                if tool_id:
                                    messages_by_tool_id[tool_id] = len(messages) - 1

                        elif block_type == "server_tool_use":
                            # SDK 0.1.65+: server-executed tool calls (advisor,
                            # web_search, web_fetch). Render identically to a
                            # tool_use with a server_side badge; we never get a
                            # caller-side result, only the matching
                            # server_tool_result block below.
                            msg_counter += 1
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name
                            messages.append({
                                "id": f"tool-{uuid}-{tool_id or msg_counter}",
                                "uuid": uuid,
                                "role": "assistant",
                                "content": "",
                                "type": "tool_use",
                                "toolName": tool_name,
                                "toolId": tool_id,
                                "toolInput": tool_input,
                                "toolStatus": "running",
                                "serverSide": True,
                                "metadata": {"timestamp": timestamp},
                                "streaming": False
                            })
                            if tool_id:
                                messages_by_tool_id[tool_id] = len(messages) - 1

                        elif block_type == "server_tool_result":
                            # SDK 0.1.65+: paired with server_tool_use above.
                            # Content is a dict, not a string. Attach to the
                            # prior tool_use row if present, else emit standalone.
                            tool_use_id = block.get("tool_use_id")
                            raw_content = block.get("content")
                            if isinstance(raw_content, (dict, list)):
                                import json as _json
                                output_text = _json.dumps(raw_content)
                            else:
                                output_text = str(raw_content) if raw_content is not None else ""
                            if tool_use_id and tool_use_id in messages_by_tool_id:
                                idx = messages_by_tool_id[tool_use_id]
                                messages[idx]["toolResult"] = output_text
                                messages[idx]["toolStatus"] = "complete"
                            else:
                                # Emit as a synthetic tool_use row with the
                                # result pre-attached. MessageArea.svelte
                                # unconditionally skips `type: "tool_result"`
                                # (standalone results are expected to merge
                                # into a prior tool_use), so a standalone
                                # tool_result here would silently render
                                # nothing. Rendering through the tool_use
                                # path preserves the server badge and result
                                # panel. ToolUseMessage.svelte gates its
                                # input section on Object.keys(input).length,
                                # so an empty toolInput renders cleanly.
                                msg_counter += 1
                                messages.append({
                                    "id": f"tool-{uuid}-{tool_use_id or msg_counter}",
                                    "uuid": uuid,
                                    "role": "assistant",
                                    "content": "",
                                    "type": "tool_use",
                                    "toolName": tool_names_by_id.get(tool_use_id, "server_tool"),
                                    "toolId": tool_use_id,
                                    "toolInput": {},
                                    "toolResult": output_text,
                                    "toolStatus": "complete",
                                    "serverSide": True,
                                    "metadata": {"timestamp": timestamp},
                                    "streaming": False
                                })

            elif isinstance(content, str) and content:
                # Plain string content (less common)
                msg_counter += 1
                messages.append({
                    "id": f"text-{uuid}",
                    "uuid": uuid,  # Raw JSONL UUID for rewind
                    "role": "assistant",
                    "content": content,
                    "type": "text",
                    "metadata": {
                        "timestamp": timestamp,
                        "model": message_data.get("model")
                    },
                    "streaming": False
                })

    logger.debug(f"Transformed {len(messages)} messages from {len(messages)} transcript entries")

    # Build usage_data from accumulated values
    usage_data = {}
    if total_input_tokens or total_output_tokens or last_usage:
        usage_data = {
            "total_tokens_in": total_input_tokens,
            "total_tokens_out": total_output_tokens,
            "cache_creation_tokens": last_usage.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": last_usage.get("cache_read_input_tokens", 0),
            "last_input_tokens": last_usage.get("input_tokens", 0),
            "model": usage_model
        }

    return messages, usage_data


def _count_usage_once(
    message_id: Any,
    usage: Dict[str, Any],
    seen: Dict[str, Dict[str, Any]],
) -> Tuple[int, int]:
    """``(input, output)`` token deltas to add for one assistant entry.

    The CLI writes ONE line per content block of an API response (thinking,
    text, each tool_use), and every one of those lines repeats the response's
    full ``usage`` under the same ``message.id``. Summing per line counted a
    response once per block. Count each ``message.id`` once: a repeat replaces
    the previously counted usage (so a later, larger streaming snapshot wins)
    instead of adding to it. Entries with no id are counted as they come.
    """
    d_in = usage.get("input_tokens", 0) or 0
    d_out = usage.get("output_tokens", 0) or 0
    if isinstance(message_id, str) and message_id and usage:
        prev = seen.get(message_id)
        if prev is not None:
            d_in -= prev.get("input_tokens", 0) or 0
            d_out -= prev.get("output_tokens", 0) or 0
        seen[message_id] = usage
    return d_in, d_out


def _merge_hook_messages(
    messages: List[Dict[str, Any]],
    sdk_session_id: str,
    working_dir: str,
) -> List[Dict[str, Any]]:
    """Merge plugin-warning hook messages (from ``<sid>.hooks.jsonl``) into the
    timeline using an O(N+M) timestamp merge.

    JSONL-source only. In ``postgres_only`` mode hooks JSONL is NOT mirrored,
    so this is intentionally skipped — a known, accepted parity gap (hook
    warnings simply aren't rendered on reload). See PR C.1 / follow-up.
    """
    hook_messages = parse_hooks_jsonl(sdk_session_id, working_dir)
    if not hook_messages:
        return messages
    logger.debug(f"Merging {len(hook_messages)} hook messages from hooks.jsonl")
    # Both lists are roughly in timestamp order — merge them
    merged: List[Dict[str, Any]] = []
    mi, hi = 0, 0
    while mi < len(messages) and hi < len(hook_messages):
        msg_ts = messages[mi].get("metadata", {}).get("timestamp", "")
        hook_ts = hook_messages[hi].get("metadata", {}).get("timestamp", "")
        if hook_ts and msg_ts and hook_ts < msg_ts:
            merged.append(hook_messages[hi])
            hi += 1
        else:
            merged.append(messages[mi])
            mi += 1
    merged.extend(messages[mi:])
    merged.extend(hook_messages[hi:])
    logger.debug(f"Total messages after merging hooks: {len(merged)}")
    return merged


def _make_file_agent_children_loader(sdk_session_id: str, working_dir: str):
    """Build a consume-once subagent-children loader backed by JSONL files.

    Mirrors the legacy lazy-load + clear-after-consume behavior so an agent's
    children attach to exactly one subagent message. Agent files are discovered
    once; each file is parsed on first request and the result handed out a
    single time (subsequent requests for the same agent_id return ``[]``).
    """
    agent_files = get_agent_jsonl_paths(sdk_session_id, working_dir)
    cache: Dict[str, List[Dict[str, Any]]] = {}

    def _loader(agent_id: str):
        if agent_id not in cache and agent_id in agent_files:
            cache[agent_id] = parse_agent_history(agent_files[agent_id])
        if agent_id in cache:
            children = cache[agent_id]
            cache[agent_id] = []  # consume-once
            return children
        return None

    return _loader


def _parse_session_history_uncached(
    sdk_session_id: str,
    working_dir: str,
    jsonl_path: Path
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Parse a session's JSONL history file and return messages + usage data.

    Reads transcript entries from the JSONL file and runs them through the
    shared :func:`_transform_entries`, then merges plugin-warning hook messages
    from the sidecar hooks JSONL. Behavior-preserving wrapper around the
    refactored core.

    Args:
        sdk_session_id: The Claude session ID
        working_dir: The working directory for finding the project
        jsonl_path: Path to the JSONL file (caller is responsible for resolving this)

    Returns:
        Tuple of (messages, usage_data) — see :func:`_transform_entries`.
    """
    logger.debug(f"Parsing JSONL history from {jsonl_path}")

    agent_children_loader = _make_file_agent_children_loader(sdk_session_id, working_dir)

    messages, usage_data = _transform_entries(
        parse_jsonl_file(jsonl_path),
        sdk_session_id,
        working_dir,
        agent_children_loader=agent_children_loader,
    )

    # Merge plugin warning messages from the hooks JSONL sidecar (file-only).
    messages = _merge_hook_messages(messages, sdk_session_id, working_dir)

    return messages, usage_data


def _make_store_agent_children_loader(sdk_session_id: str, store_loader):
    """Build a consume-once subagent-children loader backed by the session store.

    ``store_loader`` is a synchronous callable
    ``store_loader(session_id, subpath=None) -> list[dict]`` that returns raw
    transcript entries from ``session_store_entries``. Subagent transcripts live
    under ``subpath="subagents/agent-<agent_id>"``. Children are parsed on first
    request and handed out a single time (consume-once), mirroring the file
    loader so an agent's children attach to exactly one subagent message.
    """
    cache: Dict[str, List[Dict[str, Any]]] = {}
    loaded: set = set()

    def _loader(agent_id: str):
        if agent_id not in loaded:
            loaded.add(agent_id)
            subpath = f"subagents/agent-{agent_id}"
            try:
                entries = store_loader(sdk_session_id, subpath) or []
            except Exception as e:
                logger.warning(
                    f"Store subagent load failed for {sdk_session_id} {subpath}: {e}"
                )
                entries = []
            cache[agent_id] = transform_agent_entries(entries) if entries else []
        if agent_id in cache:
            children = cache[agent_id]
            cache[agent_id] = []  # consume-once
            return children
        return None

    return _loader


def parse_session_history_from_store(
    sdk_session_id: str,
    working_dir: str,
    store_loader,
    *,
    use_cache: bool = True,
    store_version_loader=None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse a session's transcript from the Postgres session store.

    The ``postgres_only`` analogue of :func:`parse_session_history`. Loads the
    main transcript entries (``subpath IS NULL``) from the store and runs them
    through the SAME :func:`_transform_entries` core, so the resulting
    messages/usage are identical to feeding the same entries through the JSONL
    path. Subagent children are pulled from the store (``subpath="subagents/
    agent-<id>"``) via :func:`_make_store_agent_children_loader`.

    Hooks JSONL is intentionally NOT merged — it is not mirrored into the store
    (a known, accepted parity gap; see PR C.1).

    Args:
        sdk_session_id: The Claude session ID.
        working_dir: The working directory (passed through for context only;
            NOT used to reconstruct a project_key — the store reads match on
            session_id alone).
        store_loader: Synchronous callable
            ``store_loader(session_id, subpath=None) -> list[dict]`` returning
            raw ``entry`` dicts ordered by id ASC.
        use_cache: When True (default) use the dedicated ``_store_parse_cache``
            namespace (kept separate from the JSONL cache).
        store_version_loader: Optional ``loader(session_id) -> (count, max_id)``
            returning the cheap store version stamp. When omitted the sync DB
            helper ``_store_version_by_session_id_sync`` is used. Injected by
            tests to avoid a live Postgres, and by callers that already hold a
            store handle.

    Returns:
        Tuple of (messages, usage_data) — see :func:`_transform_entries`.

    Cache validity note: ``_store_parse_cache`` has no on-disk mtime to
    self-invalidate, so each entry is stamped with the store's ``(count,
    max_id)`` version captured at parse time and re-validated on read. Because
    ``session_store_entries`` is an append-only log SHARED across installs via
    Postgres, this pair changes on any insert (a new turn) or delete (a rewind)
    — including a rewind performed on a DIFFERENT install. Install B therefore
    stops serving a pre-rewind transcript on the very next read instead of only
    after LRU eviction / restart (the cross-install "rewind still broken" bug).
    Entries primed before stamping existed (legacy 2-tuples) are treated stale.
    """
    # Resolve the current store version ONCE per call. Reused to (a) validate a
    # cache hit and (b) stamp a fresh write, so both see the same snapshot.
    current_version = None
    if use_cache:
        current_version = _resolve_store_version(sdk_session_id, store_version_loader)
        with _store_parse_cache_lock:
            cached = _store_parse_cache.get(sdk_session_id)
            if cached is not None:
                # Only a stamped 3-tuple whose version still matches the store is
                # safe to serve verbatim. Legacy 2-tuples carry no stamp (may hold
                # a pre-rewind transcript) and an unresolvable/mismatched version
                # both mean "cannot prove fresh" — drop and re-parse in every such
                # case so a stale entry can never be served again.
                if (
                    len(cached) == 3
                    and current_version is not None
                    and cached[2] == current_version
                ):
                    _store_parse_cache.move_to_end(sdk_session_id)
                    c_messages, c_usage, _ = cached
                    # Legacy JSON-string entries (older build, or a test
                    # planting the old shape) are re-parsed rather than
                    # served: the value shape is part of the cache contract.
                    if isinstance(c_messages, str) or isinstance(c_usage, str):
                        _store_parse_cache.pop(sdk_session_id, None)
                    else:
                        return _copy_messages(c_messages), dict(c_usage)
                else:
                    _store_parse_cache.pop(sdk_session_id, None)

    main_entries = store_loader(sdk_session_id, None) or []

    agent_children_loader = _make_store_agent_children_loader(
        sdk_session_id, store_loader
    )

    messages, usage_data = _transform_entries(
        main_entries,
        sdk_session_id,
        working_dir,
        agent_children_loader=agent_children_loader,
    )

    # Only cache when we could stamp a version — an unstamped write would just be
    # treated as stale on the next read (pointless) and could not invalidate
    # cross-install. When the version is unresolvable we re-parse each time: safe,
    # just slower.
    if use_cache and current_version is not None:
        with _store_parse_cache_lock:
            _store_parse_cache[sdk_session_id] = (
                _copy_messages(messages),
                dict(usage_data),
                current_version,
            )

    return messages, usage_data


# ─── Windowed store reads ───────────────────────────────────────────────────
# Pagination used to live entirely in the API layer: the store materialised the
# WHOLE transcript, this module transformed all of it, and only then was a
# window sliced off the end and sent. A long session paid a full table read, a
# full parse and a full transform to render its newest page — every open.
#
# ``parse_session_window_from_store`` pushes the window down to the SQL: it asks
# the store for one page of ENTRIES (plus a small, constant merge context) and
# transforms only those.
#
# The cache trap: ``_store_parse_cache`` is keyed by ``sdk_session_id`` and its
# value is "the whole transcript". A windowed result put under that key would be
# served to the next FULL reader as if it were the entire session — the
# transcript would silently lose its history. And a full transcript served to a
# windowed reader would defeat the point of the window. So windowed results live
# under their own composite key, in the SAME cache (so they share its byte
# budget and eviction), and the two shapes can never be confused:
#
#     full read   -> key  sdk_session_id            value (messages, usage, version)
#     window read -> key  ("window", sid, limit, before_seq)
#                                              value (messages, usage, pagination, version)
#
# Both are stamped with the same ``(count, max_id)`` store version, so a new turn
# or a rewind (including one performed on another install) drops both.


def _window_cache_key(sdk_session_id: str, limit: int, before_seq) -> tuple:
    """Cache key for one windowed parse — never collides with the full-read key.

    The full-transcript entry is keyed by the bare ``sdk_session_id`` (a str);
    every windowed entry is keyed by a tuple whose first element is the literal
    ``"window"``. A str and a tuple can never be equal, so no windowed result
    can be served to (or evict-by-overwrite) the full-transcript entry.
    """
    return ("window", sdk_session_id, int(limit), before_seq)


def _drop_window_cache_entries(sdk_session_id: Optional[str]) -> None:
    """Drop windowed cache entries for one session (or all of them).

    Call with the ``_store_parse_cache`` lock held. Windowed keys are tuples, so
    they are not reachable by the bare-``sdk_session_id`` pop that clears the
    full-transcript entry — they have to be swept explicitly or a stale page
    would outlive the invalidation that dropped the full parse.
    """
    for key in _store_parse_cache.keys():
        if not isinstance(key, tuple) or not key or key[0] != "window":
            continue
        if sdk_session_id is None or key[1] == sdk_session_id:
            _store_parse_cache.pop(key, None)


def _window_pagination(
    total: int, window_start: int, window_end: int, page_limit: int, returned_messages: int
) -> Dict[str, Any]:
    """Build the window descriptor returned alongside a page.

    UNITS: every ``seq`` here — ``total``, ``returned``, ``first_seq``,
    ``next_before_seq`` — counts TRANSCRIPT ENTRIES, not rendered messages. That
    is forced by where the window is now applied: the store can only page rows,
    and one entry can produce zero, one or several messages (an assistant entry
    with two text blocks and a tool call is three messages; a tool_result entry
    that merges into an earlier card is none). Counting messages instead would
    mean reading the whole transcript to find out how many there are, which is
    exactly the cost being removed.

    ``messages_returned`` carries the true message count for the page, and
    ``unit`` names the space so a consumer never has to guess.
    """
    empty = window_end <= window_start
    return {
        "total": total,
        "returned": max(0, window_end - window_start),
        "limit": page_limit,
        "has_more": window_start > 0,
        "first_seq": None if empty else window_start,
        "last_seq": None if empty else window_end - 1,
        "next_before_seq": window_start if window_start > 0 else None,
        "has_more_after": window_end < total,
        "messages_returned": returned_messages,
        "unit": "entries",
    }


#: Entry types whose output messages carry the entry's uuid (or, without one,
#: the counter fallback) as their identity. See :func:`_entries_are_window_safe`.
_WINDOW_ID_ENTRY_TYPES = frozenset({"user", "assistant", "system", "hook_event"})


def _entries_are_window_safe(entries) -> bool:
    """True when a windowed parse of ``entries`` yields trustworthy identities.

    The transform falls back to ``msg-<running counter>`` for an entry with no
    ``uuid``, and that counter depends on how many entries were processed
    BEFORE it. In a full parse the fallback is stable; in a windowed parse the
    same entry would be labelled differently, and ``uuid`` is the anchor a
    REWIND resolves against — a mislabelled one could truncate a session at the
    wrong point. Real CLI entries always carry a uuid; the pre-cutover and
    hand-built ones that do not are rare and are worth the slower, whole-
    transcript path.
    """
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        # Only entries that can PRODUCE a message take an identity from the
        # counter. Bookkeeping lines the CLI writes without a uuid
        # (queue-operation, last-prompt, custom-title, file-history-*) render
        # nothing, so they cannot mislabel anything — refusing on them sent
        # nearly every real transcript down the whole-transcript path.
        if entry.get("type") in _WINDOW_ID_ENTRY_TYPES and not entry.get("uuid"):
            return False
    return True


def _window_merges_are_provable(
    page: List[Dict[str, Any]], load_start: int, load_end: int, total: int
) -> bool:
    """True when this page's tool cards cannot depend on unloaded entries.

    The merge context (``_WINDOW_MERGE_CONTEXT_ENTRIES``) covers the distance
    between a ``tool_use`` and its ``tool_result`` many times over, but "many
    times over" is not "always" — a backgrounded command can return thousands of
    entries later. This is the check that turns "almost certainly right" into
    "right, or slower": it looks for the two shapes a missed merge produces and
    reports whether the loaded range can account for them.

    * A free-standing ``tool_result`` card means the transform never found a
      matching ``tool_use``. That is genuine only if we read from the very
      beginning of the transcript; otherwise the ``tool_use`` may be in an entry
      we skipped, and a full parse would have MERGED the two — one card, not two.
    * A tool card with no result yet is genuine only if we read to the very end;
      otherwise its result may live past the entries we loaded, and a full parse
      would show it complete instead of running.

    Anything unproven sends the caller to the whole-transcript path, which is
    slower and certainly correct.
    """
    for msg in page:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "tool_result" and load_start > 0:
            return False
        if load_end >= total:
            continue
        if msg.get("type") in ("tool_use", "subagent") and "toolResult" not in msg:
            return False
    return True


def parse_session_window_from_store(
    sdk_session_id: str,
    working_dir: str,
    window_loader,
    store_loader,
    *,
    limit: int,
    before_seq: Optional[int] = None,
    use_cache: bool = True,
    store_version_loader=None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """Parse ONE window of a store-backed transcript — newest page first.

    The paginated counterpart of :func:`parse_session_history_from_store`, and
    the reason a long session no longer pays for its whole history on every
    open. Only the window's rows are read from Postgres and only they (plus a
    small constant merge context) are transformed.

    Args:
        sdk_session_id: The Claude session ID.
        working_dir: Passed through for context (never used to rebuild a
            project_key — store reads match on session_id alone).
        window_loader: ``loader(session_id, subpath=None, *, limit, before_seq)``
            returning the dict described by
            ``pg_session_store._load_entries_window_by_session_id_sync``:
            ``entries`` (id ASC, INCLUDING boundary context), ``total``,
            ``start``, ``window_start``, ``window_end``.
        store_loader: ``loader(session_id, subpath=None) -> list[dict]``
            returning a WHOLE transcript. Required, for two reasons: subagent
            child transcripts are always read whole (they are small), and it is
            the slower-but-correct path this function falls back to whenever a
            window cannot be proved equivalent to slicing a full parse.
        limit: Maximum ENTRIES in the window (>= 1).
        before_seq: Entry-seq cursor; ``None`` means the newest window.
        use_cache: Use the windowed namespace of ``_store_parse_cache``.
        store_version_loader: ``loader(session_id) -> (count, max_id)`` — the
            same cheap version stamp the full path uses.

    Returns:
        ``(messages, usage_data, pagination)``. ``messages`` is exactly the
        window's messages, oldest→newest; ``pagination`` is the entry-space
        descriptor from :func:`_window_pagination`.

    Equivalence with slicing a full parse — what is guaranteed and what is not:

    * Guaranteed: the page contains the same messages, in the same order, with
      the same ``uuid``, role, type, content, tool merges and metadata that a
      full parse of the whole transcript produces for that entry range. The
      merge context is what buys this at the window's edges.
    * NOT guaranteed: the synthesized ``id`` of assistant text/thinking cards
      (``text-<uuid>-<n>``), where ``n`` is the transform's running counter and
      therefore depends on how much transcript preceded the window. Ids stay
      unique (they carry the entry ``uuid``), but the SAME message read through
      a full parse and through a window gets different ``id`` values. Callers
      must not merge results from the two paths into one view.

    ``usage_data`` is likewise windowed: its cumulative totals cover the window
    only, and it carries ``"windowed": True`` so a caller can tell and prefer
    the session row's persisted totals.
    """
    page_limit = max(1, int(limit))
    cursor = before_seq if isinstance(before_seq, int) and not isinstance(before_seq, bool) else None

    current_version = None
    cache_key = _window_cache_key(sdk_session_id, page_limit, cursor)
    if use_cache:
        current_version = _resolve_store_version(sdk_session_id, store_version_loader)
        with _store_parse_cache_lock:
            cached = _store_parse_cache.get(cache_key)
            if cached is not None:
                if (
                    len(cached) == 4
                    and current_version is not None
                    and cached[3] == current_version
                    and not isinstance(cached[0], str)
                ):
                    _store_parse_cache.move_to_end(cache_key)
                    c_messages, c_usage, c_pagination, _ = cached
                    return (
                        _copy_messages(c_messages),
                        dict(c_usage),
                        dict(c_pagination),
                    )
                _store_parse_cache.pop(cache_key, None)

    agent_children_loader = _make_store_agent_children_loader(
        sdk_session_id, store_loader
    )

    def _full_parse_and_slice(reason: str):
        """The slower-but-certainly-correct path: parse everything, cut the
        same entry range out of it.

        A fresh subagent-children loader is built here on purpose: the one the
        fast path used has already CONSUMED the children of any subagent it
        rendered, and consume-once means a second read would come back empty.
        """
        logger.debug(
            "Windowed store parse for %s falling back to a full parse (%s)",
            sdk_session_id, reason,
        )
        all_entries = list(store_loader(sdk_session_id, None) or [])
        bounds: List[int] = []
        all_msgs, usage = _transform_entries(
            all_entries,
            sdk_session_id,
            working_dir,
            agent_children_loader=(
                _make_store_agent_children_loader(sdk_session_id, store_loader)
            ),
            entry_message_boundaries=bounds,
        )
        n = len(all_entries)
        end = n if cursor is None else max(0, min(cursor, n))
        start = max(0, end - page_limit)
        page = all_msgs[
            _boundary(bounds, start, len(all_msgs)):
            _boundary(bounds, end, len(all_msgs))
        ]
        return page, dict(usage), n, start, end

    window = window_loader(
        sdk_session_id, None, limit=page_limit, before_seq=cursor
    ) or {}
    entries = list(window.get("entries") or [])
    total = int(window.get("total") or 0)
    load_start = int(window.get("start") or 0)
    window_start = int(window.get("window_start") or 0)
    window_end = int(window.get("window_end") or 0)

    if entries and not _entries_are_window_safe(entries):
        # An entry with no uuid takes its identity from a running counter, and
        # that counter means something different inside a window.
        messages, usage_data, total, window_start, window_end = _full_parse_and_slice(
            "uuid-less entries"
        )
    else:
        boundaries: List[int] = []
        all_messages, usage_data = _transform_entries(
            entries,
            sdk_session_id,
            working_dir,
            agent_children_loader=agent_children_loader,
            entry_message_boundaries=boundaries,
        )
        # Drop the merge context: it was transformed only so the window's own
        # messages merge the way a full parse merges them.
        messages = all_messages[
            _boundary(boundaries, window_start - load_start, len(all_messages)):
            _boundary(boundaries, window_end - load_start, len(all_messages))
        ]
        usage_data = dict(usage_data)

        if not _window_merges_are_provable(
            messages, load_start, load_start + len(entries), total
        ):
            messages, usage_data, total, window_start, window_end = (
                _full_parse_and_slice("a tool card may merge outside the window")
            )

    usage_data["windowed"] = True
    pagination = _window_pagination(
        total, window_start, window_end, page_limit, len(messages)
    )

    if use_cache and current_version is not None:
        with _store_parse_cache_lock:
            _store_parse_cache[cache_key] = (
                _copy_messages(messages),
                dict(usage_data),
                dict(pagination),
                current_version,
            )

    return messages, usage_data, pagination


def _boundary(boundaries: List[int], index: int, total_messages: int) -> int:
    """Message index where entry ``index`` starts, clamped to the parse.

    ``boundaries[i]`` is only defined for entries that were actually
    transformed; ``index`` at or past the end means "everything that was
    produced", and a negative index means "from the beginning".
    """
    if index <= 0:
        return 0
    if index >= len(boundaries):
        return total_messages
    return boundaries[index]


def get_session_cost_from_jsonl(
    sdk_session_id: str,
    working_dir: str = "/workspace"
) -> Dict[str, Any]:
    """
    Extract cost/usage information from JSONL file.

    Note: JSONL files don't contain total cost, only per-message usage.
    This extracts what we can find.

    Returns dict with:
    - total_tokens_in: Input tokens (not including cache tokens)
    - total_tokens_out: Output tokens
    - cache_creation_tokens: Tokens used to create cache entries
    - cache_read_tokens: Tokens read from cache (doesn't count toward context)
    - model: The model used
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        return {}

    total_input_tokens = 0
    total_output_tokens = 0
    model = None

    # Track the last usage data - for cache tokens we want the final state
    last_usage = {}
    usage_by_message_id: Dict[str, Dict[str, Any]] = {}

    for entry in parse_jsonl_file(jsonl_path):
        if entry.get("type") == "assistant":
            message_data = entry.get("message") or {}
            usage = message_data.get("usage") or {}

            # Get model
            if not model:
                model = message_data.get("model")

            # Sum up input/output tokens (these are incremental per turn),
            # counting each API response once across its per-block lines.
            d_in, d_out = _count_usage_once(
                message_data.get("id"), usage, usage_by_message_id
            )
            total_input_tokens += d_in
            total_output_tokens += d_out

            # Keep track of latest usage for cache tokens
            if usage:
                last_usage = usage

    # Cache tokens from final message represent current cache state
    # - cache_creation_input_tokens: take from last message (represents final cache size)
    # - cache_read_input_tokens: take from last message (represents what was read last)
    # For context window calculation, we need last turn's input tokens (not cumulative)
    return {
        "total_tokens_in": total_input_tokens,
        "total_tokens_out": total_output_tokens,
        "cache_creation_tokens": last_usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": last_usage.get("cache_read_input_tokens", 0),
        # Last turn's input tokens for context calculation
        "last_input_tokens": last_usage.get("input_tokens", 0),
        "model": model
    }


def _get_session_title_and_preview(jsonl_path: Path) -> tuple[Optional[str], Optional[str], int]:
    """
    Extract session title (first user message) and preview from JSONL file.

    Returns:
        tuple of (title, preview, message_count)
    """
    title = None
    preview = None
    message_count = 0

    try:
        for entry in parse_jsonl_file(jsonl_path):
            entry_type = entry.get("type")

            # Skip non-message entries
            if entry_type in ("queue-operation", "file-history-snapshot", "system"):
                continue

            # Skip meta and sidechain messages
            if entry.get("isMeta") or entry.get("isSidechain"):
                continue

            message_data = entry.get("message", {})
            role = message_data.get("role")
            content = message_data.get("content")

            if entry_type == "user" and role == "user":
                if isinstance(content, str) and not _is_system_content(content) and not _is_task_notification(content):
                    message_count += 1
                    if title is None:
                        # First user message is the title
                        title = derive_session_title(content)
                elif isinstance(content, list):
                    # Check for text blocks
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text and not _is_system_content(text) and not _is_task_notification(text):
                                message_count += 1
                                if title is None:
                                    title = derive_session_title(text)
                                break

            elif entry_type == "assistant" and role == "assistant":
                message_count += 1
                # Get preview from first assistant response
                if preview is None and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                preview = text[:150].strip()
                                if len(text) > 150:
                                    preview = preview.rsplit(' ', 1)[0] + "..."
                                break

    except Exception as e:
        logger.warning(f"Failed to extract title/preview from {jsonl_path}: {e}")

    return title, preview, message_count


def list_chat_history_sessions(
    working_dir: str = "/workspace",
    search: str = "",
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    List chat history sessions with rich metadata for the @ reference UI.

    Filters out agent-*.jsonl files as they are subagent logs, not main sessions.

    Args:
        working_dir: The project working directory
        search: Optional search filter for title/content
        limit: Maximum number of sessions to return

    Returns:
        List of session dicts with: name, path, title, preview, message_count,
        modified_at, size_bytes, type='chat_history'
    """
    claude_projects_dir = settings.get_claude_projects_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    sessions = []
    search_lower = search.lower() if search else ""

    if not project_dir.exists():
        return sessions

    for jsonl_file in project_dir.glob("*.jsonl"):
        try:
            # Skip agent files (subagent logs) and ``<sid>.hooks.jsonl``
            # sidecars (plugin warnings, not a session of their own).
            if jsonl_file.name.startswith("agent-") or jsonl_file.name.endswith(".hooks.jsonl"):
                continue

            stat = jsonl_file.stat()
            title, preview, message_count = _get_session_title_and_preview(jsonl_file)

            # Apply search filter
            if search_lower:
                title_match = title and search_lower in title.lower()
                preview_match = preview and search_lower in preview.lower()
                if not title_match and not preview_match:
                    continue

            # Build display name
            display_name = title or f"Session {jsonl_file.stem[:8]}..."

            sessions.append({
                "name": display_name,
                "type": "chat_history",
                "path": str(jsonl_file),
                "sdk_session_id": jsonl_file.stem,
                "title": title,
                "preview": preview,
                "message_count": message_count,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size_bytes": stat.st_size
            })

        except Exception as e:
            logger.warning(f"Failed to process {jsonl_file}: {e}")

    # Sort by modified time, newest first
    sessions.sort(key=lambda x: x["modified_at"], reverse=True)

    # Apply limit
    return sessions[:limit]
