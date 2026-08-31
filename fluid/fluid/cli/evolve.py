"""CLI surfaces for anchors and guarded evolution (Phase 2.1/2.2).

``fluid anchors``  — manage the anchor set: list, add, rm, run (replay and
record baselines), harvest (draft candidates from recent project sessions,
user-confirmed before anything lands).

``fluid evolve``   — the commit gate's first client: propose a per-model
guidance edit that commits only if it survives validity, improvement, and
retention; and the audit surfaces (parked proposals, the evolution log).

This module owns the guidance-specific halves the generic gate deliberately
does not know about: the validator (the prompt vocabulary lives at this
layer) and the apply step (writing ``~/.fluid/guidance/<stem>.md``).
Phase 2.3 will make the synthesizer the second proposer through the same
``CommitGate`` path.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from fluid.codemode.anchors import (
    AnchorStore,
    validate_anchor,
)
from fluid.codemode.evolution import (
    CommitGate,
    Proposal,
    evolution_log,
    parked_proposals,
)
from fluid.codemode.skills import DEFAULT_SKILLS_ROOT
from fluid.config.model import HarnessConfig

_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ANCHOR_TAG_RE = re.compile(
    r'<anchor title="([^"]{1,120})">\n?(.*?)</anchor>', re.DOTALL
)
MAX_HARVEST_DRAFTS = 5

_HARVEST_SYSTEM = """\
You harvest regression anchors for an agent harness. An anchor is a compact
replayable case — a task prompt plus outcome checks — capturing one behavior
the harness currently gets right, so future harness changes can be replayed
against it.

You will see recent sessions: the opening user request, the closing assistant
reply, and which tools ran. Draft anchors only for sessions that clearly
succeeded at something worth protecting: a memory/recall flow that fired, a
convention that held, a task pattern that completed cleanly.

Hard rules:
- The anchor prompt must be natural task phrasing a user would type. It must
  NEVER name a tool or instruct a method — build the situation, not the flow.
- Checks assert outcomes: {"kind": "contains", "pattern": <regex over the
  final reply>}, {"kind": "not_contains", "pattern": ...}, {"kind":
  "tool_called", "name": <tool that should fire>}, {"kind": "file", "path":
  <workspace-relative>, "pattern": <optional regex>}.
- An anchor protecting an assertion must discriminate: a lone "contains"
  check false-passes on hedging replies that mention both the right and the
  wrong answer. Pair it with a "not_contains" for the plausible wrong
  answer whenever one exists.
- Keep prompts self-contained: a fresh session with an empty workspace must
  be able to attempt them. Use a "setup" object of workspace seed files when
  the case needs one.
- Most sessions yield NO anchor — that is the correct common outcome.

Output, and nothing else — up to {max} anchors:
<anchor title="short behavior description">
{"prompt": "...", "checks": [...], "setup": {"relative/path.md": "..."}}
</anchor>
If nothing is worth anchoring: NO_ANCHORS
"""


def skills_root(config: HarnessConfig) -> Path:
    return Path(config.skills_dir or DEFAULT_SKILLS_ROOT)


def guidance_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".fluid" / "guidance"


# ─── guidance proposal: validator + apply ────────────────────────────────────


# A tool named where a directive verb expects one ("call X", "invoke X",
# "the X tool"). ``mentioned_tool_names`` scans for *known* names and so
# cannot flag invented ones; this catches the hallucinated-tool shape at its
# source — learned guidance instructing calls to tools that do not exist.
_TOOL_DIRECTIVE_RE = re.compile(
    r"\b(?:call(?:ing)?|invoke|invoking)\s+(?:the\s+)?([A-Z][A-Za-z0-9_]*)\b"
    r"|\bthe\s+([A-Z][A-Za-z0-9_]*)\s+tool\b"
)


def guidance_validator(proposal: Proposal) -> list[str]:
    """Validity stage for guidance proposals — free, runs before any replay."""
    from fluid.cli.prompt import BUILTIN_TOOL_NAMES, GUIDANCE_FILE_MAX_CHARS
    from fluid.context.screening import screen_findings

    errors: list[str] = []
    if not _STEM_RE.match(proposal.target or ""):
        errors.append(
            f"bad guidance stem {proposal.target!r}: lowercase letters, "
            "digits, dot, dash, underscore"
        )
    content = proposal.content or ""
    if not content.strip():
        errors.append("guidance content is empty")
    if len(content) > GUIDANCE_FILE_MAX_CHARS:
        errors.append(
            f"guidance content exceeds {GUIDANCE_FILE_MAX_CHARS} chars"
        )
    findings = screen_findings(content)
    if findings:
        errors.append(
            "guidance content matches injection screening patterns: "
            + ", ".join(findings)
        )
    unknown = {
        name
        for match in _TOOL_DIRECTIVE_RE.finditer(content)
        for name in match.groups()
        if name and name not in BUILTIN_TOOL_NAMES
    }
    if unknown:
        errors.append(
            "guidance directs calls to tools that do not exist: "
            + ", ".join(sorted(unknown))
            + " (prose about absent capability baits hallucinated calls)"
        )
    return errors


def apply_guidance(proposal: Proposal, *, home: Path | None = None) -> Path:
    """Write the committed guidance file (the gate calls this on commit)."""
    directory = guidance_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{proposal.target}.md"
    content = proposal.content
    if not content.endswith("\n"):
        content += "\n"
    target.write_text(content, encoding="utf-8")
    return target


# ─── command implementations ─────────────────────────────────────────────────


def cmd_anchors(args, config: HarnessConfig) -> int:
    store = AnchorStore(skills_root(config))
    action = args.anchors_command
    if action == "list":
        return _anchors_list(store)
    if action == "add":
        return _anchors_add(args, store)
    if action == "rm":
        if store.delete(args.id):
            print(f"removed {args.id}")
            return 0
        print(f"no anchor {args.id}", file=sys.stderr)
        return 2
    if action == "run":
        return asyncio.run(_anchors_run(args, config, store))
    if action == "harvest":
        return asyncio.run(_anchors_harvest(args, config, store))
    return 2


def _anchors_list(store: AnchorStore) -> int:
    anchors = store.list()
    if not anchors:
        print("no anchors yet — `fluid anchors add` or `fluid anchors harvest`")
        return 0
    for a in anchors:
        print(f"{a.id}  [{a.baseline:^7}]  {a.title}")
        print(f"     prompt: {a.prompt[:100]}")
        print(f"     checks: {json.dumps(list(a.checks), ensure_ascii=False)}")
        if a.last_result and a.last_result != "pass":
            print(f"     last:   {a.last_result}")
    return 0


def _parse_cli_checks(args) -> list[dict]:
    checks: list[dict] = []
    for pattern in args.expect or ():
        checks.append({"kind": "contains", "pattern": pattern})
    for pattern in args.forbid or ():
        checks.append({"kind": "not_contains", "pattern": pattern})
    for name in args.tool or ():
        checks.append({"kind": "tool_called", "name": name})
    for spec in args.check_file or ():
        path, _, pattern = spec.partition(":")
        check: dict = {"kind": "file", "path": path}
        if pattern:
            check["pattern"] = pattern
        checks.append(check)
    return checks


def _anchors_add(args, store: AnchorStore) -> int:
    checks = _parse_cli_checks(args)
    try:
        anchor = store.add(
            title=args.title or args.prompt[:60],
            prompt=args.prompt,
            checks=checks,
            tools=args.tools.split(",") if args.tools else None,
            model=args.anchor_model,
            source="manual",
        )
    except ValueError as exc:
        print(f"fluid: {exc}", file=sys.stderr)
        return 2
    print(f"added {anchor.id}: {anchor.title}")
    print("baseline is unknown until `fluid anchors run` replays it")
    return 0


async def _anchors_run(args, config: HarnessConfig, store: AnchorStore) -> int:
    from fluid.cli.replay import AnchorRunner

    targets = store.list()
    if getattr(args, "id", None):
        targets = [a for a in targets if a.id == args.id]
        if not targets:
            print(f"no anchor {args.id}", file=sys.stderr)
            return 2
    if not targets:
        print("no anchors to run")
        return 0
    runner = AnchorRunner(config, provider_name=getattr(args, "provider", None))
    failed = 0
    for anchor in targets:
        outcome = await runner.replay(anchor)
        passed = not outcome.failures
        detail = "; ".join(outcome.failures)
        store.record_result(anchor.id, passed=passed, detail=detail)
        marker = "pass" if passed else "FAIL"
        print(f"{anchor.id}  {marker}  {anchor.title}")
        if not passed:
            failed += 1
            print(f"     {detail[:300]}")
    print(f"{len(targets) - failed}/{len(targets)} anchors pass; baselines recorded")
    return 0 if failed == 0 else 1


async def _anchors_harvest(args, config: HarnessConfig, store: AnchorStore) -> int:
    from fluid.cli.config import make_store

    session_store = make_store(getattr(args, "store", None))
    drafts = await draft_anchors(
        config,
        session_store,
        limit=getattr(args, "limit", None) or 10,
        provider_name=getattr(args, "provider", None),
    )
    if not drafts:
        print("nothing worth anchoring in recent sessions")
        return 0
    saved = 0
    for title, body in drafts:
        print(f"\ncandidate: {title}")
        print(f"  prompt: {body.get('prompt', '')[:120]}")
        print(f"  checks: {json.dumps(body.get('checks', []), ensure_ascii=False)}")
        if body.get("setup"):
            print(f"  setup:  {sorted(body['setup'])}")
        if not getattr(args, "yes", False):
            answer = input("keep it? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                continue
        try:
            anchor = store.add(
                title=title,
                prompt=body.get("prompt", ""),
                checks=body.get("checks", []),
                setup=body.get("setup"),
                source="harvest",
            )
        except ValueError as exc:
            print(f"  skipped: {exc}", file=sys.stderr)
            continue
        saved += 1
        print(f"  saved as {anchor.id}")
    if saved:
        print(f"\n{saved} anchor(s) saved — run `fluid anchors run` to record baselines")
    return 0


async def draft_anchors(
    config: HarnessConfig,
    session_store,
    *,
    limit: int = 10,
    provider_name: str | None = None,
) -> list[tuple[str, dict]]:
    """One tool-less drafting completion over recent project sessions.

    Returns ``(title, anchor-body)`` candidates; the caller confirms each.
    The session store records no success labels (by design), so the drafting
    model judges from the visible exchange and the human curates.
    """
    from fluid.config.loader import _build_provider_client
    from fluid.messages.content import Message, Text
    from fluid.providers.base import ProviderRequest
    from fluid.store.base import project_root_for

    recent = await session_store.recent_sessions(limit=max(limit * 3, limit))
    project = project_root_for(config.cwd)
    rows = [s for s in recent if s.project_root == project][:limit]
    if not rows:
        return []
    lines: list[str] = []
    for summary in rows:
        piece = await session_store.session_slice(summary.session_id)
        if piece is None:
            continue
        lines.append(f"## session {summary.session_id}")
        for message in piece.messages:
            tools = f" tools={','.join(message.tools)}" if message.tools else ""
            text = " ".join((message.text or "").split())[:400]
            lines.append(f"[{message.role}]{tools} {text}")
        lines.append("")
    if not lines:
        return []

    name = provider_name or config.default_provider
    client = _build_provider_client(config, name)
    model = config.continual_model or config.model_for(name)
    request = ProviderRequest(
        model=model,
        messages=[Message(role="user", content=[Text(text="\n".join(lines))])],
        system=_HARVEST_SYSTEM.replace("{max}", str(MAX_HARVEST_DRAFTS)),
        tools=[],
        max_tokens=4096,
    )
    parts: list[str] = []
    async for chunk in client.stream(request):
        if chunk.kind == "text_delta":
            parts.append(chunk.payload["text"])
    return parse_anchor_drafts("".join(parts))


def parse_anchor_drafts(text: str) -> list[tuple[str, dict]]:
    """Validated ``(title, body)`` pairs from ``<anchor>`` tags in ``text``."""
    out: list[tuple[str, dict]] = []
    for match in _ANCHOR_TAG_RE.finditer(text or ""):
        title, raw = match.group(1).strip(), match.group(2).strip()
        try:
            body = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(body, dict):
            continue
        error = validate_anchor(
            body.get("prompt", ""), body.get("checks", []), body.get("setup")
        )
        if error is not None:
            continue
        out.append((title, body))
        if len(out) >= MAX_HARVEST_DRAFTS:
            break
    return out


def cmd_evolve(args, config: HarnessConfig) -> int:
    root = skills_root(config)
    action = args.evolve_command
    if action == "parked":
        records = parked_proposals(root)
        if not records:
            print("nothing parked")
            return 0
        for r in records:
            when = r.get("ts", 0)
            print(
                f"[{r.get('stage')}] {r.get('kind')}:{r.get('target')} "
                f"(by {r.get('proposer')}, ts={when:.0f})"
            )
            for reason in r.get("reasons", []):
                print(f"    {reason}")
        return 0
    if action == "log":
        records = evolution_log(root)
        if not records:
            print("no committed evolutions yet")
            return 0
        for r in records:
            print(
                f"{r.get('kind')}:{r.get('target')} committed "
                f"(regressions {r.get('regressions')}/{r.get('budget')}, "
                f"by {r.get('proposer')})"
            )
        return 0
    if action == "propose":
        return asyncio.run(_evolve_propose(args, config))
    return 2


async def _evolve_propose(args, config: HarnessConfig) -> int:
    from fluid.cli.replay import AnchorRunner

    if args.file:
        try:
            content = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"fluid: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        content = args.content or ""

    motivation = None
    if args.motivation_prompt:
        checks: list[dict] = []
        for pattern in args.motivation_expect or ():
            checks.append({"kind": "contains", "pattern": pattern})
        for tool_name in args.motivation_tool or ():
            checks.append({"kind": "tool_called", "name": tool_name})
        motivation = {"prompt": args.motivation_prompt, "checks": checks}

    proposal = Proposal(
        kind="guidance",
        target=args.stem,
        content=content,
        rationale=args.why or "",
        motivation=motivation,
        proposer="manual",
    )
    runner = AnchorRunner(config, provider_name=getattr(args, "provider", None))
    gate = CommitGate(
        AnchorStore(skills_root(config)),
        runner,
        budget=args.budget if args.budget is not None else config.anchor_budget,
    )
    verdict = await gate.evaluate(
        proposal,
        validator=guidance_validator,
        apply=lambda p: apply_guidance(p),
        require_motivation=not args.no_motivation,
    )
    for anchor_id, result in sorted(verdict.anchor_results.items()):
        print(f"  {anchor_id}: {result}")
    if verdict.committed:
        print(
            f"committed: guidance/{proposal.target}.md "
            f"(regressions {verdict.regressions}/{verdict.budget})"
        )
        return 0
    print(f"parked at {verdict.stage}:")
    for reason in verdict.reasons:
        print(f"  {reason}")
    return 1
