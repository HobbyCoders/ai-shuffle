"""Cache-safe tool-result trimming (microcompaction) — prevention over lossy
compaction.

Old, bulky tool results are replaced with a short placeholder once the
estimated context size crosses a trigger. Design rules (plan §7):

* **Batch + sticky = cache-safe.** Trimming mid-history mutates the prefix and
  invalidates the provider cache, so it must be RARE and AMORTIZED: when the
  trigger fires, every eligible result is trimmed at once, and each trimmed
  block stays trimmed for the engine's lifetime (the ``_trimmed`` set). Between
  batch events the rendered prefix is byte-identical turn over turn, so cache
  hits resume immediately after the one-time break.
* **Keep recent, keep errors.** The newest K tool results are never trimmed
  (the model is often still acting on them), and error results are always kept
  verbatim — losing an error message is how doom loops start.
* **Images play by the same rules.** A delivered image is consume-once, but the
  consumption is a batched decision like any other: it needs pressure and a
  turn boundary, and it swaps only the Image blocks in place so the sibling
  text — and every request byte before the result — is untouched. Consuming an
  image between two calls of the same turn rewrote a message the provider had
  already cached and re-billed the whole prompt at full price.
* **The tree is never mutated.** Trimming is a view transformation applied when
  history is rendered for a request; the stored SessionNodes keep full fidelity
  (rewind/fork/audit see everything).

This module is tier 1 of the eviction ladder; tiers 2 and 3 (span digests and
epoch stubs) live in :mod:`fluid.context.compaction`, which owns a trimmer and
drives it through :meth:`ToolResultTrimmer.trim_batch`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from fluid.messages import Image, Message, Text, ToolResultBlock
from fluid.store.base import SessionNode


@dataclass(kw_only=True)
class TrimConfig:
    enabled: bool = True
    # Whether the session can call HistoryExpand and HistorySearch. Off, the
    # placeholders stop naming a recovery route the model cannot take and
    # say instead that the content is gone from context.
    recall_tools: bool = True
    # Estimated-token threshold that arms a batch trim (≈ chars / 4). This is
    # the FLOOR and the fallback: when the model's context window is known, the
    # effective trigger is max(trigger_tokens, window * trigger_window_ratio),
    # still capped to leave the compaction headroom free. Trimming a 1M-window
    # model at a fixed 120k threw away recoverable results — and cache-busted
    # the prefix — at 12% window usage.
    trigger_tokens: int = 120_000
    # Fraction of a known context window the history may reach before a batch
    # trim arms. 0.0 disables scaling (old fixed-trigger behavior).
    trigger_window_ratio: float = 0.6
    # The newest K tool results are never trimmed.
    keep_recent_tool_results: int = 5
    # Results shorter than this are not worth a placeholder.
    min_result_chars: int = 2_000


# Flat per-image weight, in chars (≈1500 tokens). Providers bill images by
# pixels (roughly width x height / 750), not bytes, and tool images are
# conditioned to a ~1568px long edge before they enter history (see
# fluid.tools.images), so the real charge is ~1.5-2k tokens per image. A
# flat weight keeps the estimate sane and makes image-bearing blocks eligible
# for trimming; pricing the base64 by chars/4 once made a 531k PNG look like
# 130k tokens.
IMAGE_CHARS = 6_000


def _reasoning_chars(block: Any) -> int:
    """Chars a Reasoning envelope puts on the wire.

    ``raw`` is resent verbatim when present and already contains the summary
    (OpenAI) or the thinking text plus signature (Anthropic), so it is the
    whole cost; without it the parts are sent individually. Only one of
    ``raw`` / ``encrypted_content`` goes out, never both.
    """
    raw = getattr(block, "raw", None)
    raw_len = 0
    if isinstance(raw, dict) and raw:
        try:
            raw_len = len(json.dumps(raw, default=str))
        except (TypeError, ValueError):
            raw_len = 0
    parts = 0
    for attr in ("summary_text", "signature", "encrypted_content"):
        value = getattr(block, attr, None)
        if isinstance(value, str):
            parts += len(value)
    return max(raw_len, parts)


def block_chars(block: Any) -> int:
    total = 0
    text = getattr(block, "text", None)
    if isinstance(text, str):
        total += len(text)
    # A top-level Image (a user attachment) is resent in full on every call
    # until consumed, same as one nested in a tool result.
    if isinstance(getattr(block, "data", None), str):
        total += IMAGE_CHARS
    # Reasoning envelopes carry no ``text`` but are resent verbatim every
    # call; a 60 KB encrypted blob that counts as nothing lets a
    # reasoning-heavy session grow past the trigger unseen.
    if hasattr(block, "encrypted_content") or hasattr(block, "summary_text"):
        total += _reasoning_chars(block)
    # tool_use inputs count too — Write/Edit calls carry whole files in their
    # args, and ignoring them lets a write-heavy session blow the window while
    # the estimate stays "under trigger".
    input_ = getattr(block, "input", None)
    if isinstance(input_, dict) and input_:
        try:
            total += len(json.dumps(input_, default=str))
        except (TypeError, ValueError):
            pass
    for inner in getattr(block, "content", None) or []:
        inner_text = getattr(inner, "text", None)
        if isinstance(inner_text, str):
            total += len(inner_text)
        elif isinstance(getattr(inner, "data", None), str):
            total += IMAGE_CHARS
    return total


# Back-compat alias (tests and older callers import the underscored name).
_block_chars = block_chars


def estimate_tokens(nodes: list[SessionNode]) -> int:
    """Cheap chars/4 estimate over every text carried by the path."""
    chars = sum(
        block_chars(b) for n in nodes for b in n.message.content
    )
    return chars // 4


def estimate_tokens_of_text(text: str | None) -> int:
    """The same chars/4 heuristic, for plain strings (prompt sections, tool
    schemas). Every token count fluid reports comes from this one estimator —
    there is no tokenizer and no counting round-trip to a provider."""
    return len(text) // 4 if text else 0


HEAD_EXCERPT_CHARS = 160


def block_head(block: Any, limit: int = HEAD_EXCERPT_CHARS) -> str:
    """The first line or so of a tool result's text, whitespace-collapsed."""
    parts: list[str] = []
    for item in getattr(block, "content", None) or ():
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    head = " ".join(" ".join(parts).split())
    return head[:limit] + ("…" if len(head) > limit else "")


def _placeholder(node_id: str, chars: int, head: str = "", *, recall: bool = True) -> Text:
    excerpt = f' It began: "{head}"' if head else ""
    route = (
        f'recover it verbatim with HistoryExpand("{node_id}") when any detail matters'
        if recall
        else "re-run the tool when any detail matters"
    )
    return Text(
        f"[old tool result trimmed to save context — {chars} chars removed.{excerpt} "
        f"The rest is NOT in context: do not reconstruct it from memory; {route}]"
    )


# ``[image: <path>, image/png, 1234 bytes]`` — the caption Read puts beside an
# image it attaches (fluid.tools.builtins.read). Screenshots from other tools
# may carry no path at all, which the placeholder has to say rather than
# inventing one.
_IMAGE_CAPTION_RE = re.compile(r"\[image:\s*([^,\]]+?)\s*,")


def image_source_path(head: str) -> str:
    """The file path named in an image result's caption, or "" if none."""
    match = _IMAGE_CAPTION_RE.search(head or "")
    return match.group(1).strip() if match else ""


def _image_placeholder(
    node_id: str,
    chars: int,
    head: str = "",
    *,
    recall: bool = True,
    text_retained: bool = False,
) -> Text:
    """What replaces an image once the model has already seen it.

    The generic placeholder pointed at HistoryExpand, which recovers TEXT — it
    cannot put an image back in context. In the audited transcript the model
    read that, concluded recall was impossible, and designed a UI from memory
    of a screenshot it had seen once. So this says what actually happened, says
    plainly that HistoryExpand is not the recovery route, and names the one
    that is: re-Read the file.

    ``text_retained`` is the in-place form: only the image block was swapped
    and every sibling Text in the result is still verbatim in context, so the
    "recover the text with HistoryExpand" clause would be false.
    """
    path = image_source_path(head)
    recovery = (
        f'Re-Read the file at "{path}" to see it again'
        if path
        else "Re-run the tool that produced it to see it again"
    )
    if not recall:
        return Text(
            f"[image consumed from context — {chars} chars removed. You "
            f"already saw this image; it is NOT in context now. {recovery}. "
            f"Do not describe its contents from memory — look again if any "
            f"detail matters]"
        )
    if text_retained:
        return Text(
            f"[image consumed from context — {chars} chars removed. You "
            f"already saw this image; it is NOT in context now, and "
            f"HistoryExpand cannot restore it (it recovers text only). "
            f"{recovery}. Do not describe its contents from memory — look "
            f"again if any detail matters. The text of this result is "
            f"unchanged; only the image was removed]"
        )
    return Text(
        f"[image consumed from context — {chars} chars removed. You already "
        f"saw this image; it is NOT in context now, and HistoryExpand cannot "
        f"restore it (it recovers text only). {recovery}. Do not describe its "
        f"contents from memory — look again if any detail matters. The text "
        f'beside it is still recoverable with HistoryExpand("{node_id}")]'
    )


# The ``tool_use_id`` slot of a sticky trim key when the trimmed thing is a
# user-attached image rather than a tool result. No provider issues an id in
# this shape, so it cannot collide with a real call.
USER_IMAGE_KEY = "<user-image>"


def _user_image_placeholder(node_id: str, chars: int, *, recall: bool = True) -> Text:
    """What replaces a user-attached image once the model has seen it.

    There is no file to re-Read and no tool to re-run: the only way back is
    the user attaching it again, which the placeholder says outright.
    """
    if not recall:
        return Text(
            f"[image attachment consumed from context — {chars} chars "
            f"removed. You already saw this image; it is NOT in context now. "
            f"Ask the user to attach it again if any detail matters; do not "
            f"describe its contents from memory]"
        )
    return Text(
        f"[image attachment consumed from context — {chars} chars removed. "
        f"You already saw this image; it is NOT in context now, and "
        f"HistoryExpand cannot restore it (it recovers text only). Ask the "
        f"user to attach it again if any detail matters; do not describe its "
        f"contents from memory. The text beside it is still recoverable with "
        f'HistoryExpand("{node_id}")]'
    )


@dataclass
class ToolResultTrimmer:
    """Holds the sticky trimmed-set and renders trimmed history views."""

    config: TrimConfig = field(default_factory=TrimConfig)
    # (node_id, tool_use_id) blocks already trimmed — stays for the engine's
    # lifetime so the rendered prefix is stable between batch trims.
    _trimmed: set = field(default_factory=set)
    # (node_id, tool_use_id) blocks whose IMAGE payloads have been consumed.
    # Deliberately a SEPARATE set from ``_trimmed``: the two rewrites differ in
    # blast radius. A trim replaces the whole result with one placeholder; an
    # image consumption swaps only the Image blocks and leaves every sibling
    # Text verbatim, so the 57-line test summary that shared a ToolResultBlock
    # with a PNG stops leaving context with it.
    _image_consumed: set = field(default_factory=set)

    @staticmethod
    def _has_image(block: ToolResultBlock) -> bool:
        return any(isinstance(inner, Image) for inner in block.content or [])

    def _consumed_image_results(
        self, nodes: list[SessionNode]
    ) -> set[tuple[str, str]]:
        """Image results already consumed by a later assistant response.

        A screenshot must reach the model once, but retaining its base64 in the
        durable tree must not resend it for every subsequent tool iteration or
        user turn. A later assistant node proves the image was available to a
        completed model call, so future request views replace it with the same
        sticky placeholder used for ordinary microcompaction.
        """
        out: set[tuple[str, str]] = set()
        later_assistant = False
        for node in reversed(nodes):
            if node.message.role == "assistant":
                later_assistant = True
                continue
            if node.message.role != "user" or not later_assistant:
                continue
            for block in node.message.content:
                if (
                    isinstance(block, ToolResultBlock)
                    and not block.is_error
                    and self._has_image(block)
                ):
                    out.add((node.node_id, block.tool_use_id))
                elif isinstance(block, Image):
                    # A user attachment: same consume-once rule, keyed by the
                    # node alone (every image in the message goes together).
                    out.add((node.node_id, USER_IMAGE_KEY))
        return out

    def _images_swapped(
        self, node_id: str, block: ToolResultBlock
    ) -> ToolResultBlock:
        """``block`` with each Image replaced, IN PLACE, by a placeholder.

        Every sibling block keeps its position and its bytes. That is what
        makes the rewrite local: only the request bytes of the images move, so
        a provider prefix that ends before this result stays byte-identical.
        """
        head = block_head(block)
        content: list[Any] = []
        for inner in block.content or ():
            if isinstance(inner, Image):
                content.append(
                    _image_placeholder(
                        node_id, block_chars(inner), head,
                        recall=self.config.recall_tools,
                        text_retained=True,
                    )
                )
            else:
                content.append(inner)
        return ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=content,
            is_error=block.is_error,
        )

    def _render(
        self,
        nodes: list[SessionNode],
        trimmed: set[tuple[str, str]] | None = None,
        consumed: set[tuple[str, str]] | None = None,
    ) -> list[Message]:
        trimmed = self._trimmed if trimmed is None else trimmed
        consumed = self._image_consumed if consumed is None else consumed
        if not trimmed and not consumed:
            return [n.message for n in nodes]
        out: list[Message] = []
        for node in nodes:
            message = node.message
            if message.role != "user":
                out.append(message)
                continue
            blocks: list[Any] = []
            changed = False
            for block in message.content:
                key = (
                    node.node_id,
                    block.tool_use_id if isinstance(block, ToolResultBlock)
                    else USER_IMAGE_KEY,
                )
                if isinstance(block, ToolResultBlock) and key in trimmed:
                    # An image is not recoverable the way text is, so it does
                    # not get text's placeholder.
                    render = (
                        _image_placeholder
                        if self._has_image(block)
                        else _placeholder
                    )
                    blocks.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=[
                                render(
                                    node.node_id, block_chars(block), block_head(block),
                                    recall=self.config.recall_tools,
                                )
                            ],
                            is_error=block.is_error,
                        )
                    )
                    changed = True
                elif (
                    isinstance(block, ToolResultBlock)
                    and key in consumed
                    and self._has_image(block)
                ):
                    blocks.append(self._images_swapped(node.node_id, block))
                    changed = True
                elif isinstance(block, Image) and (
                    key in trimmed or key in consumed
                ):
                    blocks.append(
                        _user_image_placeholder(
                            node.node_id, block_chars(block),
                            recall=self.config.recall_tools,
                        )
                    )
                    changed = True
                else:
                    blocks.append(block)
            out.append(
                Message(role="user", content=blocks, provider_meta=message.provider_meta)
                if changed else message
            )
        return out

    def _eligible(
        self, nodes: list[SessionNode], *, keep_recent: int | None = None
    ) -> list[tuple[str, str]]:
        """(node_id, tool_use_id) of every trimmable result, oldest first.

        ``keep_recent`` overrides the config's recency guard for one call;
        overflow recovery passes 0 because the result that no longer fits is
        usually the newest one.
        """
        found: list[tuple[str, str, int, bool]] = []
        for node in nodes:
            if node.message.role != "user":
                continue
            for block in node.message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                chars = block_chars(block)
                if (node.node_id, block.tool_use_id) in self._image_consumed:
                    # The images are already placeholders; price what is left
                    # so a result that is now just a caption is not trimmed
                    # for weight it no longer carries.
                    chars -= sum(
                        block_chars(inner)
                        for inner in block.content or ()
                        if isinstance(inner, Image)
                    )
                found.append(
                    (node.node_id, block.tool_use_id, chars,
                     bool(block.is_error))
                )
        keep = (
            self.config.keep_recent_tool_results
            if keep_recent is None
            else keep_recent
        )
        candidates = found[:-keep] if keep else found
        return [
            (nid, tuid)
            for nid, tuid, chars, is_error in candidates
            if not is_error
            and chars >= self.config.min_result_chars
            and (nid, tuid) not in self._trimmed
        ]

    def trim_batch(
        self, nodes: list[SessionNode], *, keep_recent: int | None = None
    ) -> list[tuple[str, str]]:
        """Trim every eligible result NOW, ignoring the trigger. Returns the
        newly trimmed ``(node_id, tool_use_id)`` pairs (empty when nothing was
        eligible) so the caller can persist the decision. ``keep_recent``
        overrides the recency guard for this batch only."""
        if not self.config.enabled:
            return []
        newly = self._eligible(nodes, keep_recent=keep_recent)
        self._trimmed.update(newly)
        return newly

    def consume_images(self, nodes: list[SessionNode]) -> list[tuple[str, str]]:
        """Persist consume-once image trims; return the NEWLY consumed pairs.

        Returning them is what makes the trim visible: an image leaving context
        is a real loss of information the model was working from, and it used
        to happen with no event at all — invisible in the transcript, and
        indistinguishable to the model from "I must remember what I saw".

        This is a DECISION, not a view property. Callers gate it on pressure
        and on a turn boundary; rendering never consumes on its own, because a
        consumption between two calls of the same turn rewrites a message the
        provider already cached and re-bills the whole prompt.
        """
        if not self.config.enabled:
            return []
        newly = sorted(
            self._consumed_image_results(nodes)
            - self._image_consumed
            - self._trimmed
        )
        self._image_consumed.update(newly)
        return newly

    def mark_trimmed(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Re-apply previously persisted trim decisions (resume rehydration)."""
        self._trimmed.update(tuple(p) for p in pairs)

    def mark_image_consumed(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Re-apply persisted image-consumption decisions (resume)."""
        self._image_consumed.update(tuple(p) for p in pairs)

    def image_candidates(self, nodes: list[SessionNode]) -> int:
        """How many delivered image results are still carrying their bytes."""
        if not self.config.enabled:
            return 0
        return len(
            self._consumed_image_results(nodes)
            - self._image_consumed
            - self._trimmed
        )

    def force_trim(self, nodes: list[SessionNode]) -> bool:
        """Trim every eligible result NOW, ignoring the trigger (context-
        overflow recovery). Returns True when at least one new block was
        trimmed — i.e. a retry has a smaller prompt to work with."""
        return bool(self.trim_batch(nodes))

    def view(self, nodes: list[SessionNode]) -> list[Message]:
        """The CURRENT trimmed view, read-only.

        Same rendering as :meth:`apply` minus the trigger check, so nothing is
        armed and ``_trimmed`` is left alone: a snapshot caller (the
        context-usage breakdown) sees exactly what the next request would carry
        without changing what that request will be.
        """
        if not self.config.enabled:
            return [n.message for n in nodes]
        # Decisions only. Image consumption used to be recomputed here so the
        # snapshot could predict it, which also meant an image left context
        # the moment a later assistant node existed — regardless of pressure,
        # and mid-turn. Now the next request carries exactly what has been
        # decided, so reporting the decided view IS reporting the request.
        return self._render(nodes)

    def trigger_threshold(self, context_window: int | None = None) -> int:
        """The armed threshold for this trimmer: ``trigger_tokens`` on its own,
        or ``max(trigger_tokens, window * trigger_window_ratio)`` when the host
        knows the model's window. (The Compactor applies its own headroom cap
        on top; a bare trimmer has no headroom budget to respect.)"""
        if context_window is None or self.config.trigger_window_ratio <= 0:
            return self.config.trigger_tokens
        return max(
            self.config.trigger_tokens,
            int(context_window * self.config.trigger_window_ratio),
        )

    def apply(
        self,
        nodes: list[SessionNode],
        *,
        context_window: int | None = None,
        turn_boundary: bool = True,
    ) -> list[Message]:
        """Render history with sticky trims; arm a new batch when over trigger.

        Image consumption obeys the same two gates as every other rung, which
        it used to skip: it needs real pressure (over the trigger) and a turn
        boundary. A bare trimmer has no notion of a turn, so ``turn_boundary``
        defaults to True and the engine passes the real answer.
        """
        if not self.config.enabled:
            return [n.message for n in nodes]
        rendered = self._render(nodes)
        threshold = self.trigger_threshold(context_window)
        if estimate_tokens_of_messages(rendered) <= threshold:
            return rendered
        # Images first: they are the cheapest reclaim per byte and the loss is
        # bounded (the model already saw them), so an image batch often puts
        # the estimate back under the trigger without trimming any text.
        if turn_boundary and self.consume_images(nodes):
            rendered = self._render(nodes)
            if estimate_tokens_of_messages(rendered) <= threshold:
                return rendered
        newly = self._eligible(nodes)
        if newly:
            self._trimmed.update(newly)
            rendered = self._render(nodes)
        return rendered


def estimate_tokens_of_messages(messages: list[Message]) -> int:
    chars = sum(block_chars(b) for m in messages for b in m.content)
    return chars // 4
