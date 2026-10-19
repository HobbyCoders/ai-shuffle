"""Deterministic long native-history benchmark; no credentials or live sessions.

PYTHONPATH=fluid python scripts/benchmark_compaction_target.py
Token units are Fluid's chars/4 estimator, not billed provider tokens. The cache
impact is the old/new rendered suffix at the first rewritten message (static
system/tools prefix remains cacheable). Summary text is a fixed 2k-token stand-in.
"""
import json
from fluid.context.compaction import CompactionConfig, Compactor
from fluid.context.trim import estimate_tokens_of_messages
from fluid.messages import Message, Text, ToolUseBlock, ToolResultBlock
from fluid.store.base import SessionNode


def history(turns=80):
    nodes = []
    def add(role, blocks):
        nodes.append(SessionNode(node_id=f"node_{len(nodes):06d}",
            parent_id=nodes[-1].node_id if nodes else None,
            message=Message(role=role, content=blocks)))
    for i in range(turns):
        add("user", [Text(text=f"Implement step {i}, preserve earlier acceptance criteria. " + "requirement " * 100)])
        for j in range(3):
            tid = f"read_{i}_{j}"
            add("assistant", [Text(text="Inspect and test the implementation. " * 30),
                ToolUseBlock(id=tid, name="Read", input={"file_path": f"/repo/module_{i}.py"})])
            add("user", [ToolResultBlock(tool_use_id=tid, content=[Text(text="code and test output " * 600)])])
        add("assistant", [Text(text="Step complete; test output checked. " * 70)])
    add("user", [Text(text="Continue the newest in-flight implementation; keep this request intact.")])
    return nodes


def measure(window, mode, overhead=12000):
    nodes = history(max(4, window // 16000))
    c = Compactor(config=CompactionConfig(summary_enabled=True))
    before_view = c.view(nodes)
    before = c.estimate_view_tokens(nodes, overhead)
    if mode == "manual":
        promotions = c.compact_now(nodes, context_window=window, overhead_tokens=overhead)
    else:
        _, promotions = c.apply(nodes, context_window=window, overhead_tokens=overhead)
    summary_calls = 0
    region = c.summary_region(nodes, context_window=window,
                              for_target=c.estimate_view_tokens(nodes, overhead) > window * c.config.deep_target_window_ratio)
    # Match the manual path; automatic consumes a ready/background checkpoint.
    if region and (mode == "manual" or c.estimate_view_tokens(nodes, overhead) > window * c.config.deep_target_window_ratio):
        summary_calls += 1
        p = c.apply_summary(nodes, (*region[:2], "checkpoint work status " * 360), overhead_tokens=overhead)
        if p:
            promotions.append(p)
    view = c.view(nodes)
    first = next((i for i, pair in enumerate(zip(before_view, view)) if pair[0] != pair[1]), min(len(before_view), len(view)))
    return dict(window=window, mode=mode, before=before,
        after=c.estimate_view_tokens(nodes, overhead), target=int(window * c.config.deep_target_window_ratio),
        summary_calls=summary_calls, promotions=len(promotions),
        old_uncached_suffix=estimate_tokens_of_messages(before_view[first:]),
        new_uncached_suffix=estimate_tokens_of_messages(view[first:]), static_cacheable=overhead)


if __name__ == "__main__":
    print(json.dumps([measure(w, m) for w in (400000, 256000, 32000)
                     for m in ("manual", "automatic")], indent=2))
