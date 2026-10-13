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
  image between two calls of the same turn would rewrite a message the provider
  has already cached and re-bill the whole prompt at full price. The one
  exception is the count rung (``consume_stale_tool_images``): a screenshot
  loop past a fixed number of live images drops all but the newest few in one
  batch, mid-turn, so one cache break covers many steps.
* **The tree is never mutated.** Trimming is a view transformation applied when
  history is rendered for a request; the stored SessionNodes keep full fidelity
  (rewind/fork/audit see everything).

This module is tier 1 of the eviction ladder; tiers 2 and 3 (span digests and
epoch stubs) live in :mod:`fluid.context.compaction`, which owns a trimmer and
drives it through :meth:`ToolResultTrimmer.trim_batch`.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from functools import lru_cache
from dataclasses import dataclass, field, replace
from typing import Any

from fluid.context.steer_render import (
    CURRENT_RENDER_VERSION,
    RENDER_VERSION_LEGACY,
    SteerRenderConfig,
    render_steers,
)
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
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
# for trimming; pricing the base64 by chars/4 would make a 531k PNG look like
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


_REF_RE = re.compile(r"_refs\[(\d+)\]")
_LOADED_JSON_NAME_RE = re.compile(r'"name":\s*"([^"]+)"')
_LOADED_IMPORT_RE = re.compile(r"^- ([A-Za-z0-9_]+): from tools\.", re.MULTILINE)
_MAX_NAMED = 8


def _full_text(block: Any) -> str:
    parts: list[str] = []
    for item in getattr(block, "content", None) or ():
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


# ── Code-mode cells ──────────────────────────────────────────────────────
# A run_python cell's input is Python that calls tools inside the kernel, and
# its JSON form is often half imports. Render version 2 summarizes it instead:
# the first line of real code, the tools it calls with short argument hints,
# the paths and commands it names, and which ToolSearch and Skill loads still
# hold. Everything here is a pure function of the code text.

CELL_ARG_CAP = 60
CELL_FIRST_LINE_CAP = 100
CELL_LAST_LINE_CAP = 160
_CELL_LITERAL_CAP = 120
_CELL_MAX_CALLS = 8
_CELL_MAX_LITERALS = 6
# Tools a cell may call by bare name because an earlier cell imported them.
# Any other bare name counts only when this cell imports it from ``tools``,
# so ``Path(...)`` or ``Counter(...)`` are never taken for a tool call.
_KNOWN_TOOLS = frozenset({
    "AskUser", "Bash", "Edit", "Forget", "GetUsage", "Glob", "Grep",
    "HistoryExpand", "HistorySearch", "Ledger", "ListAgents", "Monitor",
    "Note", "Read", "Recall", "Remember", "SendMessage", "SessionSearch",
    "SessionView", "Skill", "Task", "ToolSearch", "WebFetch", "WebSearch",
    "Write",
})
_PATH_KEYS = ("file_path", "path", "notebook_path")
_COMMAND_KEYS = ("command",)
# What a tool's first positional string argument is, for the tools whose
# first argument is a path, a command or a load target.
_POSITIONAL_KEY = {
    "Read": "file_path", "Write": "file_path", "Edit": "file_path",
    "Bash": "command", "Skill": "name", "ToolSearch": "query",
}
_TOOLS_ATTR_CALL_RE = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_BARE_CALL_RE = re.compile(r"(?<![.\w])([A-Z][A-Za-z]+)\s*\(")
_CELL_FOOTER_PREFIX = "[refs created this cell"


@dataclass(frozen=True)
class CellCall:
    """One tool call found in a cell's source."""

    name: str
    # The call's argument source, whitespace-collapsed and clipped.
    args: str
    # (keyword, value) for every string-literal argument, with the first
    # positional one named after ``_POSITIONAL_KEY``.
    literals: tuple[tuple[str, str], ...] = ()

    def values(self) -> dict[str, str]:
        return dict(self.literals)


def _clip_one_line(text: str, cap: int) -> str:
    text = " ".join(text.split())
    return text[:cap] + ("…" if len(text) > cap else "")


def _first_code_line(code: str) -> str:
    """The first line that is not blank, a comment or an import."""
    first = ""
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        first = first or stripped
        if not stripped.startswith(("import ", "from ")):
            return stripped
    return first


def _tool_name(func: ast.expr, aliases: dict[str, str], modules: set[str]) -> str:
    if isinstance(func, ast.Name):
        if func.id in aliases:
            return aliases[func.id]
        return func.id if func.id in _KNOWN_TOOLS else ""
    chain: list[str] = []
    while isinstance(func, ast.Attribute):
        chain.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name) and func.id in modules and chain:
        return chain[0].split("__")[-1]
    return ""


def cell_calls(code: str) -> list[CellCall]:
    """The tool calls in a cell's source, in source order.

    A cell that does not parse still names its calls, from the ``tools.X(``
    and known bare-name spellings, without argument hints.
    """
    return list(_cell_calls(code))


# Digests and epoch stubs re-render on every request, and an epoch can fold
# hundreds of cells; parsing each one again every time costs whole seconds in
# a long session. The result is a pure function of the code text, so it is
# cached.
@lru_cache(maxsize=4096)
def _cell_calls(code: str) -> tuple[CellCall, ...]:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        names = _TOOLS_ATTR_CALL_RE.findall(code) + [
            n for n in _BARE_CALL_RE.findall(code) if n in _KNOWN_TOOLS
        ]
        return tuple(CellCall(name=n.split("__")[-1], args="…")
                     for n in dict.fromkeys(names))
    aliases: dict[str, str] = {}
    modules = {"tools"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "tools" or node.module.startswith("tools.")
        ):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name.split("__")[-1]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tools" and alias.asname:
                    modules.add(alias.asname)
    found: list[tuple[int, int, CellCall]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _tool_name(node.func, aliases, modules)
        if not name:
            continue
        whole = ast.get_source_segment(code, node) or ""
        func = ast.get_source_segment(code, node.func) or ""
        args = whole[len(func):].strip() if func and whole.startswith(func) else ""
        args = args[1:-1] if args.startswith("(") and args.endswith(")") else "…"
        literals: list[tuple[str, str]] = []
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and name in _POSITIONAL_KEY
        ):
            literals.append((_POSITIONAL_KEY[name], node.args[0].value))
        for kw in node.keywords:
            if (
                kw.arg
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                literals.append((kw.arg, kw.value.value))
        found.append((
            node.lineno, node.col_offset,
            CellCall(name=name, args=_clip_one_line(args, CELL_ARG_CAP),
                     literals=tuple(literals)),
        ))
    return tuple(call for _, _, call in sorted(found, key=lambda f: (f[0], f[1])))


def loaded_by(name: str, values: dict[str, Any]) -> list[str]:
    """What a ToolSearch or Skill call loaded, as display names."""
    if name == "Skill":
        skill = str(values.get("name") or "").strip()
        return [skill] if skill else []
    if name == "ToolSearch":
        query = str(values.get("query") or "").strip()
        if query.startswith("select:"):
            return [n.strip().split("__")[-1]
                    for n in query[len("select:"):].split(",") if n.strip()]
        return [f"{query!r} matches"] if query else []
    return []


def describe_loaded(loads: Iterable[tuple[str, str]]) -> str:
    """``Skill a, b; ToolSearch x, y`` from (tool, item) pairs, deduped."""
    grouped: dict[str, list[str]] = {}
    for tool, item in loads:
        items = grouped.setdefault(tool, [])
        if item not in items:
            items.append(item)
    return "; ".join(
        f"{tool} {', '.join(items[:_MAX_NAMED])}"
        + (f" (+{len(items) - _MAX_NAMED} more)" if len(items) > _MAX_NAMED else "")
        for tool, items in grouped.items()
    )


@lru_cache(maxsize=4096)
def summarize_cell(code: str) -> str:
    """One line saying what a run_python cell did, for digests and
    placeholders. Pure: the same code always gives the same line."""
    parts: list[str] = []
    first = _first_code_line(code)
    if first:
        parts.append(f"first line: {_clip_one_line(first, CELL_FIRST_LINE_CAP)}")
    calls = cell_calls(code)
    if calls:
        shown = list(dict.fromkeys(f"{c.name}({c.args})" for c in calls))
        more = (f" (+{len(shown) - _CELL_MAX_CALLS} more)"
                if len(shown) > _CELL_MAX_CALLS else "")
        parts.append(f"calls {', '.join(shown[:_CELL_MAX_CALLS])}{more}")
        hints = " ".join(shown[:_CELL_MAX_CALLS])
        for label, keys, sep in (("paths", _PATH_KEYS, ", "),
                                 ("commands", _COMMAND_KEYS, " | ")):
            # A literal the call hint already shows in full is not repeated.
            values = [
                _clip_one_line(v, _CELL_LITERAL_CAP)
                for c in calls for k, v in c.literals
                if k in keys and v.strip() and v not in hints
            ]
            values = list(dict.fromkeys(values))
            if values:
                parts.append(f"{label}: {sep.join(values[:_CELL_MAX_LITERALS])}")
        loaded = describe_loaded(
            (c.name, item) for c in calls for item in loaded_by(c.name, c.values())
        )
        if loaded:
            parts.append(f"still loaded: {loaded}")
    return "; ".join(parts) if parts else "(empty cell)"


def last_output_line(text: str, cap: int = CELL_LAST_LINE_CAP) -> str:
    """The last printed line of a result, skipping the refs footer."""
    for line in reversed((text or "").splitlines()):
        line = " ".join(line.split())
        if line and not line.startswith(_CELL_FOOTER_PREFIX):
            return line[:cap] + ("…" if len(line) > cap else "")
    return ""


# A traceback in the last TRACEBACK_TAIL_CHARS of a successful result is an
# error the model printed rather than raised. It is held back from trimming
# one turn longer than other output (see ToolResultTrimmer._eligible).
TRACEBACK_TAIL_CHARS = 2_000
_TRACEBACK_MARK = "Traceback (most recent call last)"


def has_traceback_tail(block: Any) -> bool:
    return _TRACEBACK_MARK in _full_text(block)[-TRACEBACK_TAIL_CHARS:]


def describe_trimmed_call(
    call: Any, full_text: str, *, render_version: int = RENDER_VERSION_LEGACY,
) -> str:
    """What a trimmed result WAS, in terms the model can act on.

    A bare "trimmed" placeholder makes the model redo work it can recover
    cheaply, so:

    * a code-mode cell whose output named ``_refs[N]`` handles says those
      handles still hold the results (the kernel keeps them across cells);
    * a Read says which file and which lines were dropped;
    * a ToolSearch restates the tool names it loaded — they are still
      loaded; reloading costs the schema text again.

    From render version 2 a code-mode cell also says what it ran (see
    ``summarize_cell``), naming the Read, Skill and ToolSearch calls inside
    it, and how its output ended. Version 1 output is unchanged.
    """
    name = getattr(call, "name", None)
    input = getattr(call, "input", None) or {}
    if not isinstance(input, dict):
        input = {}
    leaf = str(name or "").split("__")[-1]
    # Only a cell's own output names refs; a Read of a file that mentions
    # ``_refs[3]`` in its text must not be mistaken for one.
    is_cell = leaf == "run_python" or (call is None and "refs created this cell" in (full_text or ""))
    refs = list(dict.fromkeys(_REF_RE.findall(full_text or ""))) if is_cell else []
    cell = ""
    if render_version > RENDER_VERSION_LEGACY and leaf == "run_python":
        code = input.get("code")
        if isinstance(code, str) and code.strip():
            cell = f" This cell ran: {summarize_cell(code)}."
        last = last_output_line(full_text)
        if last:
            cell += f' Its output ended: "{last}".'
    if refs:
        shown = ", ".join(f"_refs[{r}]" for r in refs[:_MAX_NAMED])
        more = f" (+{len(refs) - _MAX_NAMED} more)" if len(refs) > _MAX_NAMED else ""
        return cell + (
            f" The tool results this cell produced are still in the kernel as "
            f"{shown}{more} (unless the kernel restarted since) — slice those "
            f"before reaching for HistoryExpand or re-running the tools."
        )
    if cell:
        return cell
    if leaf == "Read":
        path = str(input.get("file_path") or "?")
        offset = input.get("offset")
        limit = input.get("limit")
        try:
            start = max(1, int(offset)) if offset is not None else None
        except (TypeError, ValueError):
            start = None
        try:
            count = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            count = None
        if start is not None and count is not None:
            window = f"lines {start}–{start + count - 1}"
        elif start is not None:
            window = f"from line {start}"
        else:
            window = "whole file"
        return f" This was: read {path} {window} (dropped)."
    if leaf == "ToolSearch":
        query = str(input.get("query") or "")
        names: list[str] = []
        if query.startswith("select:"):
            names = [n.strip().split("__")[-1] for n in query[len("select:"):].split(",") if n.strip()]
        if not names:
            names = _LOADED_IMPORT_RE.findall(full_text or "")
        if not names:
            names = [n.split("__")[-1] for n in _LOADED_JSON_NAME_RE.findall(full_text or "")]
        names = list(dict.fromkeys(names))
        if names:
            shown = ", ".join(names[:_MAX_NAMED])
            more = f" (+{len(names) - _MAX_NAMED} more)" if len(names) > _MAX_NAMED else ""
            return (
                f" This ToolSearch loaded: {shown}{more} — they are still "
                f"loaded and callable; do not ToolSearch them again."
            )
        return f" This was a ToolSearch for {query!r}; its tools stay loaded."
    return ""


def _placeholder(
    node_id: str, chars: int, head: str = "", *, recall: bool = True,
    call: Any = None, full_text: str = "",
    render_version: int = RENDER_VERSION_LEGACY,
) -> Text:
    excerpt = f' It began: "{head}"' if head else ""
    what = describe_trimmed_call(call, full_text, render_version=render_version)
    route = (
        f'recover it verbatim with HistoryExpand("{node_id}") when any detail matters'
        if recall
        else "re-run the tool when any detail matters"
    )
    return Text(
        f"[old tool result trimmed to save context — {chars} chars removed.{excerpt}{what} "
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

    HistoryExpand recovers TEXT and cannot put an image back in context, so
    this says what happened, says plainly that HistoryExpand is not the
    recovery route, and names the one that is: re-Read the file.

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
    # The render version new trim decisions are made under (see
    # fluid.context.steer_render). The owning Compactor sets it from its own
    # config, together with the receipt caps in ``steer_render``.
    render_version: int = CURRENT_RENDER_VERSION
    steer_render: SteerRenderConfig = field(default_factory=SteerRenderConfig)
    # Render version of each rehydrated trim decision. A pair missing here
    # was decided by this process and renders at ``render_version``.
    _trim_versions: dict = field(default_factory=dict)

    @staticmethod
    def _has_image(block: ToolResultBlock) -> bool:
        return any(isinstance(inner, Image) for inner in block.content or [])

    def _consumed_image_results(
        self, nodes: list[SessionNode], *, include_errors: bool = False
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
                    and (include_errors or not block.is_error)
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
            delivered_steers=block.delivered_steers,
            delivery_metadata_version=block.delivery_metadata_version,
        )

    def _render(
        self,
        nodes: list[SessionNode],
        trimmed: set[tuple[str, str]] | None = None,
        consumed: set[tuple[str, str]] | None = None,
        calls: dict[str, Any] | None = None,
    ) -> list[Message]:
        """``calls`` maps tool_use_id to its call for a caller that renders
        one node at a time (the Compactor). Without it the map is built from
        ``nodes``. A version 1 decision always uses the map built from
        ``nodes``, so it renders exactly as it did before the map was passed.
        """
        trimmed = self._trimmed if trimmed is None else trimmed
        consumed = self._image_consumed if consumed is None else consumed
        if not trimmed and not consumed:
            return [n.message for n in nodes]
        # The call behind each result, so a placeholder can say what the
        # result was (see describe_trimmed_call).
        local_calls: dict[str, Any] = {}
        for node in nodes:
            if node.message.role == "assistant":
                for block in node.message.content:
                    if isinstance(block, ToolUseBlock):
                        local_calls[block.id] = block
        call_map = local_calls if calls is None else calls
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
                    if self._has_image(block):
                        rendered = _image_placeholder(
                            node.node_id, block_chars(block), block_head(block),
                            recall=self.config.recall_tools,
                        )
                    else:
                        version = self._trim_versions.get(key, self.render_version)
                        source = (
                            local_calls if version <= RENDER_VERSION_LEGACY
                            else call_map
                        )
                        rendered = _placeholder(
                            node.node_id, block_chars(block), block_head(block),
                            recall=self.config.recall_tools,
                            call=source.get(block.tool_use_id),
                            full_text=_full_text(block),
                            render_version=version,
                        )
                    blocks.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=[rendered]
                            + [Text(s) for s in render_steers(
                                block.delivered_steers,
                                self._steer_config(key), node=node,
                            )],
                            is_error=block.is_error,
                            delivered_steers=block.delivered_steers,
                            delivery_metadata_version=block.delivery_metadata_version,
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

    def _steer_config(self, key: tuple[str, str]) -> SteerRenderConfig | None:
        """How the steers on trimmed result ``key`` render: None (verbatim)
        for a version-1 decision, receipts for a later one."""
        version = self._trim_versions.get(key, self.render_version)
        if version <= RENDER_VERSION_LEGACY:
            return None
        if self.steer_render.recall_tools != self.config.recall_tools:
            return replace(self.steer_render, recall_tools=self.config.recall_tools)
        return self.steer_render

    def _eligible(
        self,
        nodes: list[SessionNode],
        *,
        keep_recent: int | None = None,
        protected_from: int | None = None,
    ) -> list[tuple[str, str]]:
        """(node_id, tool_use_id) of every trimmable result, oldest first.

        ``keep_recent`` overrides the config's recency guard for one call;
        overflow recovery passes 0 because the result that no longer fits is
        usually the newest one.

        ``protected_from`` is the index in ``nodes`` where the compactor's
        token-budgeted recency tail starts. When given (and ``keep_recent``
        is not), it replaces ``keep_recent_tool_results``: every result
        before that index is in reach, every result from it on is kept.
        Without it the count-based guard still applies, which is what an
        unknown window and the trim-only contract get.
        """
        found: list[tuple[str, str, int, bool]] = []
        if protected_from is not None and keep_recent is None:
            nodes = nodes[:max(0, protected_from)]
            keep_recent = 0
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
        # A traceback printed by a successful call stays one extra turn: it is
        # held until two turn starts follow it. Overflow recovery's explicit
        # keep_recent=0 waives the hold.
        held: set[tuple[str, str]] = set()
        starts = 0
        for node in reversed(nodes) if keep_recent != 0 else ():
            if starts < 2 and node.message.role == "user":
                held.update(
                    (node.node_id, b.tool_use_id) for b in node.message.content
                    if isinstance(b, ToolResultBlock) and not b.is_error
                    and has_traceback_tail(b)
                )
            if node.message.role == "user" and any(
                not isinstance(b, ToolResultBlock) for b in node.message.content
            ):
                starts += 1
        return [
            (nid, tuid)
            for nid, tuid, chars, is_error in candidates
            if not is_error
            and chars >= self.config.min_result_chars
            and (nid, tuid) not in self._trimmed
            and (nid, tuid) not in held
        ]

    def trim_batch(
        self,
        nodes: list[SessionNode],
        *,
        keep_recent: int | None = None,
        protected_from: int | None = None,
    ) -> list[tuple[str, str]]:
        """Trim every eligible result NOW, ignoring the trigger. Returns the
        newly trimmed ``(node_id, tool_use_id)`` pairs (empty when nothing was
        eligible) so the caller can persist the decision. ``keep_recent`` and
        ``protected_from`` override the recency guard for this batch only
        (see ``_eligible``)."""
        if not self.config.enabled:
            return []
        newly = self._eligible(
            nodes, keep_recent=keep_recent, protected_from=protected_from
        )
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

    def live_tool_images(self, nodes: list[SessionNode]) -> list[tuple[str, str]]:
        """Tool results whose images are still in context, oldest first."""
        out: list[tuple[str, str]] = []
        for node in nodes:
            if node.message.role != "user":
                continue
            for block in node.message.content:
                if not isinstance(block, ToolResultBlock) or not self._has_image(block):
                    continue
                key = (node.node_id, block.tool_use_id)
                if key not in self._image_consumed and key not in self._trimmed:
                    out.append(key)
        return out

    def consume_stale_tool_images(
        self, nodes: list[SessionNode], *, keep: int
    ) -> list[tuple[str, str]]:
        """Consume seen tool-result images except the newest ``keep``.

        The count rung for long act-and-look loops (computer use, browser
        screenshots): within one turn the image rung never runs, so every
        screenshot of the turn would ride every later request. Only results a
        completed model call already saw are eligible, and the newest ``keep``
        of those stay, so the current observation never leaves context.
        Unlike ``consume_images``, error results count too: a refused browser
        action attaches a fresh screenshot to its error (stale_screenshot), and
        those would otherwise pile up for the rest of the turn.
        """
        if not self.config.enabled:
            return []
        seen = self._consumed_image_results(nodes, include_errors=True)
        eligible = [
            key for key in self.live_tool_images(nodes)
            if key in seen and key[1] != USER_IMAGE_KEY
        ]
        stale = eligible[: max(0, len(eligible) - max(0, keep))]
        newly = sorted(set(stale) - self._image_consumed - self._trimmed)
        self._image_consumed.update(newly)
        return newly

    def mark_trimmed(
        self,
        pairs: Iterable[tuple[str, str]],
        *,
        render_version: int = RENDER_VERSION_LEGACY,
    ) -> None:
        """Re-apply previously persisted trim decisions (resume rehydration).

        ``render_version`` is the version the decision was persisted under;
        events written before the field existed are version 1 and keep
        rendering exactly as they did.
        """
        for pair in pairs:
            key = tuple(pair)
            self._trimmed.add(key)
            self._trim_versions[key] = render_version

    def mark_image_consumed(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Re-apply persisted image-consumption decisions (resume)."""
        self._image_consumed.update(tuple(p) for p in pairs)

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
        # Decisions only: the next request carries exactly what has been
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

        Image consumption obeys the same two gates as every other rung: it
        needs real pressure (over the trigger) and a turn
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
