"""Hierarchical, recoverable compaction — the mechanical eviction ladder.

Tier 1 (:mod:`fluid.context.trim`) replaces old tool results with placeholders.
Tier 2 collapses whole old turns ("spans") into extractive digests. Tier 3
folds runs of old digests into one-line epoch stubs. Every evicted thing
carries a node ref, and the ``HistoryExpand`` / ``HistorySearch`` builtins
re-hydrate originals from the session store — loss is recoverable, never
permanent.

Invariants (shared with tier 1, enforced here):

* **Mechanical only.** No model writes any digest. ``render_span_digest`` and
  ``render_epoch_stub`` are pure functions of (nodes, config): extractive,
  deterministic, byte-identical on every render — which is what makes tier
  state cheap to persist (decisions only; text is re-derived).
* **Batch + sticky.** Promotions happen in rare amortized batches and are
  sticky for the engine's lifetime; between batches the rendered prefix is
  byte-identical, so provider cache hits resume after the one-time break.
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

from fluid.context.trim import (
    ToolResultTrimmer,
    TrimConfig,
    block_chars,
    estimate_tokens_of_messages,
)
from fluid.messages import (
    Message,
    Text,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.store.base import SessionNode

SPAN_DIGEST = "digest"
EPOCH_STUB = "epoch"

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


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
    # Whether the session can call HistoryExpand and HistorySearch. Off, the
    # digests and stubs stop naming a recovery route the model cannot take.
    recall_tools: bool = True
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
    # Digest extraction caps.
    user_text_cap: int = 600
    assistant_text_cap: int = 400
    tool_line_cap: int = 120
    error_text_cap: int = 2_000


# Absolute floor for a derived trigger. Below this the ladder would be
# re-rendering on every call; no model this harness targets has a window
# under 2k tokens, so the floor is never the binding one in practice.
MIN_TRIGGER_TOKENS = 1_000


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
    32k for output), and the result never drops below half the window: with
    the defaults a 32k window used to yield -16,000 and an 8k window -39,808,
    so the ladder fired on every render and ``_digest_until`` never reached
    its target. The floor only rescues a collapsed ceiling; it never raises a
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
    """What one batch rung did — the persistable decision, never the text."""

    tier: int  # 1 | 2 | 3
    reason: str = "trigger"  # "trigger" | "overflow" | "image_consumed"
    trimmed: tuple[tuple[str, str], ...] = ()  # (node_id, tool_use_id)
    spans: tuple[tuple[str, str, str], ...] = ()  # (start, end, kind)
    reclaimed_chars: int = 0


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


def strip_system_reminders(text: str) -> str:
    """Drop <system-reminder> blocks from digested user text: they are harness
    plumbing (deferred-tool catalogs, ledger recitations) that is re-derivable
    and re-injected on fresh turns — history copies are pure weight."""
    return _REMINDER_RE.sub("", text).strip()


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


def _result_size(block: ToolResultBlock) -> int:
    return block_chars(block)


def render_span_digest(
    span: list[SessionNode], config: CompactionConfig
) -> Message:
    """Extractive digest of one whole turn, as a single user-role Text message.

    Pure and deterministic: same span + same config → byte-identical output.
    """
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
    for node in span:
        message = node.message
        if message.role == "user":
            results = [
                b for b in message.content if isinstance(b, ToolResultBlock)
            ]
            for block in results:
                size = _result_size(block)
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
                    lines.append(
                        f"  ✓ result {size} chars ({node.node_id})"
                    )
            user_text = strip_system_reminders(
                "".join(
                    b.text for b in message.content if isinstance(b, Text)
                )
            )
            if user_text:
                lines.append(
                    f"user ({node.node_id}): {_clip(user_text, config.user_text_cap)}"
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
        lines.insert(1, f"tools: {summary}")
    return Message(role="user", content=[Text("\n".join(lines))])


def render_epoch_stub(
    spans: list[list[SessionNode]], config: CompactionConfig
) -> Message:
    """One-line stub replacing a run of digested spans. Deterministic."""
    start = spans[0][0].node_id
    end = spans[-1][-1].node_id
    first_user = ""
    for node in spans[0]:
        if is_turn_start(node):
            first_user = strip_system_reminders(
                "".join(
                    b.text for b in node.message.content if isinstance(b, Text)
                )
            )
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
    return Message(role="user", content=[Text(text)])


def _intake_stub(node_id: str, text: str, config: CompactionConfig) -> Text:
    preview = text[: config.intake_preview_chars]
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


@dataclass
class Compactor:
    """Owns the eviction ladder state and renders compacted history views."""

    config: CompactionConfig = field(default_factory=CompactionConfig)
    trimmer: ToolResultTrimmer = None  # type: ignore[assignment]
    # Sticky promotion state: span key (start, end) -> SPAN_DIGEST|EPOCH_STUB,
    # and epoch ranges by their start node for O(1) lookup while walking.
    _digested: dict = field(default_factory=dict)
    _epoch_by_start: dict = field(default_factory=dict)
    _digest_by_start: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trimmer is None:
            self.trimmer = ToolResultTrimmer(config=self.config.trim)

    # ── Mode ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """True when this compactor rewrites the view at all."""
        return self.config.enabled or self.config.trim.enabled

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
        # Consume-once image trims need the FULL path: a later assistant node
        # is the evidence an image was already delivered, and the per-node
        # trimmer calls below can never see one. Recomputed every render
        # (a deterministic view, like trimmer.view); apply() persists the pairs.
        trimmed = self.trimmer._trimmed
        if self.trimmer.config.enabled:
            consumed = self.trimmer._consumed_image_results(nodes)
            if consumed:
                trimmed = trimmed | consumed
        out: list[Message] = []
        i = 0
        while i < len(nodes):
            node = nodes[i]
            if not self.config.enabled:
                # Trim-only: tier 2/3 decisions are never made here, and a
                # persisted one rehydrated from a session that *did* run the
                # ladder stays inert rather than digesting history behind the
                # host app's back.
                out.append(self._render_live(node, trimmed))
                i += 1
                continue
            end_id = self._epoch_by_start.get(node.node_id)
            if end_id is not None and end_id in index and index[end_id] >= i:
                j = index[end_id]
                out.append(
                    render_epoch_stub(
                        split_spans(nodes[i : j + 1]), self.config
                    )
                )
                i = j + 1
                continue
            end_id = self._digest_by_start.get(node.node_id)
            if end_id is not None and end_id in index and index[end_id] >= i:
                j = index[end_id]
                out.append(
                    render_span_digest(nodes[i : j + 1], self.config)
                )
                i = j + 1
                continue
            out.append(self._render_live(node, trimmed))
            i += 1
        return out

    def _render_live(
        self, node: SessionNode, trimmed: set[tuple[str, str]]
    ) -> Message:
        message = (
            self.trimmer._render([node], trimmed)[0]
            if self.trimmer.config.enabled
            else node.message
        )
        if message.role != "user":
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
                        ],
                        is_error=block.is_error,
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
    ) -> tuple[list[Message], list[Promotion]]:
        """Render history; when the estimated total crosses the threshold,
        run the ladder as one amortized batch and return what it did."""
        if not self.config.enabled:
            if not self.config.trim.enabled:
                return [n.message for n in nodes], []
            return self._apply_trim_only(
                nodes,
                overhead_tokens=overhead_tokens,
                context_window=context_window,
                output_reserve_tokens=output_reserve_tokens,
            )
        # Persist consume-once image trims into the sticky set (mirrors
        # trimmer.apply, which the Compactor path never calls). The decision
        # is deterministic from the path, so this is about keeping the
        # rendered prefix byte-identical between requests, not durability.
        promotions: list[Promotion] = self._consume_images(nodes)
        rendered = self._render(nodes)
        threshold = self._threshold(
            self.config.trigger_tokens, context_window, output_reserve_tokens
        )
        total = overhead_tokens + estimate_tokens_of_messages(rendered)
        if total <= threshold:
            return rendered, promotions
        target = int(threshold * self.config.target_ratio)

        # Rung 1: batch-trim tool results.
        pairs = self.trimmer.trim_batch(nodes)
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

    def _consume_images(self, nodes: list[SessionNode]) -> list[Promotion]:
        """Persist consume-once image trims and report them as a promotion.

        An image dropping out of context is a real loss — HistoryExpand cannot
        put it back — and it used to happen silently, so neither the transcript
        nor the user could see it. Reporting it as a tier-1 promotion with its
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

    def _apply_trim_only(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int,
        context_window: int | None,
        output_reserve_tokens: int,
    ) -> tuple[list[Message], list[Promotion]]:
        """Tier 1 on its own: trim tool results, never digest a turn.

        Same batch-and-sticky discipline as the full ladder (rare, amortized,
        cache-safe), driven by the tier-1 trigger rather than the ladder's.
        """
        promotions = self._consume_images(nodes)
        rendered = self._render(nodes)
        threshold = self._threshold(
            self.config.trim.trigger_tokens,
            context_window,
            output_reserve_tokens,
            window_ratio=self.config.trim.trigger_window_ratio,
        )
        if overhead_tokens + estimate_tokens_of_messages(rendered) <= threshold:
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
        """
        if not self.active:
            return []
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
        digest_target = target or 0

        def trim(keep: int | None) -> Callable[[], Promotion | None]:
            def rung() -> Promotion | None:
                pairs = self.trimmer.trim_batch(nodes, keep_recent=keep)
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

        rungs: list[Callable[[], Promotion | None]] = (
            [trim(None), trim(0)]
            if trim_only
            else [trim(None), digest(None), fold, trim(0), digest(0)]
        )
        promotions: list[Promotion] = []
        for rung in rungs:
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
            promotions.append(replace(promo, reclaimed_chars=reclaimed))
            if target is None or after <= target:
                break
        return promotions

    def _estimate(self, nodes: list[SessionNode], overhead_tokens: int) -> int:
        """Estimated tokens the next request carries under the current view."""
        return overhead_tokens + estimate_tokens_of_messages(self._render(nodes))

    def rehydrate(self, promotions: Iterable[Promotion]) -> None:
        """Re-apply persisted decisions (resume). Rendering is deterministic,
        so the rehydrated view is byte-identical to the pre-restart one."""
        for promo in promotions:
            self.trimmer.mark_trimmed(promo.trimmed)
            if not self.config.enabled:
                continue  # trim-only: tier 2/3 history is not re-armed
            for start, end, kind in promo.spans:
                self._mark_span(start, end, kind)

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
        trimmed = set(self.trimmer._trimmed)

        def restore() -> None:
            self._digested.clear()
            self._digested.update(digested)
            self._epoch_by_start.clear()
            self._epoch_by_start.update(epochs)
            self._digest_by_start.clear()
            self._digest_by_start.update(digests)
            self.trimmer._trimmed.clear()
            self.trimmer._trimmed.update(trimmed)

        return restore

    def _mark_span(self, start: str, end: str, kind: str) -> None:
        self._digested[(start, end)] = kind
        if kind == EPOCH_STUB:
            self._epoch_by_start[start] = end
        else:
            self._digest_by_start[start] = end

    def _eligible_spans(
        self, nodes: list[SessionNode], *, keep_recent: int | None = None
    ) -> list[list[SessionNode]]:
        """Old spans not yet promoted, oldest first. ``keep_recent`` overrides
        ``keep_recent_turns`` for one call (0 makes the in-flight turn
        eligible; overflow recovery's last rung)."""
        spans = split_spans(nodes)
        keep = self.config.keep_recent_turns if keep_recent is None else keep_recent
        old = spans[:-keep] if keep else spans
        covered: set[str] = set()  # node ids inside existing promotions
        index = {n.node_id: i for i, n in enumerate(nodes)}
        for (start, end), _kind in self._digested.items():
            if start in index and end in index:
                for k in range(index[start], index[end] + 1):
                    covered.add(nodes[k].node_id)
        return [s for s in old if s[0].node_id not in covered]

    def _digest_until(
        self,
        nodes: list[SessionNode],
        *,
        overhead_tokens: int,
        target: int,
        keep_recent: int | None = None,
    ) -> list[tuple[str, str]]:
        """Promote eligible spans oldest-first until the estimate reaches
        target (target=0 → promote everything eligible). Incremental
        accounting avoids a full re-render per span."""
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
            span_chars = sum(
                block_chars(b)
                for m in self.trimmer.view(span)
                for b in m.content
            )
            digest_chars = sum(
                block_chars(b)
                for b in render_span_digest(span, self.config).content
            )
            key = (span[0].node_id, span[-1].node_id)
            self._mark_span(*key, SPAN_DIGEST)
            promoted.append(key)
            current -= max(0, (span_chars - digest_chars) // 4)
        return promoted

    def _fold_epochs(
        self, nodes: list[SessionNode], *, force: bool = False
    ) -> list[tuple[str, str]]:
        """Fold the oldest run(s) of resident span digests into epoch stubs
        whenever more than max_resident_span_digests are resident (force →
        fold everything foldable regardless of the residency cap)."""
        index = {n.node_id: i for i, n in enumerate(nodes)}
        resident = sorted(
            (
                (index[start], start, end)
                for (start, end), kind in self._digested.items()
                if kind == SPAN_DIGEST and start in index and end in index
            ),
        )
        folded: list[tuple[str, str]] = []
        fold_n = self.config.epoch_fold_spans
        while (
            len(resident) > (0 if force else self.config.max_resident_span_digests)
            and len(resident) >= (1 if force else fold_n)
        ):
            chunk = resident[:fold_n]
            resident = resident[fold_n:]
            start = chunk[0][1]
            end = chunk[-1][2]
            for _pos, s, e in chunk:  # spans leave the digest tier
                self._digested.pop((s, e), None)
                self._digest_by_start.pop(s, None)
            self._mark_span(start, end, EPOCH_STUB)
            folded.append((start, end))
            if force and not resident:
                break
        return folded

    # ── Stats (for the context-usage breakdown) ──────────────────────────

    def stats(self, nodes: list[SessionNode]) -> dict[str, int]:
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
            "reclaimedTokens": max(0, (raw - rendered) // 4),
        }
