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
* **The tree is never mutated.** Trimming is a view transformation applied when
  history is rendered for a request; the stored SessionNodes keep full fidelity
  (rewind/fork/audit see everything).

This module is tier 1 of the eviction ladder; tiers 2 and 3 (span digests and
epoch stubs) live in :mod:`fluid.context.compaction`, which owns a trimmer and
drives it through :meth:`ToolResultTrimmer.trim_batch`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from fluid.messages import Image, Message, Text, ToolResultBlock
from fluid.store.base import SessionNode


@dataclass(kw_only=True)
class TrimConfig:
    enabled: bool = True
    # Estimated-token threshold that arms a batch trim (≈ chars / 4).
    trigger_tokens: int = 120_000
    # The newest K tool results are never trimmed.
    keep_recent_tool_results: int = 5
    # Results shorter than this are not worth a placeholder.
    min_result_chars: int = 2_000


def block_chars(block: Any) -> int:
    total = 0
    text = getattr(block, "text", None)
    if isinstance(text, str):
        total += len(text)
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
        # Image blocks: base64 length is a wild overestimate of provider image
        # tokens, so charge a flat ≈1500-token weight instead. This keeps the
        # estimate sane AND makes image-bearing results eligible for trimming
        # (they'd otherwise sit under min_result_chars forever).
        elif isinstance(getattr(inner, "data", None), str):
            total += 6_000
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


def _placeholder(node_id: str, chars: int) -> Text:
    return Text(
        f"[old tool result trimmed to save context — {chars} chars removed; "
        f'recover verbatim with HistoryExpand("{node_id}") if needed]'
    )


@dataclass
class ToolResultTrimmer:
    """Holds the sticky trimmed-set and renders trimmed history views."""

    config: TrimConfig = field(default_factory=TrimConfig)
    # (node_id, tool_use_id) blocks already trimmed — stays for the engine's
    # lifetime so the rendered prefix is stable between batch trims.
    _trimmed: set = field(default_factory=set)

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
        return out

    def _render(
        self,
        nodes: list[SessionNode],
        trimmed: set[tuple[str, str]] | None = None,
    ) -> list[Message]:
        trimmed = self._trimmed if trimmed is None else trimmed
        if not trimmed:
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
                if (
                    isinstance(block, ToolResultBlock)
                    and (node.node_id, block.tool_use_id) in trimmed
                ):
                    blocks.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=[_placeholder(node.node_id, block_chars(block))],
                            is_error=block.is_error,
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

    def _eligible(self, nodes: list[SessionNode]) -> list[tuple[str, str]]:
        """(node_id, tool_use_id) of every trimmable result, oldest first."""
        found: list[tuple[str, str, int, bool]] = []
        for node in nodes:
            if node.message.role != "user":
                continue
            for block in node.message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                found.append(
                    (node.node_id, block.tool_use_id, block_chars(block),
                     bool(block.is_error))
                )
        keep = self.config.keep_recent_tool_results
        candidates = found[:-keep] if keep else found
        return [
            (nid, tuid)
            for nid, tuid, chars, is_error in candidates
            if not is_error
            and chars >= self.config.min_result_chars
            and (nid, tuid) not in self._trimmed
        ]

    def trim_batch(self, nodes: list[SessionNode]) -> list[tuple[str, str]]:
        """Trim every eligible result NOW, ignoring the trigger. Returns the
        newly trimmed ``(node_id, tool_use_id)`` pairs (empty when nothing was
        eligible) so the caller can persist the decision."""
        if not self.config.enabled:
            return []
        newly = self._eligible(nodes)
        self._trimmed.update(newly)
        return newly

    def mark_trimmed(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Re-apply previously persisted trim decisions (resume rehydration)."""
        self._trimmed.update(tuple(p) for p in pairs)

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
        # Keep this read-only while still reporting the exact view the next
        # request will carry: previously consumed images are deterministically
        # compacted even before apply() persists the sticky key.
        trimmed = self._trimmed | self._consumed_image_results(nodes)
        return self._render(nodes, trimmed)

    def apply(self, nodes: list[SessionNode]) -> list[Message]:
        """Render history with sticky trims; arm a new batch when over trigger."""
        if not self.config.enabled:
            return [n.message for n in nodes]
        # Consume-once semantics for screenshots and image reads. This is
        # independent of the ordinary text-size trigger because provider image
        # tokenization can dwarf the base64 chars/4 estimate.
        self._trimmed.update(self._consumed_image_results(nodes))
        rendered = self._render(nodes)
        if estimate_tokens_of_messages(rendered) > self.config.trigger_tokens:
            newly = self._eligible(nodes)
            if newly:
                self._trimmed.update(newly)
                rendered = self._render(nodes)
        return rendered


def estimate_tokens_of_messages(messages: list[Message]) -> int:
    chars = sum(block_chars(b) for m in messages for b in m.content)
    return chars // 4
