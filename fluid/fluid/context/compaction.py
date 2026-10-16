"""Hierarchical, recoverable compaction — the mechanical eviction ladder.

Tier 1 (:mod:`fluid.context.trim`) replaces old tool results with placeholders.
Tier 2 collapses whole old turns ("spans") into extractive digests. Tier 3
folds runs of old digests into one-line epoch stubs. Optional tier 4 replaces an
old compacted prefix with persisted model-written checkpoint text. Every evicted thing
carries a node ref, and the ``HistoryExpand`` / ``HistorySearch`` builtins
re-hydrate originals from the session store — loss is recoverable, never
permanent.

Invariants (shared with tier 1, enforced here):

* **Mechanical tiers 1–3.** No model writes their digests. ``render_span_digest`` and
  ``render_epoch_stub`` are pure functions of (nodes, config): extractive,
  deterministic, byte-identical on every render — which is what makes tier
  state cheap to persist (decisions only; text is re-derived).
* **Batch + sticky.** Promotions happen in rare amortized batches and are
  sticky for the engine's lifetime; between batches the rendered prefix is
  byte-identical, so provider cache hits resume after the one-time break.
  Consume-once image trims obey this too — they wait for pressure and for the
  first model call of a turn, and they swap only the image blocks in place,
  so a screenshot never forces a full-price re-send of the whole prompt.
* **The tree is never mutated.** All tiers are view transformations; the
  stored SessionNodes keep full fidelity (rewind/fork/audit/recall see
  everything).
* **Errors survive verbatim.** Tier 1 never trims error results; tiers 2/3
  carry error text verbatim (capped) inside digests.
* **Fork-safe.** A promotion applies only when both its boundary nodes sit on
  the currently rendered path (the parent chain is unique, so the interval is
  then identical to the one promoted). Off-path promotions are silently inert.

Tiers are separable at the bottom: ``CompactionConfig(enabled=False,
trim=TrimConfig(enabled=True))`` runs tier 1 alone — tool results become
recoverable placeholders, no turn is ever digested, no epoch is ever folded.
That is the contract host apps configure when they want the context saving
without the model ever reading a machine summary of its own history.

Oversize user payloads ("intake") are a stateless view policy, not a tier: a
user ``Text`` block over ``intake_stub_chars`` always renders as a stub with a
preview and an expand hint, from the very first request — a pure function of
size, so it needs no state, no rehydration, and never breaks the cache.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from fluid.context.arg_elision import elidable_call_ids, elide_tool_args
from fluid.context.cache_signal import (
    ZONE_BELOW_FLOOR,
    ZONE_OPPORTUNISTIC,
    ZONE_PRICED,
    CacheSignal,
    Schedule,
    Zones,
    decide_schedule,
)
from fluid.context.peer import PEER_OPEN_PREFIX
from fluid.context.recall import parse_ref
from fluid.context.steer_render import (
    CURRENT_RENDER_VERSION,
    RENDER_VERSION_LEGACY,
    SteerRenderConfig,
    render_steers,
)
from fluid.context.trim import (
    ToolResultTrimmer,
    TrimConfig,
    block_chars,
    cell_calls,
    describe_loaded,
    estimate_tokens_of_messages,
    last_output_line,
    loaded_by,
    summarize_cell,
)
from fluid.messages import (
    Image,
    Message,
    Text,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.store.base import SessionNode

SPAN_DIGEST = "digest"
EPOCH_STUB = "epoch"
#: ``ContextCompaction.reason`` of the one summary event an owner-requested
#: compaction (:meth:`Compactor.compact_now`) emits after its promotions. It
#: carries the before/after sizes and no decisions, so replay ignores it.
MANUAL_COMPACT_REASON = "manual_compact"

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# Version 2 digests and epoch stubs carry this line under their header, so a
# compacted region never reads as more current than what the user said last.
OUTRANK_LINE = (
    "Latest user message outranks this; HistoryExpand a node before relying "
    "on an exact value."
)
OUTRANK_LINE_NO_RECALL = (
    "Latest user message outranks this; do not rely on an exact value from it."
)
# Files and loads an epoch stub names, at most.
_EPOCH_MAX_PATHS = 12
_EPOCH_PATH_CAP = 120


@dataclass(kw_only=True)
class CompactionConfig:
    # The ladder above tier 1: span digests (tier 2) and epoch folds (tier 3).
    # ``enabled=False`` with ``trim=TrimConfig(enabled=True)`` is the supported
    # *trim-only* contract — tool results are trimmed to recoverable
    # placeholders and oversize payloads still render as stubs, but no turn is
    # ever digested and no epoch is ever folded. Host apps that want cheap,
    # fully recoverable tool-result eviction without the model ever reading a
    # digest of its own history configure exactly that.
    enabled: bool = True
    # Tier 4 is opt-in for library callers; mechanical tiers remain independent.
    summary_enabled: bool = False
    summary_trigger_window_ratio: float = 0.10
    summary_max_output_tokens: int = 4_000
    summary_input_cap_tokens: int = 120_000
    summary_model: str | None = None
    # Whether the session can call HistoryExpand and HistorySearch. Off, the
    # digests and stubs stop naming a recovery route the model cannot take.
    recall_tools: bool = True
    # Fraction of the arming threshold at which consume-once image trims fire.
    # 1.0 = the same trigger as every other rung. Lower it to reclaim image
    # bytes earlier (they are the cheapest reclaim and the model has already
    # seen them); 0 disables image consumption outside overflow recovery.
    # Never fires mid-turn whatever the value — see Compactor.apply.
    image_trigger_ratio: float = 1.0
    # Count rung for images, the one image rung allowed mid-turn. When more
    # than ``live_image_cap`` tool results still carry images the model has
    # already seen, all but the newest ``live_image_keep`` of them are
    # consumed in one batch. A desktop or browser loop takes a screenshot per
    # step; without this a long turn resends every one of them on every call
    # (cost, and stale views next to the current one) until a provider's
    # per-request image limit rejects the request. Batching keeps the prefix
    # rewrite rare: one cache break per (cap - keep) new screenshots.
    # 0 disables the rung.
    live_image_cap: int = 12
    live_image_keep: int = 3
    # Tier-1 knobs (the owned trimmer's config).
    trim: TrimConfig = field(default_factory=TrimConfig)
    # Estimated-total-context trigger FLOOR; also the whole story when the
    # model's context window is unknown.
    trigger_tokens: int = 120_000
    # Fraction of a KNOWN context window the prompt may reach before the ladder
    # arms. The effective threshold is
    #   min(max(trigger_tokens, window * trigger_window_ratio),
    #       window - headroom_tokens - output reserve)
    # so a 200k model keeps the historical 120k trigger while a 1M model gets
    # 600k instead of compacting (and cache-busting) at 12% window usage.
    # 0.0 restores the old fixed trigger.
    trigger_window_ratio: float = 0.6
    # Reserve kept free below a known context window so arrivals can't
    # overflow: it is the cap in the formula above.
    headroom_tokens: int = 16_000
    # A batch reclaims down to target_ratio * threshold (hysteresis: batches
    # stay rare, renders stay byte-identical for long stretches).
    target_ratio: float = 0.7
    # The newest K turns are never digested (the in-flight turn is always the
    # newest).
    keep_recent_turns: int = 3
    # Tier 3: when more span digests than this are resident, fold the oldest
    # epoch_fold_spans of them into one epoch stub.
    max_resident_span_digests: int = 20
    epoch_fold_spans: int = 10
    # A harness reminder the model must keep seeing through any fold: the
    # (full block, change list) headers of the chain. The host sends the full
    # block once and change lists after it; when a digest, epoch, checkpoint
    # summary or intake stub would hide part of the chain as it stood when
    # the fold was decided (the path's last node then, persisted as the
    # ContextCompaction event's node_id), the fold's message carries it
    # verbatim (_PinScan.chain). A newer full block therefore never rewrites
    # an older fold. None turns pinning off.
    pinned_reminder: tuple[str, str] | None = ("# Memory\n", "# Memory changes\n")
    # Intake: user Text blocks longer than this always render as a stub.
    intake_stub_chars: int = 25_000
    intake_preview_chars: int = 500
    # Intake for tool results: a non-error tool-result Text block over this
    # renders as a stub immediately — the batch trim ladder fires on a token
    # trigger, so without this a single monster result (or several in one
    # turn) could overflow the window before any trigger runs. Same stateless
    # pure-function-of-size contract as the user-attachment stub; errors are
    # never stubbed (losing an error message is how doom loops start).
    tool_result_stub_chars: int = 50_000
    # Digest extraction caps. ``user_text_cap`` covers user text that is not
    # a real user's request (wake and harness notices); from render version 2
    # a real user's turn-start text gets ``user_turn_text_cap`` instead.
    user_text_cap: int = 600
    user_turn_text_cap: int = 2_000
    assistant_text_cap: int = 400
    tool_line_cap: int = 120
    error_text_cap: int = 2_000
    # Retry watermark discipline for the full ladder (plan item 4, B1). On, a
    # decision below the trigger, or a candidate that reclaimed nothing, does
    # not arm the watermark, so the first call that really crosses the
    # trigger builds its batch at once instead of waiting for more
    # reclaimable growth. Off restores the legacy arming; the parity tests
    # pin that path. The trim-only contract (``enabled=False``) always keeps
    # the legacy arming so its renders stay byte-identical.
    below_threshold_skips_retry: bool = True
    # Image rung inside the size batch (plan item 6, B13). On, whether a batch
    # is due is decided on the prompt before images are consumed, and the size
    # rungs still run when the images alone pull it under the trigger. Off
    # restores the legacy early return, which split one due batch into two
    # rebuilds a few calls apart, or sank it under the 5% floor when the image
    # reclaim was small. The trim-only contract always keeps the legacy path.
    image_rung_in_batch: bool = True
    # Deep target (plan item 5, B2). With a known window every full-ladder
    # batch reclaims down to T = deep_target_window_ratio * window measured
    # tokens, or the legacy target_ratio * threshold when that is lower. The
    # legacy target sat only 30% under the trigger, so a long turn regrew to
    # the trigger within a few calls and paid for another rebuild. 0 restores
    # the legacy target. With no known window the legacy target always holds.
    deep_target_window_ratio: float = 0.35
    # Hard ceiling H = min(hard_window_ratio * window, L - hard_margin_tokens),
    # where L is the window-protection limit. Between H and L a batch skips
    # the shortfall watermark below (not the legacy floor watermark), and it
    # must land at or below the trigger S or run the guarded escalation. The
    # margin covers the largest single-call jump seen in production (a 36.6k
    # steer). With no known window, a 0 ratio, or an H at or below S, there
    # is no hard zone.
    hard_window_ratio: float = 0.75
    hard_margin_tokens: int = 32_000
    # Shortfall watermark (plan item 5, B2). On, a commit with a known window
    # that still leaves the prompt above its batch target sets a watermark
    # at the shortfall instead of clearing it: later mid-turn calls over the
    # trigger wait until the reclaimable chars have grown by that much, so
    # one shallow batch is not followed by another a few calls later. The
    # hard zone and window protection skip the watermark, so it never delays
    # a needed batch. Off restores the legacy clear on every commit.
    shortfall_watermark: bool = True
    # Token-budgeted recency (plan item 2, B3). With a known window the
    # newest min(recency_tail_tokens, recency_tail_window_ratio * window)
    # measured tokens of history stay verbatim, together with the newest
    # recency_tail_calls model calls and the in-flight turn-start node.
    # Everything older is in reach: tool results anywhere, and completed
    # segments of any turn, the in-flight one included. This replaces "the
    # newest 3 turns keep their newest 3 segments" and "the newest 5 results
    # stay" (keep_recent_tool_results), which let one long turn grow past
    # the window with nothing the guarded rungs were allowed to touch. 0
    # restores the legacy guards, and so does an unknown window.
    recency_tail_tokens: int = 40_000
    recency_tail_window_ratio: float = 0.15
    recency_tail_calls: int = 2
    # The guarded in-flight rung (tier 1, reason "turn_budget"). When the
    # in-flight turn holds more than turn_budget_window_ratio * window live
    # tokens and the prompt is at or above turn_budget_floor_ratio * window
    # (the F zone), the guarded escalation trims in-flight results older
    # than the recency tail. At or over the window-protection limit it
    # narrows the tail to the newest calls and also digests completed
    # in-flight segments, because a provider may accept prompts far past
    # the configured window and never send the overflow error the unguarded
    # rungs wait for. 0 turns the rung off.
    turn_budget_window_ratio: float = 0.2
    turn_budget_floor_ratio: float = 0.45
    # Window protection at or above window_floor_waiver_ratio * window
    # commits any batch that reclaims window_floor_waiver_tokens measured
    # tokens or more, in place of the 5% floor. Near the window a small
    # reclaim beats a prompt the provider may reject or bill past the
    # window. 0 keeps the 5% floor everywhere.
    window_floor_waiver_ratio: float = 0.9
    window_floor_waiver_tokens: int = 2_000
    # Wake-aware turn counting. Turns started by an unpark, a wakeup or a
    # peer agent message (the engine reads the source from TurnStarted and
    # fills ``Compactor.wake_turn_starts``) do not count toward
    # keep_recent_turns, so a run of wakes cannot age a real user turn out
    # of the protected set. Off restores the legacy count.
    wake_aware_turns: bool = True
    # Cache-priced schedule (plan item 3, B4, B5; see fluid.context.cache_signal).
    # It runs only when the engine passes a CacheSignal to ``apply`` and the
    # window is known; with no signal the unpriced ladder runs unchanged.
    # Below F = opportunistic_floor_window_ratio * window nothing is built.
    # Between F and the trigger a batch is built only when the cache is cold
    # and commits only when it is deep. Between the trigger and the hard
    # ceiling a warm rewrite waits until the cached-price rent of deferring
    # reaches its buy price. The hard zone and window protection commit
    # regardless. Off ignores the signal.
    cache_schedule: bool = True
    opportunistic_floor_window_ratio: float = 0.45
    # Boundary sweep (plan item 8; tier 1, reason "sweep"). At the first call
    # of a turn, non-error results of trim.min_result_chars or more in
    # earlier turns become placeholders, except the newest sweep_keep_tokens
    # measured tokens of such results and the pinned ones, and images the
    # model has already seen are consumed in the same batch. A sweep is
    # priced like any other rewrite: on a cold cache it commits on its own
    # once it frees sweep_min_reclaim_tokens, at or over the trigger it rides
    # the batch that is due anyway, and on a warm cache below the trigger it
    # waits. Without a cache signal it only rides a due batch. "off" plans
    # nothing, "shadow" logs the would-be sweep in the decision telemetry
    # and leaves the view untouched, "on" commits it.
    sweep_mode: str = "shadow"
    sweep_keep_tokens: int = 20_000
    # Results a HistoryExpand call in the turn that just ended asked for, and
    # the expand call's own result, are pinned for the next turn, newest
    # first, up to this many measured tokens. The pins are derived from the
    # tool calls on the path, so a replay derives the same ones.
    sweep_pin_tokens: int = 16_000
    sweep_min_reclaim_tokens: int = 2_000
    # Render version new decisions are made under (fluid.context.steer_render).
    # Persisted decisions keep the version they were made under, so raising
    # this never re-renders an old session. 1 is the original renderer.
    render_version: int = CURRENT_RENDER_VERSION
    # Version 2 steer receipts: a harness notice (origin "system") at least
    # this long renders, once compacted, as its sender, a preview of this many
    # chars and a HistoryExpand ref. User and peer steers stay verbatim.
    steer_receipt_chars: int = 1_000
    steer_receipt_preview_chars: int = 300
    # Version 2 epoch stubs pin the folded turns' user requests verbatim
    # (each at most ``user_turn_text_cap``) within this many tokens, counted
    # at 4 chars per token like every other estimate here.
    intent_pin_tokens: int = 4_000
    # Arg elision (fluid.context.arg_elision): the input of an assistant tool
    # call whose result is already trimmed renders as a small JSON summary.
    # Off by default; off, every view is byte-identical to before the flag.
    elide_old_args: bool = False

    def steer_render(self) -> SteerRenderConfig:
        return SteerRenderConfig(
            receipt_min_chars=self.steer_receipt_chars,
            receipt_preview_chars=self.steer_receipt_preview_chars,
            recall_tools=self.recall_tools,
        )


# TurnStarted sources that are wakes rather than a person's message. Their
# turns do not age real user turns out of keep_recent_turns.
WAKE_TURN_SOURCES = frozenset({"unpark", "wakeup", "agent_message"})


# Absolute floor for a derived trigger. Below this the ladder would be
# re-rendering on every call; no model this harness targets has a window
# under 2k tokens, so the floor is never the binding one in practice.
MIN_TRIGGER_TOKENS = 1_000


def _in_estimate_units(tokens: int, scale: float) -> int:
    """A measured-token threshold expressed in chars/4 estimate units.

    ``scale`` is measured/estimated (at most 1), so the estimate that
    matches ``tokens`` measured is ``tokens / scale``.
    """
    if scale <= 0 or scale >= 1.0:
        return tokens
    return int(tokens / scale)


def derive_trigger_tokens(
    *,
    base: int,
    window_ratio: float,
    context_window: int | None,
    headroom_tokens: int,
    output_reserve_tokens: int = 0,
) -> int:
    """The estimated-token threshold that arms an eviction batch.

    With no known window this is ``base`` verbatim — the pre-existing fixed
    trigger, unchanged. With a window it scales: ``base`` becomes a floor,
    ``window * window_ratio`` the target, and ``window - headroom - reserve``
    the ceiling, so a large window is actually used instead of being compacted
    away at a fraction of its capacity.

    Two clamps keep the ceiling meaningful on small windows. The output
    reserve is capped at a quarter of the window (a 32k model cannot reserve
    32k for output), and the result never drops below half the window, or a
    small window would yield a negative threshold and the ladder would fire on
    every render. The floor only rescues a collapsed ceiling; it never raises a
    threshold above ``scaled``, so ``window_ratio=0`` still pins ``base``.
    """
    if context_window is None:
        return base
    scaled = base
    if window_ratio > 0:
        scaled = max(base, int(context_window * window_ratio))
    reserve = min(output_reserve_tokens, context_window // 4)
    ceiling = context_window - headroom_tokens - reserve
    floor = max(context_window // 2, MIN_TRIGGER_TOKENS)
    return max(min(scaled, ceiling), min(scaled, floor))


@dataclass(frozen=True, kw_only=True)
class Promotion:
    """A persistable rung decision, with frozen text for a model checkpoint."""

    tier: int  # 1 | 2 | 3 | 4
    # "trigger" | "overflow" | "image_consumed" | "turn_budget" | "sweep"
    reason: str = "trigger"
    trimmed: tuple[tuple[str, str], ...] = ()  # (node_id, tool_use_id)
    spans: tuple[tuple[str, str, str], ...] = ()  # (start, end, kind)
    reclaimed_chars: int = 0
    # Batch estimates (all rungs in one transaction share these boundaries).
    before_tokens: int = 0
    after_tokens: int = 0
    decision_reason: str = ""
    first_changed_message: int | None = None
    # What the window-protection decision was sized on: "measured" (the last
    # provider-billed prompt carried forward) or "estimate" (chars/4, used only
    # when no measured call is on the path). "" for provider-overflow rungs.
    token_basis: str = ""
    # The measured next-prompt size the decision used; 0 with an estimate.
    measured_prompt_tokens: int = 0
    # Renderer version the decision was made under (fluid.context.steer_render).
    # The default is the current version, because the promotions that omit it
    # are the ones Compactor.apply and escalate make now. A decision read back
    # from a ContextCompaction event passes the event's value, which is 1 for
    # an event written before the field existed, so it renders as it did then.
    # Compactor.rehydrate never replays above the compactor's own version.
    render_version: int = CURRENT_RENDER_VERSION
    summary_text: str = ""
    # The path's last node when the decision was made (the event's node_id),
    # read back on resume: a fold carries the pinned reminder chain as of it.
    # None (an unknown end) falls back to the fold's own last node.
    path_end: str | None = None


def describe_promotions(
    promotions: Iterable[Promotion],
) -> list[dict[str, Any]]:
    """Compact records of the rungs just applied — the "history was rewritten
    since the previous request" signal.

    A rung rewrites messages the provider already cached (a consume-once image
    trim replaces a block in a message that was sent verbatim last call), so
    the next request misses. Handing these to the cache probe lets a miss say
    "we rewrote history" instead of pointing at provider routing. Descriptive
    only: nothing here changes what gets rendered.
    """
    return [
        {
            "tier": promo.tier,
            "reason": promo.reason,
            "trimmed": len(promo.trimmed),
            "spans": len(promo.spans),
            "reclaimed_chars": promo.reclaimed_chars,
        }
        for promo in promotions
    ]


def _chars_of(
    nodes: list[SessionNode], pairs: list[tuple[str, str]]
) -> int:
    """Estimated chars carried by the named tool-result blocks."""
    wanted = set(pairs)
    return sum(
        block_chars(block)
        for node in nodes
        for block in node.message.content
        if isinstance(block, ToolResultBlock)
        and (node.node_id, block.tool_use_id) in wanted
    )


def is_turn_start(node: SessionNode) -> bool:
    """A real user turn: user-role with any non-tool-result content. Tool
    result nodes are user-role too, but carry only ToolResultBlocks."""
    return node.message.role == "user" and any(
        not isinstance(b, ToolResultBlock) for b in node.message.content
    )


def split_spans(nodes: list[SessionNode]) -> list[list[SessionNode]]:
    """Chunk the path into turns: each span runs from a turn-start user node
    up to (not including) the next one. A leading run before the first
    turn-start (shouldn't happen — roots are user turns) forms its own span
    rather than being dropped."""
    spans: list[list[SessionNode]] = []
    for node in nodes:
        if not spans or is_turn_start(node):
            spans.append([node])
        else:
            spans[-1].append(node)
    return spans


def completed_segments(
    span: list[SessionNode],
    calls_per_segment: int = 8,
    error_text_cap: int = 2_000,
) -> list[list[SessionNode]]:
    """Bound long turns at completed call/result groups, not at textual markers.

    Only new results with complete typed delivery metadata qualify. Legacy
    tool text may contain a real steer whose provenance cannot be recovered.
    An error result blocks a segment only when its text is longer than
    ``error_text_cap`` (the digest quotes errors up to that cap verbatim, so
    a short error loses nothing in digestion); code-mode cells that raise are
    error results, and treating every one as unsafe would keep whole turns
    undigestible.
    """
    segments: list[list[SessionNode]] = []
    batch: list[SessionNode] = []
    pending: set[str] = set()
    calls = 0
    safe = True
    for node in span:
        if is_turn_start(node):
            continue  # Never digest the long turn's original user constraints.
        batch.append(node)
        for block in node.message.content:
            if isinstance(block, ToolUseBlock):
                if block.id in pending:
                    safe = False
                pending.add(block.id)
            elif isinstance(block, ToolResultBlock):
                if block.tool_use_id not in pending:
                    safe = False
                pending.discard(block.tool_use_id)
                if block.delivery_metadata_version != 1:
                    safe = False
                elif block.is_error and _error_text_len(block) > error_text_cap:
                    safe = False
        if node.message.role == "assistant":
            calls += 1
        if not pending and calls >= calls_per_segment:
            if safe:
                segments.append(batch)
            batch, calls, safe = [], 0, True
    return segments


def _error_text_len(block: ToolResultBlock) -> int:
    return sum(len(inner.text) for inner in block.content if isinstance(inner, Text))


def strip_system_reminders(text: str) -> str:
    """Drop <system-reminder> blocks from digested user text: they are harness
    plumbing (deferred-tool catalogs, ledger recitations) that is re-derivable
    and re-injected on fresh turns — history copies are pure weight."""
    return _REMINDER_RE.sub("", text).strip()


_PIN_OPEN = "<system-reminder>\n"
_PIN_CLOSE = "\n</system-reminder>"


def _reminder_blocks(node: SessionNode, headers: tuple[str, str]) -> list[tuple[str, str]]:
    """``("full" | "changes", block)`` for every pinned-header reminder in a
    user node's text, in order; each block is verbatim, tags included."""
    if node.message.role != "user":
        return []
    text = "\n".join(b.text for b in node.message.content if isinstance(b, Text))
    if _PIN_OPEN not in text:
        return []
    found: list[tuple[int, str, str]] = []
    for kind, header in zip(("full", "changes"), headers):
        marker = _PIN_OPEN + header
        start = text.find(marker)
        while start >= 0:
            end = text.find(_PIN_CLOSE, start)
            if end < 0:
                break
            found.append((start, kind, text[start:end + len(_PIN_CLOSE)]))
            start = text.find(marker, end)
    found.sort()
    return [(kind, block) for _, kind, block in found]


class _PinScan:
    """Every pinned-header reminder on a path, scanned once per render."""

    __slots__ = ("blocks", "newest_full")

    def __init__(self, nodes: list[SessionNode], headers: tuple[str, str] | None) -> None:
        self.blocks: list[list[tuple[str, str]]] = (
            [_reminder_blocks(n, headers) for n in nodes] if headers else [[] for _ in nodes]
        )
        # newest_full[k]: index of the newest node at or before k carrying a
        # full block, -1 when there is none.
        self.newest_full: list[int] = []
        newest = -1
        for k, blocks in enumerate(self.blocks):
            if any(kind == "full" for kind, _ in blocks):
                newest = k
            self.newest_full.append(newest)

    def chain(self, first: int, last: int, as_of: int | None = None) -> dict[int, list[str]]:
        """The chain as of node ``as_of`` (default ``last``): the newest full
        block on ``nodes[:as_of + 1]`` and every change list after it,
        restricted to nodes ``first..last``. Node index -> blocks.

        A fold passes the path end its decision saw, so what it carries never
        depends on anything appended later: a newer full block leaves the
        fold's (now superseded) copy where it is and the cached prefix stays
        byte-identical. Whatever the newest chain on the path holds inside
        the fold is always among it.
        """
        if last < first or not self.blocks:
            return {}
        as_of = last if as_of is None else min(max(as_of, last), len(self.blocks) - 1)
        newest = self.newest_full[as_of]
        if newest < 0 or newest > last:
            return {}
        chain: dict[int, list[str]] = {}
        if newest >= first:
            blocks = self.blocks[newest]
            last_full = max(k for k, (kind, _) in enumerate(blocks) if kind == "full")
            chain[newest] = [block for _, block in blocks[last_full:]]
        for k in range(max(newest + 1, first), last + 1):
            changes = [block for kind, block in self.blocks[k] if kind == "changes"]
            if changes:
                chain[k] = changes
        return chain


def pinned_reminder_chain(
    nodes: list[SessionNode], headers: tuple[str, str] | None,
) -> dict[int, list[str]]:
    """The newest pinned reminder chain on ``nodes``: node index -> blocks.

    The chain is the newest full block and every change list after it: what
    the model must see on this path. A fold carries the chain as of the path
    end its decision saw (``_PinScan.chain``), which includes every part of
    this chain that the fold covers.
    """
    return _PinScan(nodes, headers).chain(0, len(nodes) - 1)


def _carry_pins(
    message: Message, pins: _PinScan, first: int, last: int, as_of: int | None = None,
) -> Message:
    """``message`` (rendered from nodes ``first..last``) with every block of
    the chain as of ``as_of`` that it covers but no longer shows appended,
    verbatim, in order."""
    chain = pins.chain(first, last, as_of)
    blocks = [block for k in sorted(chain) for block in chain[k]]
    if not blocks:
        return message
    shown = "\n".join(b.text for b in message.content if isinstance(b, Text))
    missing = [block for block in blocks if block not in shown]
    if not missing:
        return message
    return replace(message, content=[*message.content, Text("\n\n".join(missing))])


def _user_text(node: SessionNode) -> str:
    """A user node's own text, harness reminders stripped."""
    return strip_system_reminders(
        "".join(b.text for b in node.message.content if isinstance(b, Text))
    )


def is_wake_text(text: str) -> bool:
    """True for turn-start text the harness wrote rather than the user.

    Judged from the stored text alone, because a SessionNode records no turn
    source: an empty text once reminders are stripped (fluid's own unpark
    wake is one reminder block), a peer's message (the agent-message marker)
    and a task notification (the host's ``<task-notification>`` substring
    convention). None of these is a request that governs the work.
    """
    return (
        not text
        or text.startswith(PEER_OPEN_PREFIX)
        or "<task-notification>" in text
    )


def _clip_verbatim(text: str, cap: int) -> str:
    """Like ``_clip`` but keeps the text's own line breaks and spacing."""
    if len(text) <= cap:
        return text
    return f"{text[:cap]}… (+{len(text) - cap} chars)"


def _intent_pin(
    spans: list[list[SessionNode]], config: CompactionConfig
) -> list[str]:
    """The pinned intent block of a version 2 epoch stub.

    The folded run's first real user request comes first, then the later
    real user requests newest-first, each clipped to ``user_turn_text_cap``,
    while they fit in ``intent_pin_tokens``. Wake and harness notices are
    left out, so they can never push a user's request out of the budget.
    """
    requests = [
        (node.node_id, text)
        for span in spans
        for node in span
        if is_turn_start(node)
        and not is_wake_text(text := _user_text(node))
    ]
    if not requests:
        return []
    cap = config.user_turn_text_cap
    budget = config.intent_pin_tokens * 4
    first_id, first_text = requests[0]
    first = _clip_verbatim(first_text, cap)
    budget -= len(first)
    lines = [
        "Pinned intent: the user's own requests in these folded turns, "
        "verbatim (a later user message outranks them).",
        f"original request ({first_id}):",
        first,
    ]
    later = requests[1:][::-1]
    for i, (node_id, text) in enumerate(later):
        clipped = _clip_verbatim(text, cap)
        if len(clipped) > budget:
            omitted = len(later) - i
            lines.append(
                f"({omitted} older request(s) not pinned for space"
                + ("; HistoryExpand the epoch to read them)"
                   if config.recall_tools else ")")
            )
            break
        budget -= len(clipped)
        lines.extend([f"later request ({node_id}):", clipped])
    return lines


def _clip(text: str, cap: int) -> str:
    text = " ".join(text.split())  # collapse newlines/runs for one-line-ness
    if len(text) <= cap:
        return text
    return f"{text[:cap]}… (+{len(text) - cap} chars)"


def _arg_hint(input_: dict[str, Any], cap: int) -> str:
    try:
        rendered = json.dumps(input_, default=str)
    except (TypeError, ValueError):
        rendered = "…"
    return rendered[:cap] + ("…" if len(rendered) > cap else "")


def _outrank_line(config: CompactionConfig) -> str:
    return OUTRANK_LINE if config.recall_tools else OUTRANK_LINE_NO_RECALL


def _leaf(name: str) -> str:
    return str(name or "").split("__")[-1]


def _cell_code(block: ToolUseBlock) -> str | None:
    """A run_python call's code, or None for any other call."""
    if _leaf(block.name) != "run_python" or not isinstance(block.input, dict):
        return None
    code = block.input.get("code")
    return code if isinstance(code, str) else ""


def _call_inventory(spans: list[list[SessionNode]]) -> list[str]:
    """The files read and the tools and skills loaded in a folded run,
    counting the Read, Skill and ToolSearch calls made inside code cells."""
    paths: list[str] = []
    loads: list[tuple[str, str]] = []

    def note(name: str, values: dict[str, Any]) -> None:
        if name == "Read":
            path = values.get("file_path") or values.get("path")
            if isinstance(path, str) and path.strip():
                clipped = _clip(path, _EPOCH_PATH_CAP)
                if clipped not in paths:
                    paths.append(clipped)
        loads.extend((name, item) for item in loaded_by(name, values))

    for span in spans:
        for node in span:
            if node.message.role != "assistant":
                continue
            for block in node.message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                code = _cell_code(block)
                if code is None:
                    note(_leaf(block.name), block.input if isinstance(block.input, dict) else {})
                    continue
                for call in cell_calls(code):
                    note(call.name, call.values())
    lines: list[str] = []
    if paths:
        more = len(paths) - _EPOCH_MAX_PATHS
        lines.append(
            "files read in these turns: "
            + ", ".join(paths[:_EPOCH_MAX_PATHS])
            + (f" (+{more} more)" if more > 0 else "")
        )
    loaded = describe_loaded(loads)
    if loaded:
        lines.append(f"still loaded from these turns: {loaded}")
    return lines


def _steer_config(config: CompactionConfig) -> SteerRenderConfig | None:
    """None (every steer verbatim) at render version 1, receipts above it."""
    if config.render_version <= RENDER_VERSION_LEGACY:
        return None
    return config.steer_render()


def render_span_digest(
    span: list[SessionNode], config: CompactionConfig
) -> Message:
    """Extractive digest of one whole turn, as a single user-role Text message.

    Pure and deterministic: same span + same config → byte-identical output.
    ``config.render_version`` picks the shape. Version 1 renders steers
    verbatim and every call as clipped JSON. Version 2 renders large harness
    notices as receipts, adds the outrank line under the header, summarizes
    code cells with ``summarize_cell`` and ends a successful cell's result
    line with its last printed line.
    """
    steer_cfg = _steer_config(config)
    v2 = config.render_version > RENDER_VERSION_LEGACY
    calls = {
        block.id: block
        for node in span if node.message.role == "assistant"
        for block in node.message.content if isinstance(block, ToolUseBlock)
    } if v2 else {}
    start, end = span[0].node_id, span[-1].node_id
    ref = start if start == end else f"{start}..{end}"
    tool_names: list[str] = []
    lines: list[str] = [
        f"[compacted turn {ref} — mechanical digest; originals intact: "
        f'HistoryExpand("{ref}") re-reads verbatim, HistorySearch finds text]'
        if config.recall_tools
        else f"[compacted turn {ref} — mechanical digest; the originals are "
        "not recoverable in this session, so do not reconstruct their detail "
        "from memory]"
    ]
    if v2:
        lines.append(_outrank_line(config))
    for node in span:
        message = node.message
        if message.role == "user":
            results = [
                b for b in message.content if isinstance(b, ToolResultBlock)
            ]
            for block in results:
                lines.extend(render_steers(
                    block.delivered_steers, steer_cfg, node=node,
                ))
                size = block_chars(block)
                if block.is_error:
                    error_text = " ".join(
                        inner.text
                        for inner in block.content
                        if isinstance(inner, Text)
                    )
                    lines.append(
                        f"  ✗ error result ({node.node_id}): "
                        f"{_clip(error_text, config.error_text_cap)}"
                    )
                else:
                    last = ""
                    call = calls.get(block.tool_use_id)
                    if call is not None and _cell_code(call) is not None:
                        last = last_output_line("\n".join(
                            inner.text for inner in block.content
                            if isinstance(inner, Text)
                        ))
                    lines.append(
                        f"  ✓ result {size} chars ({node.node_id})"
                        + (f'; last line: "{last}"' if last else "")
                    )
            user_text = _user_text(node)
            if user_text:
                cap = config.user_text_cap
                if steer_cfg is not None and not is_wake_text(user_text):
                    cap = config.user_turn_text_cap
                lines.append(
                    f"user ({node.node_id}): {_clip(user_text, cap)}"
                )
        elif message.role == "assistant":
            texts = [b for b in message.content if isinstance(b, Text)]
            if texts:  # the last text block is the conclusion
                lines.append(
                    f"assistant ({node.node_id}): "
                    f"{_clip(texts[-1].text, config.assistant_text_cap)}"
                )
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    tool_names.append(block.name)
                    code = _cell_code(block) if v2 else None
                    if code is not None:
                        lines.append(
                            f"  → {block.name}: {summarize_cell(code)}"
                        )
                        continue
                    lines.append(
                        f"  → {block.name}("
                        f"{_arg_hint(block.input, config.tool_line_cap)})"
                    )
        # Thinking/Reasoning blocks are deliberately dropped; system-role
        # nodes don't occur mid-history.
    if tool_names:
        counts: dict[str, int] = {}
        for name in tool_names:
            counts[name] = counts.get(name, 0) + 1
        summary = ", ".join(f"{n}×{c}" for n, c in sorted(counts.items()))
        lines.insert(2 if v2 else 1, f"tools: {summary}")
    return Message(role="user", content=[Text("\n".join(lines))])


def render_epoch_stub(
    spans: list[list[SessionNode]], config: CompactionConfig
) -> Message:
    """Stub replacing a run of digested spans. Deterministic.

    ``config.render_version`` picks the shape. Version 1 is one line that
    quotes the first folded turn's first 120 chars, and steers render
    verbatim. Version 2 drops that quote for the outrank line, the files
    read and the tools and skills loaded in the folded turns (including the
    calls made inside code cells), and a pinned intent block (see
    ``_intent_pin``); it renders large harness notices as receipts.
    """
    start = spans[0][0].node_id
    end = spans[-1][-1].node_id
    steer_cfg = _steer_config(config)
    hint = ""
    if steer_cfg is None:
        first_user = ""
        for node in spans[0]:
            if is_turn_start(node):
                first_user = _user_text(node)
                break
        hint = f"; began with: {_clip(first_user, 120)}" if first_user else ""
    if config.recall_tools:
        text = (
            f"[epoch {start}..{end} — {len(spans)} older turns folded; "
            f"HistorySearch finds their content, "
            f'HistoryExpand("{start}..{end}") re-reads them{hint}]'
        )
    else:
        text = (
            f"[epoch {start}..{end} — {len(spans)} older turns folded; their "
            f"content is not recoverable in this session{hint}]"
        )
    if steer_cfg is not None:
        text = "\n".join([
            text, _outrank_line(config), *_call_inventory(spans),
            *_intent_pin(spans, config),
        ])
    deliveries = [Text(s) for span in spans for node in span
                  for block in node.message.content if isinstance(block, ToolResultBlock)
                  for s in render_steers(block.delivered_steers, steer_cfg, node=node)]
    return Message(role="user", content=[Text(text), *deliveries])


# Lines of a harness task notification that identify the task rather than
# say what happened: tag-only lines, the identity fields, and the untrusted-data
# fence note. A preview made of them tells the model nothing it can act on.
_NOTIFICATION_ID_TAGS = frozenset({
    "kind", "task-id", "tool-use-id", "output-file",
    "usage", "subagent_tokens", "tool_uses", "duration_ms",
})
_TAG_ONLY_LINE_RE = re.compile(r"^(?:</?[\w-]+(?:\s[^<>]*)?>\s*)+$")
_ID_FIELD_LINE_RE = re.compile(r"^<([\w-]+)>[^<>]*</\1>$")
_FENCE_NOTE_HEAD = "The content below was retrieved from an external source."


def _intake_preview(text: str, config: CompactionConfig) -> str:
    """The first ``intake_preview_chars`` of an oversized user text.

    At render version 2 a harness task notification skips its header lines,
    so the preview shows the summary and result instead of the task id and
    output path. Every other text, and every text at version 1, previews from
    its first char.
    """
    cap = config.intake_preview_chars
    if config.render_version < 2 or "<task-notification>" not in text:
        return text[:cap]
    kept: list[str] = []
    size = 0
    for line in text.splitlines():
        bare = line.strip()
        field = _ID_FIELD_LINE_RE.match(bare)
        if (
            not bare
            or _TAG_ONLY_LINE_RE.match(bare)
            or (field and field.group(1) in _NOTIFICATION_ID_TAGS)
            or bare.startswith(_FENCE_NOTE_HEAD)
        ):
            continue
        kept.append(bare)
        size += len(bare) + 1
        if size >= cap:
            break
    return "\n".join(kept)[:cap]


def _intake_stub(node_id: str, text: str, config: CompactionConfig) -> Text:
    preview = _intake_preview(text, config)
    route = (
        f'read the rest with HistoryExpand("{node_id}", offset=N)'
        if config.recall_tools
        else "the rest is not in context; ask the user for the part you need"
    )
    return Text(
        f"[large user attachment: {len(text)} chars — preview: {preview}… {route}]"
    )


def _tool_result_stub(node_id: str, text: str, config: CompactionConfig) -> Text:
    preview = text[: config.intake_preview_chars]
    route = (
        f'recover it with HistoryExpand("{node_id}", offset=N) when any detail matters'
        if config.recall_tools
        else "re-run the tool for the part you need when any detail matters"
    )
    return Text(
        f"[large tool result: {len(text)} chars — preview: {preview}… "
        f"The rest is NOT in context: do not reconstruct it from memory; {route}]"
    )


# A literal HistoryExpand call in a code-mode cell's source.
_EXPAND_CALL_RE = re.compile(
    r"HistoryExpand\s*\(\s*(?:ref\s*=\s*)?[\"'](node_[0-9a-f]+(?:\.\.node_[0-9a-f]+)?)[\"']"
)


def _expand_refs(block: ToolUseBlock) -> list[tuple[str, str]]:
    """Node ranges a tool call expands: the ``ref`` of a HistoryExpand call,
    or each literal ``HistoryExpand("node_...")`` in the string arguments of
    a code-mode cell."""
    input_ = block.input if isinstance(block.input, dict) else {}
    if block.name == "HistoryExpand":
        ref = input_.get("ref")
        parsed = parse_ref(ref) if isinstance(ref, str) else None
        return [parsed] if parsed else []
    found: list[tuple[str, str]] = []
    for value in input_.values():
        if isinstance(value, str) and "HistoryExpand" in value:
            for match in _EXPAND_CALL_RE.finditer(value):
                parsed = parse_ref(match.group(1))
                if parsed:
                    found.append(parsed)
    return found


def _result_text_chars(
    nodes: list[SessionNode], pairs: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Chars of each named tool result without its images, which the sweep
    consumes separately and the keep does not count."""
    wanted = set(pairs)
    out: dict[tuple[str, str], int] = {}
    for node in nodes:
        if node.message.role != "user":
            continue
        for block in node.message.content:
            if isinstance(block, ToolResultBlock):
                key = (node.node_id, block.tool_use_id)
                if key in wanted:
                    out[key] = block_chars(block) - sum(
                        block_chars(inner) for inner in block.content or ()
                        if isinstance(inner, Image)
                    )
    return out


@dataclass(frozen=True, kw_only=True)
class SweepPlan:
    """What a boundary sweep would do on one call (plan item 8).

    ``pairs`` are the results it placeholders and ``images`` the image
    results it consumes. ``gate`` says how the schedule treats it: "cold"
    (free, commits on its own), "batch" (rides the batch due at or over the
    trigger), "warm_defer" (warm cache below the trigger), "below_threshold"
    (no cache signal and below the trigger) or "empty" (nothing to sweep).
    ``reclaim_tokens`` is in the chars/4 units of ``before_tokens``.
    """

    pairs: tuple[tuple[str, str], ...] = ()
    images: tuple[tuple[str, str], ...] = ()
    pinned: tuple[tuple[str, str], ...] = ()
    pinned_chars: int = 0
    reclaim_tokens: int = 0
    first_changed: int | None = None
    gate: str = "empty"

    def fields(self, mode: str) -> dict[str, Any]:
        return {
            "sweep_mode": mode,
            "sweep_gate": self.gate,
            "swept_results": len(self.pairs),
            "sweep_images": len(self.images),
            "sweep_reclaim_tokens": self.reclaim_tokens,
            "pinned_results": len(self.pinned),
            "pinned_tokens": self.pinned_chars // 4,
            "sweep_first_changed": self.first_changed,
        }


@dataclass
class Compactor:
    """Owns the eviction ladder state and renders compacted history views."""

    config: CompactionConfig = field(default_factory=CompactionConfig)
    trimmer: ToolResultTrimmer = None  # type: ignore[assignment]
    # Sticky promotion state: span key (start, end) -> SPAN_DIGEST|EPOCH_STUB,
    # and epoch ranges by their start node for O(1) lookup while walking.
    _digested: dict = field(default_factory=dict)
    _summaries: dict[tuple[str, str], str] = field(default_factory=dict)
    _epoch_by_start: dict = field(default_factory=dict)
    _digest_by_start: dict = field(default_factory=dict)
    _optional_retry: tuple[int, int] | None = None
    last_decision: dict[str, Any] = field(default_factory=dict)
    # Set by the last candidate build: the trigger it compared against (in
    # chars/4 units, like ``before_tokens``) and whether the rendered total
    # stayed at or below it. ``apply`` reads them to label the decision.
    _candidate_threshold: int | None = None
    _candidate_below: bool = False
    # The batch target of the last candidate that ran its size rungs, in the
    # same chars/4 units; None when no size batch ran.
    _candidate_target: int | None = None
    # Shortfall watermark (plan item 5): reclaimable chars at the commit and
    # the growth the next mid-turn batch over the trigger waits for.
    _shortfall_retry: tuple[int, int] | None = None
    # Node ids of turn starts whose TurnStarted source is a wake (see
    # WAKE_TURN_SOURCES). SessionNode carries no source, so the engine reads
    # it from the TurnStarted events and fills this set; turns it does not
    # name count as real user turns, which is the legacy behaviour.
    wake_turn_starts: set = field(default_factory=set)
    # Where the token-budgeted recency tail starts (an index into the nodes
    # of the decision in progress), or None for the legacy count-based
    # guards. Set for the length of one apply or escalate call only.
    _tail_from: int | None = None
    # The in-flight turn's live size in measured tokens when the decision in
    # progress started, before any rung ran, or None outside one.
    _turn_live: int | None = None
    # Consecutive window-protection decisions that committed nothing.
    _window_misses: int = 0
    # Rent the warm deferrals since the last commit have paid (plan item 3),
    # in measured tokens. Process memory: a resumed engine is cold anyway.
    _rent_paid: float = 0.0
    # Render version per promoted span key; a key made by this process
    # renders at config.render_version.
    _span_versions: dict = field(default_factory=dict)
    # (start, end) -> the path's last node id when the span was decided; the
    # fold carries the pinned reminder chain as of that node (_PinScan.chain).
    _span_pin_ends: dict = field(default_factory=dict)
    # Span keys rehydrated from persisted decisions, in replay order. Live
    # promotion retires the decisions a new span contains (segment
    # retirement, epoch folds). That needs node order, which rehydrate does
    # not have, so the pops are replayed the first time a path is seen.
    _replayed_spans: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.trimmer is None:
            self.trimmer = ToolResultTrimmer(config=self.config.trim)
        self.trimmer.render_version = self.config.render_version
        self.trimmer.steer_render = self.config.steer_render()

    @property
    def render_version(self) -> int:
        """The render version this compactor's new decisions are made under.

        Whoever persists a Promotion as a ContextCompaction event records this
        value, and whoever rehydrates passes the event's value back on the
        Promotion.
        """
        return self.config.render_version

    # ── Mode ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """True when this compactor rewrites the view at all."""
        return self.config.enabled or self.config.trim.enabled or bool(self._summaries)

    @property
    def trim_only(self) -> bool:
        """Tier 1 without the ladder above it — the ai-shuffle contract."""
        return not self.config.enabled and self.config.trim.enabled

    # ── Rendering (pure view; no promotion state is touched) ─────────────

    def view(self, nodes: list[SessionNode]) -> list[Message]:
        """The CURRENT compacted view, read-only — nothing is armed."""
        if not self.active:
            return [n.message for n in nodes]
        return self._render(nodes)

    def _render(self, nodes: list[SessionNode]) -> list[Message]:
        index = {n.node_id: i for i, n in enumerate(nodes)}
        # Decided state only: apply() owns the consume-once image decision
        # (pressure plus a turn boundary); rendering just replays it, so a
        # render never rewrites a message inside an already-cached prefix.
        trimmed = self.trimmer._trimmed
        consumed = self.trimmer._image_consumed
        # The whole path's calls, so a placeholder rendered one node at a
        # time can still say what its call was (version 2 decisions only;
        # see ToolResultTrimmer._render).
        calls: dict[str, Any] = {
            block.id: block
            for n in nodes if n.message.role == "assistant"
            for block in n.message.content if isinstance(block, ToolUseBlock)
        } if trimmed else {}
        elide = (
            elidable_call_ids(trimmed, index) if self.config.elide_old_args else None
        )
        # A fold must not hide the pinned reminder chain (the host's # Memory
        # snapshot): its message carries the chain as of the path end its
        # decision saw, verbatim, so a later snapshot never rewrites it.
        pins = _PinScan(nodes, self.config.pinned_reminder)

        def as_of(start: str, end: str) -> int | None:
            pin_end = self._span_pin_ends.get((start, end))
            return index.get(pin_end) if pin_end is not None else None

        out: list[Message] = []
        i = 0
        while i < len(nodes):
            node = nodes[i]
            summary = max(((end, text) for (start, end), text in self._summaries.items()
                           if start == node.node_id and end in index and index[end] >= i),
                          key=lambda item: index[item[0]], default=None)
            if summary is not None:
                end, text = summary
                j = index[end]
                out.append(_carry_pins(
                    self._summary_message(node.node_id, end, text), pins, i, j,
                    as_of(node.node_id, end),
                ))
                i = j + 1
                continue
            if not self.config.enabled:
                # Trim-only: tier 2/3 decisions are never made here, and a
                # persisted one rehydrated from a session that *did* run the
                # ladder stays inert rather than digesting history behind the
                # host app's back.
                out.append(_carry_pins(
                    self._render_live(node, trimmed, consumed, calls, elide), pins, i, i,
                ))
                i += 1
                continue
            end_id = self._epoch_by_start.get(node.node_id)
            if end_id is not None and end_id in index and index[end_id] >= i:
                j = index[end_id]
                out.append(_carry_pins(
                    render_epoch_stub(
                        split_spans(nodes[i : j + 1]),
                        self._config_at(node.node_id, end_id),
                    ),
                    pins, i, j, as_of(node.node_id, end_id),
                ))
                i = j + 1
                continue
            end_id = self._digest_by_start.get(node.node_id)
            if end_id is not None and end_id in index and index[end_id] >= i:
                j = index[end_id]
                out.append(_carry_pins(
                    render_span_digest(
                        nodes[i : j + 1], self._config_at(node.node_id, end_id)
                    ),
                    pins, i, j, as_of(node.node_id, end_id),
                ))
                i = j + 1
                continue
            # An intake stub hides a long user text block, reminders included.
            out.append(_carry_pins(
                self._render_live(node, trimmed, consumed, calls, elide), pins, i, i,
            ))
            i += 1
        return out

    def _config_at(self, start: str, end: str) -> CompactionConfig:
        """The config a promoted span renders with: this compactor's own,
        with ``render_version`` set to the version the span was decided at."""
        version = self._span_versions.get((start, end), self.config.render_version)
        if version == self.config.render_version:
            return self.config
        return replace(self.config, render_version=version)

    def _render_live(
        self,
        node: SessionNode,
        trimmed: set[tuple[str, str]],
        consumed: set[tuple[str, str]],
        calls: dict[str, Any] | None = None,
        elide: frozenset[str] | None = None,
    ) -> Message:
        message = (
            self.trimmer._render([node], trimmed, consumed, calls)[0]
            if self.trimmer.config.enabled
            else node.message
        )
        if message.role != "user":
            if elide is not None:
                return elide_tool_args(node.node_id, message, elide)
            return message
        blocks: list[Any] = []
        changed = False
        for block in message.content:
            if (
                isinstance(block, Text)
                and len(block.text) > self.config.intake_stub_chars
            ):
                blocks.append(
                    _intake_stub(node.node_id, block.text, self.config)
                )
                changed = True
            elif (
                isinstance(block, ToolResultBlock)
                and not block.is_error
                and any(
                    isinstance(b, Text)
                    and len(b.text) > self.config.tool_result_stub_chars
                    for b in block.content
                )
            ):
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=[
                            _tool_result_stub(node.node_id, b.text, self.config)
                            if isinstance(b, Text)
                            and len(b.text) > self.config.tool_result_stub_chars
                            else b
                            for b in block.content
                        # A stub is stateless (a pure function of size, from
                        # the first request on), so it cannot know when the
                        # result leaves the protected tail. Its steers stay
                        # verbatim; a receipt needs a trim, digest or fold
                        # decision, which re-renders them at that version.
                        ] + [Text(s) for s in render_steers(
                            block.delivered_steers, None,
                        )],
                        is_error=block.is_error,
                        delivered_steers=block.delivered_steers,
                        delivery_metadata_version=block.delivery_metadata_version,
                    )
                )
                changed = True
            else:
                blocks.append(block)
        if not changed:
            return message
        return Message(
            role="user", content=blocks, provider_meta=message.provider_meta
        )

    # ── Promotion (batched, sticky) ──────────────────────────────────────

    def apply(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int = 0,
        context_window: int | None = None,
        output_reserve_tokens: int = 0,
        turn_boundary: bool = True,
        measured_prompt_tokens: int | None = None,
        cache: CacheSignal | None = None,
    ) -> tuple[list[Message], list[Promotion]]:
        """Commit a batch only when its complete view saves enough.

        The 5% floor amortizes prefix rewrites; it is not a savings estimate.
        It applies to window protection too, up to
        ``window_floor_waiver_ratio`` of the window: a mandatory pass that
        cannot free 5% there is a cache break for almost nothing. From that
        line on any reclaim of ``window_floor_waiver_tokens`` commits,
        because a provider may bill a prompt far past the configured window
        without ever sending the overflow error that runs the unguarded
        ``escalate``. Window protection bypasses the retry watermarks.

        ``measured_prompt_tokens`` is the last billed prompt carried forward to
        this request. chars/4 runs about 12% high at the median and up to 49%
        high, which fired window protection at 55-64% real occupancy, so when
        a measurement exists the window decision is sized on it: the estimate
        is scaled by ``clamp(measured / estimate, 0.6, 1.0)``. The factor never
        exceeds 1, so a stale measurement falls back to the estimate and can
        never inflate it. The same factor sizes the optional trigger and every
        stop target the ladder and the guarded escalation compare against:
        each is divided by it, which puts it in chars/4 units, so a batch that
        is not needed on the measured prompt does not run as
        ``meaningful_reclaim`` either.

        ``cache`` is the engine's view of the provider cache for this call.
        With it, a known window and ``cache_schedule`` on, the decision is
        priced (plan item 3, see :mod:`fluid.context.cache_signal`): below
        the opportunistic floor nothing is built, a cold cache commits from
        the floor up, and a warm one defers rewrites between the trigger and
        the hard ceiling until break-even (``warm_defer``). Every such
        decision logs the signal. ``cache=None`` runs the unpriced ladder.
        """
        try:
            capped = self._cap_live_images(nodes)
            rendered, promotions = self._apply(
                nodes, overhead_tokens=overhead_tokens,
                context_window=context_window,
                output_reserve_tokens=output_reserve_tokens,
                turn_boundary=turn_boundary,
                measured_prompt_tokens=measured_prompt_tokens,
                cache=cache,
            )
            return rendered, capped + promotions
        finally:
            self._tail_from = None
            self._turn_live = None

    def _apply(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int,
        context_window: int | None,
        output_reserve_tokens: int,
        turn_boundary: bool,
        measured_prompt_tokens: int | None,
        cache: CacheSignal | None = None,
    ) -> tuple[list[Message], list[Promotion]]:
        before_view = self.view(nodes)
        before = overhead_tokens + estimate_tokens_of_messages(before_view)
        limit = (
            max(1, context_window - self.config.headroom_tokens - output_reserve_tokens)
            if context_window is not None
            else None
        )
        if measured_prompt_tokens is not None and measured_prompt_tokens > 0 and before > 0:
            basis, measured = "measured", measured_prompt_tokens
            scale = min(1.0, max(0.6, measured / before))
        else:
            basis, measured, scale = "estimate", 0, 1.0
        mandatory = limit is not None and before * scale >= limit
        # The hard zone (plan item 5): past H the batch skips the shortfall
        # watermark and must reach the trigger. It still keeps the legacy
        # floor watermark, which only waits for a batch worth 4x the 5%
        # floor; skipping that too made hard-zone batches smaller and more
        # frequent. Full ladder only; the trim-only contract keeps its
        # legacy path.
        hard_at = (
            self._hard_ceiling(context_window, output_reserve_tokens)
            if self.config.enabled else None
        )
        hard = not mandatory and hard_at is not None and before * scale >= hard_at
        minimum = max(1, int(before * 0.05))
        waiver = self.config.window_floor_waiver_ratio
        if (
            mandatory and self.config.enabled and waiver > 0
            and context_window is not None
            and before * scale >= context_window * waiver
        ):
            # Near the window a small reclaim beats a prompt the provider may
            # reject, or bill past the window without a word (plan item 2).
            minimum = min(minimum, max(1, _in_estimate_units(
                self.config.window_floor_waiver_tokens, scale,
            )))
        self._tail_from = self._recency_tail_start(nodes, context_window, scale)
        eligible_chars = self._eligible_chars(nodes)
        basis_fields = {"token_basis": basis, "measured_prompt_tokens": measured}
        decision_fields = {"window_protection": mandatory, **basis_fields}
        if hard_at is not None:
            decision_fields["hard_ceiling_tokens"] = hard_at
            decision_fields["hard_ceiling"] = hard
        if self.config.enabled and context_window is not None:
            self._turn_live = int(self._inflight_estimate(nodes) * scale)
            decision_fields["turn_live_tokens"] = self._turn_live
        if context_window is not None and before * scale > context_window:
            # The prompt is past the configured window. Some providers accept
            # that silently, so the decision has to say so.
            decision_fields["over_window"] = True
        zones = (
            self._zones(context_window, output_reserve_tokens)
            if cache is not None and self.config.enabled and self.config.cache_schedule
            else None
        )
        schedule: Schedule | None = None
        if cache is not None and zones is not None:
            schedule = decide_schedule(
                cache, zones, prompt=before * scale, scale=scale,
                rent_paid=self._rent_paid,
            )
            decision_fields.update(
                schedule=schedule.zone, cold_now=cache.cold_now,
                cold_reason=cache.cold_reason, p_cold=round(cache.p_cold, 4),
                floor_tokens=_in_estimate_units(zones.floor, scale),
                cache_signal=cache.to_dict(),
            )
        sweep = self._boundary_sweep(
            nodes, before_view, scale=scale, schedule=schedule, cache=cache,
            prompt=before * scale,
            trigger=self._threshold(
                self.config.trigger_tokens, context_window, output_reserve_tokens
            ),
            turn_boundary=turn_boundary, decision_fields=decision_fields,
        )
        free_sweep = sweep is not None and sweep.gate == "cold"

        def sweep_alone() -> tuple[list[Message], list[Promotion]] | None:
            # A cold cache makes the sweep free, so it commits on its own
            # when nothing bigger is due or the bigger candidate was dropped.
            if not free_sweep:
                return None
            assert sweep is not None
            return self._sweep_only(
                nodes, before_view, before, sweep,
                overhead_tokens=overhead_tokens, scale=scale,
                decision_fields=decision_fields, basis_fields=basis_fields,
            )

        if schedule is not None and schedule.action == "skip":
            assert zones is not None
            swept = sweep_alone()
            if swept is not None:
                return swept
            trigger_est = _in_estimate_units(zones.trigger, scale)
            if not (
                turn_boundary and before > self._image_threshold(trigger_est)
            ):
                # Below F, or warm between F and the trigger: build nothing,
                # arm nothing, and drop rent paid toward an older candidate.
                # An image rung that would fire still takes the ladder path.
                self._optional_retry = None
                self._rent_paid = 0.0
                self.last_decision = {"reason": "below_threshold",
                                      "before_tokens": before, "after_tokens": before,
                                      "threshold_tokens": trigger_est,
                                      **decision_fields}
                return before_view, []
        retry = self._optional_retry
        if not mandatory and retry is not None and not turn_boundary:
            last_chars, growth = retry
            if eligible_chars < last_chars + growth:
                self.last_decision = {"reason": "awaiting_reclaimable_growth",
                                      "before_tokens": before, "after_tokens": before,
                                      **decision_fields}
                return before_view, []
        shortfall_retry = self._shortfall_retry
        if shortfall_retry is not None and before * scale > self._threshold(
            self.config.trigger_tokens, context_window, output_reserve_tokens
        ):
            # Under the trigger the candidate is a cheap below_threshold, so
            # the watermark only gates calls over it, and it is spent the
            # first time a candidate over the trigger is built.
            last_chars, growth = shortfall_retry
            # A cold cache makes the rewrite free of its re-billed tail, so
            # the priced schedule lets a cold call through the hold.
            cold = schedule is not None and cache is not None and cache.cold_now
            if (
                not mandatory and not hard and not turn_boundary and not cold
                and self._reclaimable_chars(nodes) < last_chars + growth
            ):
                self.last_decision = {"reason": "awaiting_reclaimable_growth",
                                      "before_tokens": before, "after_tokens": before,
                                      "shortfall_tokens": growth // 4,
                                      **decision_fields}
                return before_view, []
            self._shortfall_retry = None
        restore = self._tier_snapshot()
        self._candidate_threshold, self._candidate_below = None, False
        self._candidate_target = None
        opportunistic = (
            schedule is not None and schedule.action == "build"
            and schedule.zone == ZONE_OPPORTUNISTIC
        )
        if opportunistic:
            # Rent is paid toward a priced-band candidate; under the trigger
            # there is none.
            self._rent_paid = 0.0
        rendered, promotions = self._apply_candidate(
            nodes, overhead_tokens=overhead_tokens, context_window=context_window,
            output_reserve_tokens=output_reserve_tokens, turn_boundary=turn_boundary,
            scale=scale, force=opportunistic, sweep=sweep,
        )
        # Reachability: a hard-zone batch still over the trigger would be
        # followed by another one within a few calls, so it runs the guarded
        # escalation, whose stop target is the trigger.
        unreached = (
            hard and self._candidate_threshold is not None
            and self._estimate(nodes, overhead_tokens) > self._candidate_threshold
        )
        if unreached or (
            mandatory and self._estimate(nodes, overhead_tokens) * scale >= limit
        ):
            # Recency-guarded rungs only, the in-flight rung (turn_budget)
            # included, so the hard zone and window protection reach into a
            # long turn. The unguarded rungs trim and digest the newest
            # result and any steer delivered with it; they are for a prompt
            # the provider rejected.
            promotions.extend(self.escalate(
                nodes, overhead_tokens=overhead_tokens, context_window=context_window,
                output_reserve_tokens=output_reserve_tokens, guarded_only=True,
                scale=scale,
            ))
            rendered = self.view(nodes)
        after = overhead_tokens + estimate_tokens_of_messages(rendered)
        reason = (
            "window_protection" if mandatory
            else "hard_ceiling" if hard
            else "meaningful_reclaim"
        )
        threshold_fields = (
            {"threshold_tokens": self._candidate_threshold}
            if self._candidate_threshold is not None else {}
        )
        if self._candidate_target is not None:
            threshold_fields["target_tokens"] = self._candidate_target
        if mandatory:
            decision_fields["window_protection_misses"] = (
                self._window_misses + int(before - after < minimum)
            )
        if before - after < minimum:
            restore()
            if not mandatory and not hard:
                swept = sweep_alone()
                if swept is not None:
                    return swept
            self._window_misses = self._window_misses + 1 if mandatory else 0
            below = self._candidate_below
            # The retry watermark makes later mid-turn calls wait for more
            # reclaimable growth before building another candidate. It is
            # only meant for a prompt over the trigger whose candidate fell
            # short of the floor. Arming it below the trigger, or when the
            # candidate reclaimed nothing, made the first real crossing wait
            # out that growth (B1, plan item 4), so neither case arms it.
            skip_arm = (
                self.config.enabled
                and self.config.below_threshold_skips_retry
                and (below or after >= before)
            ) or opportunistic
            if skip_arm:
                self._optional_retry = None
            else:
                self._optional_retry = (eligible_chars, minimum * 4)
            reason = (
                "below_threshold" if below and not promotions and not mandatory
                else "insufficient_reclaim"
            )
            self.last_decision = {"reason": reason,
                                  "before_tokens": before, "after_tokens": before,
                                  "candidate_after_tokens": after,
                                  **threshold_fields, **decision_fields}
            return before_view, []
        if schedule is not None and schedule.action == "build":
            assert cache is not None and zones is not None
            priced = decide_schedule(
                cache, zones, prompt=before * scale, scale=scale,
                candidate_after=after * scale, rent_paid=self._rent_paid,
            )
            if priced.zone == ZONE_PRICED:
                threshold_fields.update(buy_price=priced.buy_price,
                                        rent_per_call=priced.rent_per_call)
            if priced.action != "commit":
                # warm_defer: keep the cached prefix and pay one more call of
                # rent. A cold opportunistic candidate that is not deep
                # enough is dropped the same way and leaves no rent behind,
                # but a free sweep inside it can still commit on its own.
                restore()
                swept = sweep_alone()
                if swept is not None:
                    return swept
                threshold_fields["rent_paid"] = round(self._rent_paid)
                self._optional_retry = None
                if priced.action == "defer":
                    self._rent_paid += priced.rent_per_call
                else:
                    self._rent_paid = 0.0
                self.last_decision = {"reason": priced.reason,
                                      "before_tokens": before, "after_tokens": before,
                                      "candidate_after_tokens": after,
                                      **threshold_fields, **decision_fields}
                return before_view, []
            reason = priced.reason
        if schedule is not None:
            threshold_fields["rent_paid"] = round(self._rent_paid)
        self._rent_paid = 0.0
        # Shortfall watermark (plan item 5, B2). The legacy commit cleared the
        # watermark, so a batch that could not reach its target was followed
        # by another shallow one as soon as the prompt crossed the trigger
        # again, every few calls in a long turn. When the commit leaves the
        # prompt above its target, the next mid-turn batch waits until the
        # reclaimable growth covers the shortfall. The hard zone and window
        # protection skip the watermark, so it never holds a needed batch.
        self._optional_retry = None
        self._window_misses = 0
        target = self._candidate_target
        shortfall = 0
        if (
            self.config.enabled and self.config.shortfall_watermark
            and context_window is not None
            and target is not None and after > target
        ):
            shortfall = after - target
            self._shortfall_retry = (
                self._reclaimable_chars(nodes), max(minimum, shortfall) * 4,
            )
        else:
            self._shortfall_retry = None
        first_changed = next((i for i, (a, b) in enumerate(zip(before_view, rendered))
                              if a != b), min(len(before_view), len(rendered)))
        self.last_decision = {"reason": reason, "before_tokens": before,
                              "after_tokens": after,
                              "first_changed_message": first_changed,
                              **threshold_fields, **decision_fields}
        if shortfall:
            self.last_decision["shortfall_tokens"] = shortfall
        return rendered, [replace(p, before_tokens=before, after_tokens=after,
                                  decision_reason=reason,
                                  first_changed_message=first_changed,
                                  **basis_fields)
                          for p in promotions]

    def _apply_candidate(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int = 0,
        context_window: int | None = None,
        output_reserve_tokens: int = 0,
        turn_boundary: bool = True,
        scale: float = 1.0,
        force: bool = False,
        sweep: SweepPlan | None = None,
    ) -> tuple[list[Message], list[Promotion]]:
        """Render history; when the estimated total crosses the threshold,
        run the ladder as one amortized batch and return what it did.

        ``sweep`` is a boundary sweep planned by ``apply``. It runs as the
        first size rung of a batch that is due, so it shares the one
        rewrite; when no batch is due it does not run here.

        ``force`` runs the size rungs under the threshold too. The priced
        schedule sets it for a cold call between the opportunistic floor and
        the trigger, where a rewrite costs no extra cache break.

        ``scale`` is the measured/estimated calibration from ``apply``; the
        threshold is divided by it so it compares against chars/4 totals.

        ``turn_boundary`` says whether this is the FIRST model call of a turn.
        The image rung waits for one — rewriting a result between two calls of
        the same turn breaks the prefix the provider cached seconds earlier and
        re-bills the whole prompt. The size rungs do not: past the trigger the
        prompt is close enough to the window that one cache break beats an
        overflow.
        """
        if not self.config.enabled:
            if not self.config.trim.enabled:
                return [n.message for n in nodes], []
            return self._apply_trim_only(
                nodes,
                overhead_tokens=overhead_tokens,
                context_window=context_window,
                output_reserve_tokens=output_reserve_tokens,
                turn_boundary=turn_boundary,
                scale=scale,
            )
        rendered = self._render(nodes)
        threshold = _in_estimate_units(self._threshold(
            self.config.trigger_tokens, context_window, output_reserve_tokens
        ), scale)
        total = overhead_tokens + estimate_tokens_of_messages(rendered)
        # Consume-once image trims: a rung like any other, so they wait for
        # pressure and for a turn boundary rather than rewriting the prefix
        # mid-turn at low occupancy.
        promotions: list[Promotion] = []
        due = total > threshold or force
        if turn_boundary and total > self._image_threshold(threshold):
            promotions = self._consume_images(nodes)
            if promotions:
                rendered = self._render(nodes)
                total = overhead_tokens + estimate_tokens_of_messages(rendered)
        self._candidate_threshold = threshold
        # Whether the batch is due is decided before the images leave. Deciding
        # it after them (the legacy path) let a due batch stop at the images:
        # the size trim then came a few calls later as a second rebuild, or a
        # small image reclaim fell under the 5% floor and nothing ran (B13).
        if not (due if self.config.image_rung_in_batch else total > threshold or force):
            self._candidate_below = True
            return rendered, promotions
        # The rungs below stay available mid-turn on purpose: past the trigger
        # the prompt is genuinely close to the window, and a rewrite that costs
        # one cache break beats a turn that overflows. Only the image rung is
        # held for a turn boundary.
        target = int(threshold * self.config.target_ratio)
        deep = self._deep_target(context_window, scale)
        if deep is not None:
            target = min(target, deep)
        self._candidate_target = target

        # Rung 0: the boundary sweep (plan item 8), when apply planned one.
        if sweep is not None:
            swept = self._run_sweep(nodes, sweep)
            if swept:
                promotions.extend(swept)
                rendered = self._render(nodes)
                total = overhead_tokens + estimate_tokens_of_messages(rendered)

        # Rung 1: batch-trim tool results.
        pairs = self.trimmer.trim_batch(nodes, protected_from=self._tail_from)
        if pairs:
            before = estimate_tokens_of_messages(rendered)
            rendered = self._render(nodes)
            promotions.append(
                Promotion(
                    tier=1,
                    trimmed=tuple(pairs),
                    reclaimed_chars=max(
                        0,
                        (before - estimate_tokens_of_messages(rendered)) * 4,
                    ),
                )
            )
            total = overhead_tokens + estimate_tokens_of_messages(rendered)

        # Rung 2: digest oldest eligible spans until under target.
        if total > target:
            promoted = self._digest_until(
                nodes, overhead_tokens=overhead_tokens, target=target
            )
            if promoted:
                before = estimate_tokens_of_messages(rendered)
                rendered = self._render(nodes)
                promotions.append(
                    Promotion(
                        tier=2,
                        spans=tuple(
                            (s, e, SPAN_DIGEST) for s, e in promoted
                        ),
                        reclaimed_chars=max(
                            0,
                            (before - estimate_tokens_of_messages(rendered))
                            * 4,
                        ),
                    )
                )

        # Rung 3: fold surplus digests into epoch stubs (residency rule —
        # keeps the resident cost of N turns near-constant).
        restore = self._tier_snapshot()
        folded = self._fold_epochs(nodes)
        if folded:
            before = estimate_tokens_of_messages(rendered)
            folded_render = self._render(nodes)
            reclaimed = max(
                0, (before - estimate_tokens_of_messages(folded_render)) * 4
            )
            if reclaimed:
                rendered = folded_render
                promotions.append(
                    Promotion(
                        tier=3,
                        spans=tuple((s, e, EPOCH_STUB) for s, e in folded),
                        reclaimed_chars=reclaimed,
                    )
                )
            else:
                # An escalation that frees nothing is pure cost: it breaks the
                # provider's cache prefix, writes a ContextCompaction event
                # describing no change, and leaves the ladder a rung higher for
                # the next batch. Roll it back; the digests stay digests.
                restore()
        return rendered, promotions

    def _boundary_sweep(
        self,
        nodes: list[SessionNode],
        before_view: list[Message],
        *,
        scale: float,
        schedule: Schedule | None,
        cache: CacheSignal | None,
        prompt: float,
        trigger: int,
        turn_boundary: bool,
        decision_fields: dict[str, Any],
    ) -> SweepPlan | None:
        """Plan the boundary sweep for this call, log it in the decision, and
        return it when ``sweep_mode`` is "on" and the schedule lets it run.

        The plan is made at every turn boundary of the full ladder while the
        mode is not "off", so shadow mode logs what the sweep would do.
        ``prompt`` and ``trigger`` are measured tokens.
        """
        mode = self.config.sweep_mode
        if (
            not turn_boundary or mode not in ("shadow", "on")
            or not self.config.enabled or not self.trimmer.config.enabled
        ):
            return None
        plan = self._plan_sweep(nodes, before_view, scale)
        if not (plan.pairs or plan.images):
            gate = "empty"
        elif schedule is not None and cache is not None:
            # Free when cold. Warm below the trigger it waits; at or over the
            # trigger it is part of the batch the schedule prices anyway.
            if cache.cold_now:
                gate = "cold"
            elif schedule.zone in (ZONE_BELOW_FLOOR, ZONE_OPPORTUNISTIC):
                gate = "warm_defer"
            else:
                gate = "batch"
        else:
            gate = "batch" if prompt > trigger else "below_threshold"
        plan = replace(plan, gate=gate)
        decision_fields.update(plan.fields(mode))
        if mode != "on" or gate not in ("cold", "batch"):
            return None
        return plan

    def _plan_sweep(
        self, nodes: list[SessionNode], before_view: list[Message], scale: float,
    ) -> SweepPlan:
        """What a sweep would take now. Pure for the caller: it runs the
        sweep on the live state to measure it and then puts the state back.

        Candidates are the trimmable results before the in-flight turn
        (non-error, at least ``trim.min_result_chars``, not inside a digest).
        Walking them newest first, the keep holds results while they fit in
        ``sweep_keep_tokens``; the first one that does not fit and every
        older one is swept, so the keep is always the newest content. Pinned
        results are skipped and do not count toward the keep.
        """
        spans = split_spans(nodes)
        if len(spans) < 2:
            return SweepPlan()
        inflight = len(nodes) - len(spans[-1])
        covered = self._covered_ids(nodes)
        restore = self._tier_snapshot()
        try:
            images = tuple(self.trimmer.consume_images(nodes))
            candidates = [
                p for p in self.trimmer._eligible(nodes, protected_from=inflight)
                if p[0] not in covered
            ]
            sizes = _result_text_chars(nodes, candidates)
            pins, pinned_chars = self._sweep_pins(
                nodes, spans[-2], candidates, sizes, scale,
            )
            pinned = set(pins)
            keep = max(0, _in_estimate_units(self.config.sweep_keep_tokens, scale)) * 4
            kept = 0
            keeping = True
            swept: list[tuple[str, str]] = []
            for pair in reversed(candidates):
                if pair in pinned:
                    continue
                if keeping and kept + sizes[pair] <= keep:
                    kept += sizes[pair]
                    continue
                keeping = False
                swept.append(pair)
            swept.reverse()
            if not swept and not images:
                return SweepPlan(pinned=tuple(pins), pinned_chars=pinned_chars)
            self.trimmer.mark_trimmed(
                swept, render_version=self.trimmer.render_version
            )
            rendered = self._render(nodes)
        finally:
            restore()
        first_changed = next(
            (i for i, (a, b) in enumerate(zip(before_view, rendered)) if a != b),
            None,
        )
        return SweepPlan(
            pairs=tuple(swept), images=images, pinned=tuple(pins),
            pinned_chars=pinned_chars,
            reclaim_tokens=max(
                0,
                estimate_tokens_of_messages(before_view)
                - estimate_tokens_of_messages(rendered),
            ),
            first_changed=first_changed,
        )

    def _sweep_pins(
        self,
        nodes: list[SessionNode],
        previous: list[SessionNode],
        candidates: list[tuple[str, str]],
        sizes: dict[tuple[str, str], int],
        scale: float,
    ) -> tuple[list[tuple[str, str]], int]:
        """Candidates the sweep leaves for one more turn, and their chars.

        A HistoryExpand call in the turn that just ended (``previous``), as
        a tool call or inside a code-mode cell, pins its own result and the
        results of the nodes it expanded: the model asked for them and is
        likely still using them. Newest first, up to ``sweep_pin_tokens``.
        """
        cap = max(0, _in_estimate_units(self.config.sweep_pin_tokens, scale)) * 4
        if cap <= 0:
            return [], 0
        index = {n.node_id: i for i, n in enumerate(nodes)}
        expand_calls: set[str] = set()
        expanded: set[str] = set()
        for node in previous:
            for block in node.message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                refs = _expand_refs(block)
                if not refs:
                    continue
                expand_calls.add(block.id)
                for start, end in refs:
                    if start in index and end in index:
                        i, j = sorted((index[start], index[end]))
                        expanded.update(n.node_id for n in nodes[i : j + 1])
        if not expand_calls:
            return [], 0
        pins: list[tuple[str, str]] = []
        used = 0
        for pair in reversed(candidates):
            if pair[1] not in expand_calls and pair[0] not in expanded:
                continue
            if used + sizes[pair] > cap:
                break
            pins.append(pair)
            used += sizes[pair]
        pins.reverse()
        return pins, used

    def _run_sweep(
        self, nodes: list[SessionNode], plan: SweepPlan
    ) -> list[Promotion]:
        """Commit a planned sweep to the sticky state: one image_consumed
        promotion for the images and one tier-1 ``sweep`` promotion for the
        results. Anything an earlier rung of the same batch already took is
        skipped, so each pair is decided once."""
        trimmer = self.trimmer
        promotions: list[Promotion] = []
        images = [
            p for p in plan.images
            if p not in trimmer._image_consumed and p not in trimmer._trimmed
        ]
        if images:
            trimmer.mark_image_consumed(images)
            promotions.append(Promotion(
                tier=1, reason="image_consumed", trimmed=tuple(images),
                reclaimed_chars=_chars_of(nodes, images),
            ))
        pairs = [p for p in plan.pairs if p not in trimmer._trimmed]
        if pairs:
            before = estimate_tokens_of_messages(self._render(nodes))
            # A live decision renders at this process's version, the same
            # version rehydrate gives it once the event carries the field.
            trimmer.mark_trimmed(pairs, render_version=trimmer.render_version)
            promotions.append(Promotion(
                tier=1, reason="sweep", trimmed=tuple(pairs),
                reclaimed_chars=max(
                    0,
                    (before - estimate_tokens_of_messages(self._render(nodes))) * 4,
                ),
            ))
        return promotions

    def _sweep_only(
        self,
        nodes: list[SessionNode],
        before_view: list[Message],
        before: int,
        plan: SweepPlan,
        *,
        overhead_tokens: int,
        scale: float,
        decision_fields: dict[str, Any],
        basis_fields: dict[str, Any],
    ) -> tuple[list[Message], list[Promotion]] | None:
        """Commit a free sweep as its own batch, or put the state back and
        return None when it frees less than ``sweep_min_reclaim_tokens``."""
        restore = self._tier_snapshot()
        promotions = self._run_sweep(nodes, plan)
        if not promotions:
            return None
        rendered = self.view(nodes)
        after = overhead_tokens + estimate_tokens_of_messages(rendered)
        floor = max(1, _in_estimate_units(self.config.sweep_min_reclaim_tokens, scale))
        if before - after < floor:
            restore()
            return None
        self._optional_retry = None
        self._rent_paid = 0.0
        first_changed = next(
            (i for i, (a, b) in enumerate(zip(before_view, rendered)) if a != b),
            min(len(before_view), len(rendered)),
        )
        self.last_decision = {"reason": "sweep", "before_tokens": before,
                              "after_tokens": after,
                              "first_changed_message": first_changed,
                              **decision_fields}
        return rendered, [replace(p, before_tokens=before, after_tokens=after,
                                  decision_reason="sweep",
                                  first_changed_message=first_changed,
                                  **basis_fields)
                          for p in promotions]

    def _threshold(
        self,
        base: int,
        context_window: int | None,
        output_reserve_tokens: int,
        window_ratio: float | None = None,
    ) -> int:
        """``base`` scaled to a known window, capped to keep headroom free."""
        return derive_trigger_tokens(
            base=base,
            window_ratio=(
                self.config.trigger_window_ratio
                if window_ratio is None
                else window_ratio
            ),
            context_window=context_window,
            headroom_tokens=self.config.headroom_tokens,
            output_reserve_tokens=output_reserve_tokens,
        )

    def _deep_target(self, context_window: int | None, scale: float) -> int | None:
        """The deep batch target T in chars/4 units, or None for the legacy
        target (no known window, or ``deep_target_window_ratio`` of 0)."""
        ratio = self.config.deep_target_window_ratio
        if context_window is None or ratio <= 0:
            return None
        return _in_estimate_units(int(context_window * ratio), scale)

    def _zones(
        self, context_window: int | None, output_reserve_tokens: int
    ) -> Zones | None:
        """The priced schedule's zones in measured tokens, or None when the
        window is unknown (the schedule then leaves the ladder unpriced)."""
        if context_window is None:
            return None
        limit = max(
            1, context_window - self.config.headroom_tokens - output_reserve_tokens
        )
        trigger = self._threshold(
            self.config.trigger_tokens, context_window, output_reserve_tokens
        )
        target = int(trigger * self.config.target_ratio)
        if self.config.deep_target_window_ratio > 0:
            target = min(
                target, int(context_window * self.config.deep_target_window_ratio)
            )
        ratio = self.config.opportunistic_floor_window_ratio
        floor = min(trigger, int(context_window * ratio)) if ratio > 0 else trigger
        return Zones(
            floor=floor, trigger=trigger,
            hard=self._hard_ceiling(context_window, output_reserve_tokens),
            target=target, limit=limit,
        )

    def schedule_event_fields(self) -> dict[str, Any]:
        """The last decision's schedule fields for its ContextCompaction
        events. Sizes are in the chars/4 units of ``before_tokens`` except
        ``rent_paid`` and ``turn_live_tokens``, which are measured tokens."""
        d = self.last_decision
        return {
            "schedule": str(d.get("schedule") or ""),
            "cold_reason": str(d.get("cold_reason") or ""),
            "threshold_tokens": int(d.get("threshold_tokens") or 0),
            "target_tokens": int(d.get("target_tokens") or 0),
            "rent_paid": int(d.get("rent_paid") or 0),
            "turn_live_tokens": int(d.get("turn_live_tokens") or 0),
        }

    def _hard_ceiling(
        self, context_window: int | None, output_reserve_tokens: int
    ) -> int | None:
        """The hard ceiling H in measured tokens, or None when there is no
        hard zone: no known window, a ratio of 0, or an H at or below the
        trigger. On a small window the trigger already sits at the limit,
        and window protection covers that band."""
        ratio = self.config.hard_window_ratio
        if context_window is None or ratio <= 0:
            return None
        limit = max(
            1, context_window - self.config.headroom_tokens - output_reserve_tokens
        )
        hard = min(int(context_window * ratio), limit - self.config.hard_margin_tokens)
        trigger = self._threshold(
            self.config.trigger_tokens, context_window, output_reserve_tokens
        )
        return hard if hard > trigger else None

    def _eligible_chars(self, nodes: list[SessionNode]) -> int:
        """Raw chars the guarded rungs could reclaim now. The retry watermark
        measures growth in these."""
        chars = _chars_of(nodes, self.trimmer._eligible(
            nodes, protected_from=self._tail_from,
        ))
        if self.config.enabled:
            chars += sum(
                block_chars(b) for span in self._eligible_spans(nodes)
                for node in span for b in node.message.content
            )
        return chars

    def _reclaimable_chars(self, nodes: list[SessionNode]) -> int:
        """Chars the guarded rungs could take out of the current view: the
        rendered size of every eligible span plus every trimmable result
        outside those spans.

        ``_eligible_chars`` counts a span at its raw size, results that are
        already placeholders included, and counts an untrimmed result twice
        when its span is eligible too. That is fine for the legacy floor
        watermark, but it would let the shortfall watermark pass long before
        a batch could really reclaim the shortfall.
        """
        trimmed = self.trimmer._trimmed
        consumed = self.trimmer._image_consumed
        chars = 0
        in_spans: set[str] = set()
        if self.config.enabled:
            for span in self._eligible_spans(nodes):
                for node in span:
                    in_spans.add(node.node_id)
                    chars += sum(
                        block_chars(b)
                        for b in self._render_live(node, trimmed, consumed).content
                    )
        pairs = [
            p for p in self.trimmer._eligible(nodes, protected_from=self._tail_from)
            if p[0] not in in_spans
        ]
        return chars + _chars_of(nodes, pairs)

    def _image_threshold(self, threshold: int) -> int:
        """Estimated tokens at which the consume-once image rung arms.

        ``image_trigger_ratio`` scales the ladder's own threshold, so an image
        never leaves context on a session that is nowhere near its window.
        A ratio of 0 pins it out of reach: images then survive until overflow
        recovery, which is the only rung that ignores every guard.
        """
        ratio = self.config.image_trigger_ratio
        if ratio <= 0:
            return 1 << 62
        return int(threshold * ratio)

    def _cap_live_images(self, nodes: list[SessionNode]) -> list[Promotion]:
        """The image count rung (see ``live_image_cap``); [] when not due."""
        cap = self.config.live_image_cap
        if cap <= 0 or not self.active:
            return []
        if len(self.trimmer.live_tool_images(nodes)) <= cap:
            return []
        newly = self.trimmer.consume_stale_tool_images(
            nodes, keep=self.config.live_image_keep
        )
        if not newly:
            return []
        return [
            Promotion(
                tier=1,
                reason="image_consumed",
                trimmed=tuple(newly),
                reclaimed_chars=_chars_of(nodes, newly),
            )
        ]

    def _consume_images(self, nodes: list[SessionNode]) -> list[Promotion]:
        """Persist consume-once image trims and report them as a promotion.

        An image dropping out of context is a real loss — HistoryExpand cannot
        put it back — so it must not happen silently. Reporting it as a tier-1
        promotion with its
        own reason gives it a ``ContextCompaction`` event, and makes the
        decision replay on resume like every other rung's.
        """
        newly = self.trimmer.consume_images(nodes)
        if not newly:
            return []
        return [
            Promotion(
                tier=1,
                reason="image_consumed",
                trimmed=tuple(newly),
                reclaimed_chars=_chars_of(nodes, newly),
            )
        ]

    def consume_images_after_rejection(
        self, nodes: list[SessionNode], *, overhead_tokens: int = 0
    ) -> list[Promotion]:
        """Recovery for a request the provider rejected for its payload.

        A non-size ``invalid_request_error`` about an image (a decoded image
        over the provider's patch limit, say) is not an overflow, so the size
        ladder does not apply. The one thing compaction can do is the image
        rung: consume every image a completed model call already saw, with no
        pressure trigger and no turn boundary, as ``escalate`` runs it. The
        rung is idempotent, so a second rejection finds nothing new and the
        engine surfaces the error instead of retrying.
        """
        if not self.active:
            return []
        before_view = self.view(nodes)
        before = self._estimate(nodes, overhead_tokens)
        newly = self.trimmer.consume_images(nodes)
        if not newly:
            return []
        after = self._estimate(nodes, overhead_tokens)
        after_view = self.view(nodes)
        # Where the rewrite starts, as escalate reports it, so the engine can
        # log the tail the retry re-bills.
        first_changed = next((i for i, (a, b) in enumerate(zip(before_view, after_view))
                              if a != b), min(len(before_view), len(after_view)))
        return [
            Promotion(
                tier=1,
                reason="image_consumed",
                trimmed=tuple(newly),
                reclaimed_chars=_chars_of(nodes, newly),
                before_tokens=before,
                after_tokens=after,
                decision_reason="provider_payload_rejection",
                first_changed_message=first_changed,
            )
        ]

    def _apply_trim_only(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int,
        context_window: int | None,
        output_reserve_tokens: int,
        turn_boundary: bool = True,
        scale: float = 1.0,
    ) -> tuple[list[Message], list[Promotion]]:
        """Tier 1 on its own: trim tool results, never digest a turn.

        Same batch-and-sticky discipline as the full ladder (rare, amortized,
        cache-safe), driven by the tier-1 trigger rather than the ladder's —
        image consumption included.
        """
        rendered = self._render(nodes)
        threshold = _in_estimate_units(self._threshold(
            self.config.trim.trigger_tokens,
            context_window,
            output_reserve_tokens,
            window_ratio=self.config.trim.trigger_window_ratio,
        ), scale)
        total = overhead_tokens + estimate_tokens_of_messages(rendered)
        promotions: list[Promotion] = []
        if turn_boundary and total > self._image_threshold(threshold):
            promotions = self._consume_images(nodes)
            if promotions:
                rendered = self._render(nodes)
                total = overhead_tokens + estimate_tokens_of_messages(rendered)
        self._candidate_threshold = threshold
        # The legacy early return after the image rung stays here on purpose:
        # the trim-only contract is byte-identical, so image_rung_in_batch
        # (B13) applies to the full ladder only.
        if total <= threshold:
            self._candidate_below = True
            return rendered, promotions
        pairs = self.trimmer.trim_batch(nodes)
        if not pairs:
            return rendered, promotions
        before = estimate_tokens_of_messages(rendered)
        rendered = self._render(nodes)
        return rendered, [
            *promotions,
            Promotion(
                tier=1,
                trimmed=tuple(pairs),
                reclaimed_chars=max(
                    0, (before - estimate_tokens_of_messages(rendered)) * 4
                ),
            ),
        ]

    def escalate(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int = 0,
        context_window: int | None = None,
        output_reserve_tokens: int = 0,
        guarded_only: bool = False,
        scale: float = 1.0,
        stop_tokens: int | None = None,
        decision_reason: str = "provider_overflow",
    ) -> list[Promotion]:
        """Overflow recovery: the provider rejected the prompt, so reclaim.

        Rungs run in order and each is sticky and persisted like a trigger
        batch, so a resume rebuilds the same view. The first three are the
        recency-guarded tiers (trim old results, digest old turns, fold
        digests). Two more follow with the recency guard off: trim every
        result, then digest every turn including the in-flight one. They
        exist because the payload that overflowed is usually the newest one,
        which is exactly what the guards protect; with the defaults the
        guarded rungs could not touch ~62k tokens of fresh results.

        With an unknown window one rung runs per call (the engine retries
        after each). With a known window rungs keep running until the
        estimate is under the normal trigger, so one retry usually suffices.

        ``guarded_only`` drops the two unguarded rungs. ``apply`` sets it for
        window protection and the hard zone, which act on a size check before
        any request is sent; only a prompt the provider actually rejected may
        trim the newest result or digest the in-flight turn whole. The
        guarded list reaches into the in-flight turn only through the
        in-flight rung below. ``scale`` is ``apply``'s measured/estimated factor;
        the stop target is divided by it. Provider-overflow recovery leaves
        it at 1.

        The guarded list ends with the in-flight rung (reason
        ``turn_budget``, plan item 2). When the in-flight turn holds more
        than ``turn_budget_window_ratio`` of the window and the prompt is at
        or above ``turn_budget_floor_ratio`` of it, the rung trims results
        older than the recency tail. If the prompt is still at or over the
        window-protection limit, it narrows the tail to the newest
        ``recency_tail_calls`` calls, trims what that frees, and then
        digests completed in-flight segments before it. It never touches
        the newest calls or the turn-start node. That is the only hard cap
        on a provider that bills past the configured window instead of
        rejecting the prompt.
        """
        if not self.active:
            return []
        # Provider-overflow recovery keeps the count-based guards on its
        # guarded rungs; the unguarded rungs follow them at once anyway. The
        # guarded escalation (window protection and the hard zone) uses the
        # recency tail ``apply`` set, or works one out when called alone.
        owns_tail = guarded_only and self._tail_from is None
        if owns_tail:
            self._tail_from = self._recency_tail_start(nodes, context_window, scale)
            self._turn_live = int(self._inflight_estimate(nodes) * scale)
        try:
            return self._escalate(
                nodes, overhead_tokens=overhead_tokens,
                context_window=context_window,
                output_reserve_tokens=output_reserve_tokens,
                guarded_only=guarded_only, scale=scale,
                stop_tokens=stop_tokens, decision_reason=decision_reason,
            )
        finally:
            if owns_tail:
                self._tail_from = None
                self._turn_live = None

    def _escalate(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int,
        context_window: int | None,
        output_reserve_tokens: int,
        guarded_only: bool,
        scale: float,
        stop_tokens: int | None = None,
        decision_reason: str = "provider_overflow",
    ) -> list[Promotion]:
        trim_only = not self.config.enabled
        target: int | None = None
        if context_window is not None:
            target = (
                self._threshold(
                    self.config.trim.trigger_tokens,
                    context_window,
                    output_reserve_tokens,
                    window_ratio=self.config.trim.trigger_window_ratio,
                )
                if trim_only
                else self._threshold(
                    self.config.trigger_tokens, context_window, output_reserve_tokens
                )
            )
        if target is not None:
            target = _in_estimate_units(target, scale)
        if stop_tokens is not None:
            # A caller-chosen stop (already in chars/4 units), lower than the
            # trigger: an owner-requested compaction goes to the deep target.
            target = stop_tokens if target is None else min(target, stop_tokens)
        digest_target = target or 0

        def images() -> Promotion | None:
            # Overflow is maximal pressure and the model has already seen every
            # one of these, so this rung ignores the trigger and the turn
            # boundary the ordinary path respects. Cheapest reclaim first.
            newly = self.trimmer.consume_images(nodes)
            if not newly:
                return None
            return Promotion(
                tier=1, reason="image_consumed", trimmed=tuple(newly)
            )

        def trim(keep: int | None) -> Callable[[], Promotion | None]:
            def rung() -> Promotion | None:
                pairs = self.trimmer.trim_batch(
                    nodes, keep_recent=keep,
                    protected_from=self._tail_from if keep is None else None,
                )
                if not pairs:
                    return None
                return Promotion(tier=1, reason="overflow", trimmed=tuple(pairs))
            return rung

        def digest(keep: int | None) -> Callable[[], Promotion | None]:
            def rung() -> Promotion | None:
                promoted = self._digest_until(
                    nodes,
                    overhead_tokens=overhead_tokens,
                    target=digest_target,
                    keep_recent=keep,
                )
                if not promoted:
                    return None
                return Promotion(
                    tier=2,
                    reason="overflow",
                    spans=tuple((s, e, SPAN_DIGEST) for s, e in promoted),
                )
            return rung

        def fold() -> Promotion | None:
            folded = self._fold_epochs(nodes, force=True)
            if not folded:
                return None
            return Promotion(
                tier=3,
                reason="overflow",
                spans=tuple((s, e, EPOCH_STUB) for s, e in folded),
            )

        limit = (
            max(1, context_window - self.config.headroom_tokens - output_reserve_tokens)
            if context_window is not None
            else None
        )

        def turn_budget_from() -> int | None:
            """Where the in-flight rung's protection starts, or None when the
            rung does not apply to the prompt as it stands now."""
            ratio = self.config.turn_budget_window_ratio
            if trim_only or ratio <= 0 or context_window is None or limit is None:
                return None
            # The turn's size when the decision started: the rungs before
            # this one shrink it, and a turn that was heavy stays in reach.
            if (self._turn_live or 0) <= context_window * ratio:
                return None
            current = self._estimate(nodes, overhead_tokens) * scale
            if current < context_window * self.config.turn_budget_floor_ratio:
                return None
            if current >= limit:
                # Still at or over the limit after every other guarded rung:
                # only the newest calls and the turn-start node stay.
                return self._newest_calls_start(nodes)
            return self._tail_from

        def turn_budget_trim() -> Promotion | None:
            protected_from = turn_budget_from()
            if protected_from is None:
                return None
            pairs = self.trimmer.trim_batch(nodes, protected_from=protected_from)
            if not pairs:
                return None
            return Promotion(tier=1, reason="turn_budget", trimmed=tuple(pairs))

        def turn_budget_digest() -> Promotion | None:
            protected_from = turn_budget_from()
            if (
                protected_from is None or limit is None
                or not self.config.recall_tools
                or self._estimate(nodes, overhead_tokens) * scale < limit
            ):
                return None
            index = {n.node_id: i for i, n in enumerate(nodes)}
            covered = self._covered_ids(nodes)
            segments = [
                seg for seg in completed_segments(
                    split_spans(nodes)[-1],
                    error_text_cap=self.config.error_text_cap,
                )
                if index[seg[-1].node_id] < protected_from
                and seg[0].node_id not in covered
            ]
            promoted = self._digest_until(
                nodes, overhead_tokens=overhead_tokens, target=digest_target,
                eligible=segments,
            )
            if not promoted:
                return None
            return Promotion(
                tier=2, reason="turn_budget",
                spans=tuple((s, e, SPAN_DIGEST) for s, e in promoted),
            )

        rungs: list[Callable[[], Promotion | None]] = (
            [images, trim(None), trim(0)]
            if trim_only
            else [images, trim(None), digest(None), fold, trim(0), digest(0)]
        )
        if guarded_only:
            rungs = rungs[:2] if trim_only else rungs[:4]
            if not trim_only and self.config.turn_budget_window_ratio > 0:
                rungs += [turn_budget_trim, turn_budget_digest]
        promotions: list[Promotion] = []
        for rung in rungs:
            before_view = self.view(nodes)
            before = self._estimate(nodes, overhead_tokens)
            restore = self._tier_snapshot()
            promo = rung()
            if promo is None:
                continue
            after = self._estimate(nodes, overhead_tokens)
            reclaimed = max(0, (before - after) * 4)
            if not reclaimed:
                # This rung changed the view's shape without making it smaller.
                # Keeping it would spend a cache break and a compaction event
                # on nothing, and would report progress the retry does not
                # have; undo it and let the next rung try.
                restore()
                continue
            after_view = self.view(nodes)
            first_changed = next((i for i, (a, b) in enumerate(zip(before_view, after_view))
                                  if a != b), min(len(before_view), len(after_view)))
            promotions.append(replace(
                promo, reclaimed_chars=reclaimed, before_tokens=before, after_tokens=after,
                decision_reason=decision_reason, first_changed_message=first_changed,
            ))
            if target is None or after <= target:
                break
        return promotions

    def compact_now(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int = 0,
        context_window: int | None = None,
        output_reserve_tokens: int = 0,
        scale: float = 1.0,
        pending_summary: tuple[str, str, str] | None = None,
    ) -> list[Promotion]:
        """Compact on request (a host's "compact now"), with the guarded ladder.

        The same rungs window protection and the hard zone run before a
        request (:meth:`escalate` with ``guarded_only``): trim old results,
        digest old turns, fold digests, then the in-flight budget rungs. The
        recency guards stay on, so the newest work is never digested. With a
        known window it stops at the deep target rather than the trigger; with
        none, one rung runs, as in overflow recovery. Decisions are sticky and
        persisted like any batch. Returns the promotions, empty when nothing
        could be reclaimed.
        """
        if not self.active:
            return []
        promotions = self.escalate(
            nodes,
            overhead_tokens=overhead_tokens,
            context_window=context_window,
            output_reserve_tokens=output_reserve_tokens,
            guarded_only=True,
            scale=scale,
            stop_tokens=self._deep_target(context_window, scale),
            decision_reason="manual",
        )
        if pending_summary is not None:
            promo = self.apply_summary(nodes, pending_summary, overhead_tokens=overhead_tokens,
                                       decision_reason="manual")
            if promo is not None:
                promotions.append(promo)
        return promotions

    def _summary_message(self, start: str, end: str, text: str) -> Message:
        ref = start if start == end else f"{start}..{end}"
        header = (
            f"[checkpoint summary of {ref} — written by the model from the compacted history; "
            f'originals intact: HistoryExpand("{ref}") re-reads verbatim, HistorySearch finds text]'
            if self.config.recall_tools else
            f"[checkpoint summary of {ref} — written by the model from the compacted history; "
            "the originals are not recoverable in this session, so do not reconstruct "
            "their detail from memory]"
        )
        return Message(role="user", content=[Text(text=f"{header}\n{_outrank_line(self.config)}\n{text}")])

    def summary_region(
        self, nodes: list[SessionNode], *, context_window: int | None = None,
        scale: float = 1.0, due_only: bool = False,
    ) -> tuple[str, str, str] | None:
        """A whole-turn compacted prefix, frozen before starting the side call.

        Never includes a protected turn or the recency tail. Input truncation
        preserves an earlier checkpoint and the newest rendered material; the
        original nodes remain recoverable even when the input cap is reached.
        """
        if not self.config.enabled or not self.config.summary_enabled or not nodes:
            return None
        self._settle_replay(nodes)
        index = {n.node_id: i for i, n in enumerate(nodes)}
        spans = split_spans(nodes)
        protected = self._protected_spans(spans, self.config.keep_recent_turns, wake_aware=True)
        guard = min((index[spans[i][0].node_id] for i in protected), default=len(nodes))
        tail = self._recency_tail_start(nodes, context_window, scale)
        if tail is not None:
            guard = min(guard, tail)
        ends = {span[-1].node_id for span in spans}
        eligible = [(start, end) for start, end in self._digested
                    if start in index and end in index and index[start] <= index[end] < guard]
        whole = [(start, end) for start, end in eligible if end in ends]
        if not whole:
            return None
        end = max(whole, key=lambda key: index[key[1]])[1]
        stop = index[end] + 1
        residue = sum(estimate_tokens_of_messages(self._render(nodes[index[s]:index[e] + 1]))
                      for s, e in eligible if index[e] < stop)
        if due_only and (not context_window or residue * scale <=
                         context_window * self.config.summary_trigger_window_ratio):
            return None
        # Do not repeatedly summarize an unchanged checkpoint.
        if (nodes[0].node_id, end) in self._summaries:
            return None
        rendered = self._render(nodes[:stop])
        def content_text(block: Any) -> str:
            if isinstance(block, Text):
                return block.text
            if isinstance(block, ToolUseBlock):
                return f"tool_use {block.id} {block.name}: {json.dumps(block.input, ensure_ascii=False)}"
            if isinstance(block, ToolResultBlock):
                body = "\n".join([*(content_text(part) for part in block.content),
                                  *(steer.render() for steer in block.delivered_steers)])
                return f"tool_result {block.tool_use_id} error={block.is_error}: {body}"
            # Checkpoints are text-only. Never send image bytes or opaque/signed
            # reasoning blobs to a side model as though they were conversation.
            return f"[{type(block).__name__} omitted; recover the original node for detail]"

        text = "\n\n".join(f"[{m.role}]\n" + "\n".join(content_text(b) for b in m.content)
                            for m in rendered)
        cap = max(1, self.config.summary_input_cap_tokens) * 4
        if len(text) > cap:
            previous = next((m.text() for m in rendered
                             if m.text().startswith("[checkpoint summary")), "")
            marker = "\n[older material omitted to fit summary input budget]\n"[:cap]
            prefix = previous[:max(0, cap - len(marker) - 1)]
            tail_chars = cap - len(prefix) - len(marker)
            text = prefix + marker + (text[-tail_chars:] if tail_chars else "")
        return nodes[0].node_id, end, text

    def apply_summary(
        self, nodes: list[SessionNode], pending: tuple[str, str, str], *,
        overhead_tokens: int = 0, decision_reason: str = "summary_batch",
    ) -> Promotion | None:
        """Commit persisted text only on its original path and only if it saves."""
        if not self.config.enabled or not self.config.summary_enabled:
            return None
        start, end, text = pending
        index = {n.node_id: i for i, n in enumerate(nodes)}
        if not text.strip() or start not in index or end not in index or index[start] > index[end]:
            return None
        before = self._estimate(nodes, overhead_tokens)
        restore = self._tier_snapshot()
        for key in list(self._digested):
            if key[0] in index and key[1] in index and index[start] <= index[key[0]] <= index[key[1]] <= index[end]:
                self._retire(key)
        self._mark_span(start, end, "summary", pin_end=nodes[-1].node_id)
        self._summaries[(start, end)] = text
        after = self._estimate(nodes, overhead_tokens)
        if after >= before:
            restore()
            return None
        return Promotion(tier=4, reason="summary", summary_text=text,
                         spans=((start, end, "summary"),), before_tokens=before,
                         after_tokens=after, reclaimed_chars=(before - after) * 4,
                         decision_reason=decision_reason, first_changed_message=0)

    def projected_after_compact(
        self, nodes: list[SessionNode], *, overhead_tokens: int = 0,
        context_window: int | None = None, output_reserve_tokens: int = 0,
        scale: float = 1.0, pending: tuple[str, str, str] | None = None,
    ) -> int:
        """Dry-run manual compaction, in calibrated tokens, without mutating state."""
        from copy import deepcopy

        candidate = deepcopy(self)
        candidate.compact_now(nodes, overhead_tokens=overhead_tokens,
                              context_window=context_window,
                              output_reserve_tokens=output_reserve_tokens, scale=scale)
        ladder = candidate._estimate(nodes, overhead_tokens)
        region = candidate.summary_region(nodes, context_window=context_window, scale=scale)
        if region is None:
            return int(ladder * scale)
        start, end, _ = region
        stop = next(i + 1 for i, n in enumerate(nodes) if n.node_id == end)
        # A ready checkpoint has an exact rendered estimate. Otherwise reserve
        # the configured output budget plus its recoverability header.
        text = pending[2] if pending and pending[:2] == (start, end) else ""
        summary_tokens = (estimate_tokens_of_messages([candidate._summary_message(start, end, text)])
                          + (0 if text else self.config.summary_max_output_tokens / max(scale, 0.01)))
        projected = overhead_tokens + summary_tokens + estimate_tokens_of_messages(candidate.view(nodes[stop:]))
        return int(min(ladder, projected) * scale)

    def estimate_view_tokens(self, nodes: list[SessionNode], overhead_tokens: int = 0) -> int:
        """Estimated tokens of the current view (chars/4 units)."""
        return self._estimate(nodes, overhead_tokens)

    def _estimate(self, nodes: list[SessionNode], overhead_tokens: int) -> int:
        """Estimated tokens the next request carries under the current view."""
        return overhead_tokens + estimate_tokens_of_messages(self._render(nodes))

    def rehydrate(self, promotions: Iterable[Promotion]) -> None:
        """Re-apply persisted decisions (resume). Rendering is deterministic,
        so the rehydrated view is byte-identical to the pre-restart one.

        Each promotion renders at its own ``render_version``, capped at this
        compactor's, so decisions persisted before version 2 existed keep
        their version-1 text.

        The engine writes the compactor's version on every ContextCompaction
        event and passes it back here on resume. Events written before the
        field existed read as version 1, which is how they rendered.
        """
        for promo in promotions:
            version = min(promo.render_version, self.config.render_version)
            # The two tier-1 reasons rewrite different amounts of the block, so
            # they replay into different sets: an image consumption that came
            # back as a full trim would take the sibling text with it and the
            # resumed prefix would not match the pre-restart one.
            if promo.reason == "image_consumed":
                self.trimmer.mark_image_consumed(promo.trimmed)
            else:
                self.trimmer.mark_trimmed(promo.trimmed, render_version=version)
            for start, end, kind in promo.spans:
                if not self.config.enabled and kind != "summary":
                    continue  # trim-only: tier 2/3 history is not re-armed
                self._mark_span(start, end, kind, render_version=version,
                                pin_end=promo.path_end)
                if kind == "summary":
                    self._summaries[(start, end)] = promo.summary_text
                self._replayed_spans.append((start, end))

    def _settle_replay(self, nodes: list[SessionNode]) -> None:
        """Replay the retirement and fold pops of rehydrated decisions.

        A live span promotion drops every decision it contains: a whole-turn
        digest retires that turn's segment digests, and an epoch fold retires
        the digests it folds. Rehydrate marks each persisted span without
        node order, so those contained decisions would linger, and ``stats()``
        would count them as resident. Walking the replay order with the path
        in hand removes each decision a later one contains, which leaves the
        state the live process had. The rendered view does not change,
        because the renderer already skips anything inside a promoted span.
        """
        if not self._replayed_spans:
            return
        index = {n.node_id: i for i, n in enumerate(nodes)}
        order = [
            key for key in dict.fromkeys(self._replayed_spans)
            if key in self._digested and key[0] in index and key[1] in index
        ]
        self._replayed_spans = []
        for pos, (start, end) in enumerate(order):
            lo, hi = index[start], index[end]
            for older in order[:pos]:
                if older not in self._digested:
                    continue
                if lo <= index[older[0]] and index[older[1]] <= hi:
                    self._retire(older)

    def _retire(self, key: tuple[str, str]) -> None:
        """Drop one promoted span, leaving any newer span with its start."""
        start, end = key
        self._digested.pop(key, None)
        # Keep superseded checkpoint text for forks ending before its successor.
        # Only _digested counts residency; rendering chooses the widest on-path checkpoint.
        self._span_versions.pop(key, None)
        self._span_pin_ends.pop(key, None)
        if self._digest_by_start.get(start) == end:
            self._digest_by_start.pop(start)
        if self._epoch_by_start.get(start) == end:
            self._epoch_by_start.pop(start)

    # ── Internals ────────────────────────────────────────────────────────

    def _tier_snapshot(self) -> Callable[[], None]:
        """Capture the whole sticky ladder state; the returned call puts it back.

        Rungs mutate state as they run, and a rung that turns out to have
        reclaimed nothing has to be undone rather than reported — an
        escalation that frees no tokens still breaks the cache prefix and
        still records a promotion the next resume would replay.
        """
        digested = dict(self._digested)
        epochs = dict(self._epoch_by_start)
        digests = dict(self._digest_by_start)
        versions = dict(self._span_versions)
        pin_ends = dict(self._span_pin_ends)
        summaries = dict(self._summaries)
        trimmed = set(self.trimmer._trimmed)
        consumed = set(self.trimmer._image_consumed)

        def restore() -> None:
            self._digested.clear()
            self._digested.update(digested)
            self._epoch_by_start.clear()
            self._epoch_by_start.update(epochs)
            self._digest_by_start.clear()
            self._digest_by_start.update(digests)
            self._span_versions.clear()
            self._span_versions.update(versions)
            self._span_pin_ends.clear()
            self._span_pin_ends.update(pin_ends)
            self._summaries.clear()
            self._summaries.update(summaries)
            self.trimmer._trimmed.clear()
            self.trimmer._trimmed.update(trimmed)
            self.trimmer._image_consumed.clear()
            self.trimmer._image_consumed.update(consumed)

        return restore

    def _mark_span(
        self, start: str, end: str, kind: str, *, render_version: int | None = None,
        pin_end: str | None = None,
    ) -> None:
        self._digested[(start, end)] = kind
        if pin_end is None:
            self._span_pin_ends.pop((start, end), None)
        else:
            self._span_pin_ends[(start, end)] = pin_end
        self._span_versions[(start, end)] = (
            self.config.render_version if render_version is None else render_version
        )
        if kind == EPOCH_STUB:
            self._epoch_by_start[start] = end
        elif kind == "summary":
            pass  # rendered from the persisted text, never a mechanical digest
        else:
            self._digest_by_start[start] = end

    def _eligible_spans(
        self, nodes: list[SessionNode], *, keep_recent: int | None = None
    ) -> list[list[SessionNode]]:
        """Old spans not yet promoted, oldest first. ``keep_recent`` overrides
        ``keep_recent_turns`` for one call (0 makes the in-flight turn
        eligible; overflow recovery's last rung).

        The newest ``keep_recent_turns`` turns are never digested whole.
        With wake-aware counting the newest real user turns are protected
        too, so a run of wake turns cannot age them out. Inside a protected
        turn, completed segments are eligible: all but the newest three
        under the legacy guard, or every one that ends before the recency
        tail when a tail is set. The tail also keeps any whole span that
        reaches into it.
        """
        spans = split_spans(nodes)
        keep = self.config.keep_recent_turns if keep_recent is None else keep_recent
        protected = self._protected_spans(spans, keep, wake_aware=keep_recent is None)
        tail = self._tail_from if keep_recent is None else None
        index = {n.node_id: i for i, n in enumerate(nodes)}
        old: list[list[SessionNode]] = []
        for i, span in enumerate(spans):
            if i not in protected:
                old.append(span)
            elif self.config.recall_tools:
                segments = completed_segments(
                    span, error_text_cap=self.config.error_text_cap,
                )
                # Legacy: protect three completed segments plus the
                # still-open tail.
                old.extend(segments if tail is not None else segments[:-3])
        if tail is not None:
            old = [s for s in old if index[s[-1].node_id] < tail]
        covered = self._covered_ids(nodes)
        return [s for s in old if s[0].node_id not in covered]

    def _protected_spans(
        self, spans: list[list[SessionNode]], keep: int, *, wake_aware: bool,
    ) -> set[int]:
        """Indexes of the spans that are never digested whole: the newest
        ``keep``, and with wake-aware counting also the newest ``keep - 1``
        real user turns before the in-flight one. Turns started by a wake
        never count toward that second set."""
        if not keep:
            return set()
        protected = set(range(max(0, len(spans) - keep), len(spans)))
        if wake_aware and self.config.wake_aware_turns and self.wake_turn_starts:
            real = 1  # the in-flight turn always counts
            for i in range(len(spans) - 2, -1, -1):
                if real >= keep:
                    break
                if spans[i][0].node_id in self.wake_turn_starts:
                    continue
                protected.add(i)
                real += 1
        return protected

    def _covered_ids(self, nodes: list[SessionNode]) -> set[str]:
        """Node ids inside a promotion that is live on this path."""
        covered: set[str] = set()
        index = {n.node_id: i for i, n in enumerate(nodes)}
        for (start, end), _kind in self._digested.items():
            if start in index and end in index:
                for k in range(index[start], index[end] + 1):
                    covered.add(nodes[k].node_id)
        return covered

    def _newest_calls_start(self, nodes: list[SessionNode]) -> int:
        """Index of the oldest of the newest ``recency_tail_calls`` model
        calls (assistant nodes), so those calls and their results stay
        verbatim. A path with fewer calls is protected from its start."""
        calls = self.config.recency_tail_calls
        if calls <= 0:
            return len(nodes)
        seen = 0
        for i in range(len(nodes) - 1, -1, -1):
            if nodes[i].message.role == "assistant":
                seen += 1
                if seen >= calls:
                    return i
        return 0

    def _recency_tail_start(
        self, nodes: list[SessionNode], context_window: int | None, scale: float,
    ) -> int | None:
        """Index where the token-budgeted recency tail starts, or None for the
        legacy count-based guards (trim-only, no known window, or a tail of
        0).

        The tail holds the newest min(recency_tail_tokens,
        recency_tail_window_ratio * window) measured tokens, priced in
        chars/4 units through ``scale`` on the raw nodes so it does not move
        while a batch rewrites the view, and never fewer than the newest
        ``recency_tail_calls`` calls. The in-flight turn-start node needs no
        entry here: tier 1 never trims it, completed segments skip it, and
        the in-flight span is always among the protected spans.
        """
        cfg = self.config
        if not cfg.enabled or context_window is None or cfg.recency_tail_tokens <= 0:
            return None
        tokens = cfg.recency_tail_tokens
        if cfg.recency_tail_window_ratio > 0:
            tokens = min(tokens, int(context_window * cfg.recency_tail_window_ratio))
        budget = _in_estimate_units(tokens, scale) * 4
        start = len(nodes)
        used = 0
        for i in range(len(nodes) - 1, -1, -1):
            used += sum(block_chars(b) for b in nodes[i].message.content)
            if used > budget:
                break
            start = i
        return min(start, self._newest_calls_start(nodes))

    def _inflight_estimate(self, nodes: list[SessionNode]) -> int:
        """Estimated tokens the in-flight turn carries under the current
        view (its span from the newest turn-start node)."""
        if not nodes:
            return 0
        start = len(nodes) - len(split_spans(nodes)[-1])
        return estimate_tokens_of_messages(self._render(nodes[start:]))

    def _digest_until(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int,
        target: int,
        keep_recent: int | None = None,
        eligible: list[list[SessionNode]] | None = None,
    ) -> list[tuple[str, str]]:
        """Promote eligible spans oldest-first until the estimate reaches
        target (target=0 → promote everything eligible). Incremental
        accounting avoids a full re-render per span. ``eligible`` replaces
        the usual candidate list (the in-flight rung passes its own)."""
        if eligible is None:
            eligible = self._eligible_spans(nodes, keep_recent=keep_recent)
        if not eligible:
            return []
        current = overhead_tokens + estimate_tokens_of_messages(
            self._render(nodes)
        )
        promoted: list[tuple[str, str]] = []
        for span in eligible:
            if target and current <= target:
                break
            key = (span[0].node_id, span[-1].node_id)
            restore = self._tier_snapshot()
            # A complete old turn can supersede its earlier within-turn
            # segments. Retire those contained decisions before promoting it.
            span_ids = {n.node_id for n in span}
            for start, end in list(self._digested):
                if start in span_ids and end in span_ids:
                    self._retire((start, end))
            self._mark_span(*key, SPAN_DIGEST, pin_end=nodes[-1].node_id)
            after = self._estimate(nodes, overhead_tokens)
            if after >= current:
                restore()
                continue
            promoted.append(key)
            current = after
        return promoted

    def _fold_epochs(
        self, nodes: list[SessionNode], *, force: bool = False
    ) -> list[tuple[str, str]]:
        """Fold the oldest run(s) of resident span digests into epoch stubs
        whenever more than max_resident_span_digests are resident (force →
        fold everything foldable regardless of the residency cap).

        A run never crosses a live gap. When a gap cuts off the oldest
        digests, the fold slides past them to the next contiguous run instead
        of stopping for good; the digests before the gap stay resident.
        """
        self._settle_replay(nodes)
        index = {n.node_id: i for i, n in enumerate(nodes)}
        resident = sorted(
            (
                (index[start], start, end)
                for (start, end), kind in self._digested.items()
                if kind == SPAN_DIGEST and start in index and end in index
                and not any(
                    other_start in index and other_end in index
                    and (other_start, other_end) != (start, end)
                    and index[other_start] <= index[start] <= index[end] <= index[other_end]
                    for other_start, other_end in self._digested
                )
            ),
        )
        folded: list[tuple[str, str]] = []
        fold_n = self.config.epoch_fold_spans
        count = len(resident)  # digests still resident, skipped ones included
        while (
            count > (0 if force else self.config.max_resident_span_digests)
            and len(resident) >= (1 if force else fold_n)
        ):
            chunk = resident[:fold_n]
            gap = next(
                (k for k, (a, b) in enumerate(zip(chunk, chunk[1:]))
                 if index[a[2]] + 1 != b[0]),
                None,
            )
            if gap is not None:
                # A live gap must never be swallowed by an epoch range. Leave
                # the digests before it resident and try the run after it.
                resident = resident[gap + 1:]
                continue
            resident = resident[fold_n:]
            count -= len(chunk)
            start = chunk[0][1]
            end = chunk[-1][2]
            for _pos, s, e in chunk:  # spans leave the digest tier
                self._digested.pop((s, e), None)
                self._digest_by_start.pop(s, None)
            self._mark_span(start, end, EPOCH_STUB, pin_end=nodes[-1].node_id)
            folded.append((start, end))
            if force and not resident:
                break
        return folded

    # ── Stats (for the context-usage breakdown) ──────────────────────────

    def stats(self, nodes: list[SessionNode]) -> dict[str, int]:
        self._settle_replay(nodes)
        index = {n.node_id: i for i, n in enumerate(nodes)}
        digests = sum(
            1
            for (s, e), k in self._digested.items()
            if k == SPAN_DIGEST and s in index and e in index
        )
        epochs = sum(
            1
            for (s, e), k in self._digested.items()
            if k == EPOCH_STUB and s in index and e in index
        )
        raw = sum(block_chars(b) for n in nodes for b in n.message.content)
        rendered = sum(
            block_chars(b) for m in self._render(nodes) for b in m.content
        )
        return {
            "digestedSpans": digests,
            "epochStubs": epochs,
            "trimmedToolResults": len(self.trimmer._trimmed),
            "consumedImages": len(self.trimmer._image_consumed),
            "reclaimedTokens": max(0, (raw - rendered) // 4),
        }
