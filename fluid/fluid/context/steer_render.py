"""How a delivered steer renders once it sits in compacted history.

A steer rides a tool result (see :mod:`fluid.context.steering`). While the
result is live, its text is part of the result exactly as delivered. Once the
result is trimmed, digested or folded, the compaction renderers re-render the
result's ``delivered_steers`` beside the placeholder or digest line, and every
one of those sites goes through :func:`render_steers`.

Render version 1 re-renders every steer verbatim. Render version 2 keeps user
and peer steers verbatim but turns a large harness notice (``origin ==
"system"``: task notifications, watcher reports) into a receipt: who sent it,
how long it was, the first few hundred characters, and the HistoryExpand ref
that re-reads the whole body. A 36k task notification otherwise rides every
later request in full. Identical notifications delivered on one result
collapse into a single receipt.

The receipt is only used when the body can actually be re-read: the recall
tools are on and the stored node's expanded text contains the body. Anything
else renders verbatim, so no receipt ever makes a steer unrecoverable.

Everything here is a pure function of the stored node and the config, so a
render is byte-identical on every call and after a resume.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fluid.messages.content import SteerDelivery
from fluid.store.base import SessionNode

# Render version 1 is the original renderer: every steer verbatim. Version 2
# adds system-notice receipts. Persisted decisions carry the version they
# were made under, so an old session keeps rendering exactly as it did.
RENDER_VERSION_LEGACY = 1
CURRENT_RENDER_VERSION = 2


@dataclass(frozen=True, kw_only=True)
class SteerRenderConfig:
    # A system notice at least this long renders as a receipt.
    receipt_min_chars: int = 1_000
    # How much of the notice body the receipt quotes.
    receipt_preview_chars: int = 300
    # Receipts point at HistoryExpand; without the recall tools there is no
    # way back to the body, so every steer stays verbatim.
    recall_tools: bool = True


def _receipt_offset(
    delivery: SteerDelivery, node: SessionNode | None, cfg: SteerRenderConfig
) -> int | None:
    """Where the body starts in ``HistoryExpand(node)`` output, when
    ``delivery`` qualifies for a receipt; None when it renders verbatim."""
    if (
        node is None
        or not cfg.recall_tools
        or delivery.origin != "system"
        or len(delivery.text) < cfg.receipt_min_chars
    ):
        return None
    from fluid.context.recall import steer_body_offset

    return steer_body_offset(node, delivery.text)


def _receipt(
    delivery: SteerDelivery, node: SessionNode, offset: int, count: int,
    cfg: SteerRenderConfig,
) -> str:
    body = " ".join(delivery.text.split())
    preview = body[: cfg.receipt_preview_chars]
    sender = delivery.sender or "the harness"
    copies = f" ({count} identical copies delivered together)" if count > 1 else ""
    return (
        f"\n\n[harness notice receipt — an automated report from {sender}, "
        f"not from the user; {len(delivery.text)} chars delivered at "
        f"{node.node_id}{copies}. It is NOT in context now. It began: "
        f'"{preview}…" Re-read it verbatim with '
        f'HistoryExpand("{node.node_id}", offset={offset}) if any detail '
        f"matters]"
    )


def render_steer(
    delivery: SteerDelivery,
    cfg: SteerRenderConfig | None,
    *,
    node: SessionNode | None = None,
    count: int = 1,
) -> str:
    """One delivered steer as compacted history shows it.

    ``cfg`` None is render version 1: verbatim. ``node`` is the stored node
    that carries the steer; without it no receipt can name a ref, so the
    steer renders verbatim.
    """
    if cfg is None or node is None:
        return delivery.render()
    offset = _receipt_offset(delivery, node, cfg)
    if offset is None:
        return delivery.render()
    return _receipt(delivery, node, offset, count, cfg)


def render_steers(
    deliveries: Iterable[SteerDelivery],
    cfg: SteerRenderConfig | None,
    *,
    node: SessionNode | None = None,
) -> list[str]:
    """Every steer delivered on one result, in delivery order.

    With a config, identical system notices that qualify for a receipt
    collapse into one receipt at the first one's position. User and peer
    steers are never collapsed, even when repeated.
    """
    deliveries = list(deliveries)
    if cfg is None or node is None:
        return [s.render() for s in deliveries]
    offsets = [_receipt_offset(s, node, cfg) for s in deliveries]
    counts: dict[tuple[str, str | None, str], int] = {}
    for delivery, offset in zip(deliveries, offsets):
        if offset is not None:
            key = (delivery.origin, delivery.sender, delivery.text)
            counts[key] = counts.get(key, 0) + 1
    out: list[str] = []
    seen: set[tuple[str, str | None, str]] = set()
    for delivery, offset in zip(deliveries, offsets):
        if offset is None:
            out.append(delivery.render())
            continue
        key = (delivery.origin, delivery.sender, delivery.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(_receipt(delivery, node, offset, counts[key], cfg))
    return out
