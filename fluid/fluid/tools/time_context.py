"""Best-effort host time lookup for tools and internally started delegated wakes."""
from __future__ import annotations


def history_zone(extra) -> str | None:
    try:
        callback = (extra or {}).get("time_context")
        return callback("wakeup").get("zone") if callable(callback) else "UTC"
    except Exception:
        return None


def delegated_wake_time(content, extra, source, *, host_time_note=False, before_context=None):
    """Host submits already carry the note; internal wakes bypass that adapter.

    Never edit earlier nodes or a CLI slash command. Only app-bound loops opt in.
    This function may consult the host store and must run off the event loop.
    """
    from fluid.context.time_awareness import RELATIVE_TIME_RULE, turn_time_lines
    from fluid.messages import Text

    callback = (extra or {}).get("time_context")
    if not callable(callback):
        return content
    # Only the submitter can attest that THIS turn carries its fresh note.
    if host_time_note:
        return content
    text = "".join(b.text for b in content if isinstance(b, Text)).lstrip()
    if text.startswith("/"):
        return content
    try:
        values = callback(source) or {}
    except Exception:
        values = {}
    lines = turn_time_lines(**values)
    if not extra.get("time_rule_in_prefix"):
        lines.append(RELATIVE_TIME_RULE)
    tag = "system" + "-" + "reminder"
    note = f"<{tag}>\n" + "\n".join(lines) + f"\n</{tag}>"
    # Keep a host-pinned memory snapshot at the tail, contiguous with any
    # compaction put-back. Only host-owned offsets, never quoted markers, can
    # select this insertion point. Images and other blocks stay unchanged.
    if before_context is not None:
        offset = 0
        for i, block in enumerate(content):
            if isinstance(block, Text):
                if offset <= before_context <= offset + len(block.text):
                    at = before_context - offset
                    return [*content[:i], Text(block.text[:at]), Text(note + "\n\n"),
                            Text(block.text[at:]), *content[i + 1:]]
                offset += len(block.text)
    return [*content, Text(note)]
