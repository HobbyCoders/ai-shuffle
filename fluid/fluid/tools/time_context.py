"""Best-effort host time lookup for tools and internally started delegated wakes."""
from __future__ import annotations


def history_zone(extra) -> str | None:
    try:
        callback = (extra or {}).get("time_context")
        return callback("wakeup").get("zone") if callable(callback) else "UTC"
    except Exception:
        return None


def delegated_wake_time(content, extra, source, *, host_time_note=False):
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
    return [*content, Text(note)]
