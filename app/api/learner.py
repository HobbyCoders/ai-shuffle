"""Owner-only view and control of the memory learner's behaviour rules.

The memory learner (fluid.codemode.continual) is the only learner. Next to
facts it writes *behaviour rules* ("When X: do Y") taken from moments where
the owner corrected the agent or was frustrated with it. Rules are facts in
the same store under a ``behavior.<family>.<digest>`` key, so they sync to
every worker like any memory. This router lists them with the learner's
setup and recent passes, and lets the owner add, edit, keep, undo or delete
a rule. The on/off switch and the daily call budget live in the owner policy
(``/api/v1/settings/owner-policy``).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.api.auth import require_admin
from app.api.memories import (
    _VERSION_CHARS,
    _metadata_version,
    _project_names,
    _require_metadata_version,
    _run,
    _visible,
)
from app.core.fluid_memory import store_for_owner
from fluid.codemode import behavior, facts
from fluid.codemode.transactions import file_transaction

logger = logging.getLogger(__name__)
_NOT_FOUND = "This rule could not be found on this worker."
_NOT_RULE = "This memory is not a behaviour rule."
# A learner rule counts as new for this long after it was written or changed.
NEW_FOR_SECONDS = 3 * 86400
ACTIVITY_ROWS = 30
_RULE_WHEN = "When working with the owner (behaviour rule)"


class LearnerRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def plain_validation(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Do not echo rejected input.
                raise HTTPException(422, "Provide rule text, who it applies to, and the version of the rule you opened.") from None

        return plain_validation


router = APIRouter(prefix="/api/v1/learner", tags=["learner"], route_class=LearnerRoute,
                   dependencies=[Depends(require_admin)])


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr
    applies_to: StrictStr


class RuleUpdate(RuleCreate):
    expected_revision: int = Field(ge=1, strict=True)
    expected_metadata_version: StrictStr = Field(min_length=1, max_length=_VERSION_CHARS)


class RuleVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    expected_metadata_version: StrictStr = Field(min_length=1, max_length=_VERSION_CHARS)


# ── status ───────────────────────────────────────────────────────────────
def _setup(store) -> dict[str, Any]:
    """The learner's switch, budget, today's calls and the model it uses."""
    from app.core.fluid_memory import learner_billing, learner_daily_calls, learner_enabled
    from fluid.codemode.continual import (
        BILLING_SUBSCRIPTION,
        METERED_PROMPT_CHAR_LIMIT,
        SUBSCRIPTION_ASSUMED_WINDOW,
        SYNTH_MAX_TOKENS,
        learner_budget,
        prompt_char_limit,
    )

    model = provider = billing = window = None
    try:
        from app.core.provider.harness_adapter import (
            SessionOptions,
            _learner_target,
            _lookup_provider_row,
            _lookup_provider_row_shape,
        )

        target = _learner_target(SessionOptions(model=""), "local")
        if target is not None:
            model = target.model
            window = target.context_window
            provider_id = target.options.extra.get("provider_id")
            if provider_id:
                row = _lookup_provider_row(str(provider_id)) or {}
                provider = row.get("name") or None
                billing = learner_billing(*_lookup_provider_row_shape(str(provider_id))[:2])
            else:
                # No row: the Claude Code CLI leg, billed by Claude's auth mode.
                billing = learner_billing(None, None)
    except Exception as exc:  # noqa: BLE001 — setup reads are best-effort
        logger.warning("Could not resolve the learner model (%s)", type(exc).__name__)
    setting = learner_daily_calls()
    budget = learner_budget(setting, billing)
    spent = store._facts.inference_budget()
    today = int(time.time() // 86400)
    # The most journal text one pass may send (Synthesizer._size_request).
    if billing == BILLING_SUBSCRIPTION:
        pass_chars = prompt_char_limit(window or SUBSCRIPTION_ASSUMED_WINDOW, SYNTH_MAX_TOKENS)
    else:
        pass_chars = min(prompt_char_limit(window, SYNTH_MAX_TOKENS), METERED_PROMPT_CHAR_LIMIT)
    return {
        "enabled": bool(learner_enabled()),
        "daily_calls_setting": int(setting or 0),
        "daily_cap": budget.daily,
        "cap_source": budget.source,
        "calls_today": int(spent.get("calls") or 0) if spent.get("day") == today else 0,
        "model": model,
        "provider": provider,
        "billing": billing or "metered",
        "pass_chars": pass_chars,
    }


# ── rules ────────────────────────────────────────────────────────────────
def _previous_text(previous: dict, family: str) -> str | None:
    """What Undo restores, naming the earlier family when the learner moved
    the rule to another one."""
    text = previous.get("text")
    was = behavior.rule_family(previous.get("key")) if previous.get("key") else None
    if text and was and was != family:
        return f"{behavior.FAMILY_LABELS.get(was, was)}: {text}"
    return text


def _rule_item(entry, project_names, now) -> dict[str, Any]:
    family = behavior.rule_family(entry.get("key")) or behavior.ALL
    kept = bool(entry.get("protected"))
    history = entry.get("history") or []
    scope = entry.get("scope", "global")
    return {
        "id": entry["id"],
        "revision": entry.get("revision", 1),
        "metadata_version": _metadata_version(entry),
        "text": entry["text"],
        "applies_to": family,
        "applies_label": behavior.FAMILY_LABELS.get(family, family),
        "kept": kept,
        "new": not kept and now - float(entry.get("updated_at") or 0) < NEW_FOR_SECONDS,
        # What an undo brings back; None means undo deletes the rule.
        "previous_text": None if kept or not history else _previous_text(history[-1], family),
        "scope_label": ("All projects" if scope == "global" else
                        project_names.get(scope[8:], "Other project") if scope.startswith("project:")
                        else "Other project"),
        "needs_review": bool(entry.get("conflicted")),
        "created_at": entry.get("created_at") or None,
        "updated_at": entry.get("updated_at") or None,
        "expires_at": None if kept else entry.get("expires_at") or None,
    }


def _rule_rows(doc) -> list[dict]:
    return [entry for entry in doc["entries"]
            if behavior.is_rule_key(entry.get("key")) and _visible(entry, doc["store_id"])
            and not facts.is_superseded(entry)]


# ── activity ─────────────────────────────────────────────────────────────
def _activity(store, rule_text: dict[str, str]) -> list[dict]:
    """The learner's recent passes, newest first, from the store's pass log.
    Errors are shown by kind only: a validator message can quote fact text."""
    try:
        lines = (Path(store.root) / ".synthesis-log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in reversed(lines):
        if len(rows) >= ACTIVITY_ROWS:
            break
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        changed = [str(c) for c in row.get("changed") or []]
        rules = [c.split(":", 1)[1] for c in changed if c.startswith("rule:")]
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        error = str(row.get("error") or "")
        rows.append({
            "at": row.get("ts"),
            "kind": str(row.get("kind") or "pass"),
            "ran": row.get("ran") is not False,
            "read": int(row.get("consumed") or 0),
            "rules": [{"id": rid, "text": rule_text.get(rid)} for rid in rules],
            "memories": sum(1 for c in changed if c.startswith("memory")),
            "input_tokens": int(usage.get("input_tokens") or 0) or None,
            "output_tokens": int(usage.get("output_tokens") or 0) or None,
            "outcome": ("budget" if error.startswith("InferenceBudgetExhausted")
                        else "skipped" if row.get("ran") is False or error.startswith("skipped:")
                        else "error" if error else "ok"),
            "error_kind": error.split(":", 1)[0][:60] if error else None,
        })
    return rows


# ── built-in guidance ────────────────────────────────────────────────────
def _guidance() -> list[dict]:
    """The guidance blocks AI Shuffle ships in code, and which model
    families get each. Read-only: they change with releases."""
    from fluid.cli.prompt import FAMILY_GUIDANCE, GENERIC_FAMILY

    blocks: dict[str, dict] = {}
    for family, pack in FAMILY_GUIDANCE.items():
        for block in pack:
            title, _, body = block.partition("\n")
            item = blocks.setdefault(block, {"title": title.lstrip("# ").strip(),
                                             "text": body.strip(), "families": []})
            item["families"].append("other models" if family == GENERIC_FAMILY else family)
    return list(blocks.values())


def _overview():
    store = store_for_owner("local", initialize=False)
    doc = store._facts.snapshot(read_only=True)
    rows = _rule_rows(doc)
    names = _project_names(rows)
    now = time.time()
    rules = [_rule_item(entry, names, now) for entry in rows]
    rules.sort(key=lambda r: (not r["new"], r["kept"], -(r["updated_at"] or 0)))
    return {
        "setup": _setup(store),
        "rules": rules,
        "activity": _activity(store, {r["id"]: r["text"] for r in rules}),
        "guidance": _guidance(),
        "applies_to": [{"value": v, "label": behavior.FAMILY_LABELS.get(v, v)} for v in behavior.APPLIES_TO],
        "limits": {"rule_chars": behavior.MAX_RULE_CHARS, "learned_rules": behavior.MAX_LEARNED_RULES},
    }


def _find_rule(store, rule_id):
    doc = store._facts.snapshot(read_only=True)
    for entry in doc["entries"]:
        if entry["id"] == rule_id and _visible(entry, doc["store_id"]):
            if not behavior.is_rule_key(entry.get("key")):
                raise HTTPException(404, _NOT_RULE)
            return entry
    raise HTTPException(404, _NOT_FOUND)


def _applies(raw) -> str:
    applies = behavior.normalize_applies_to(raw)
    if applies is None:
        raise HTTPException(422, "Choose which models the rule applies to.")
    return applies


def _text(raw) -> str:
    try:
        return behavior.validate_rule(raw)
    except ValueError as exc:
        reason = str(exc)
        raise HTTPException(422, reason[:1].upper() + reason[1:] + ".") from None


def _saved(store, rule_id):
    entry = _find_rule(store, rule_id)
    return _rule_item(entry, _project_names([entry]), time.time())


def _create(body: RuleCreate):
    applies, text = _applies(body.applies_to), _text(body.text)
    store = store_for_owner("local")
    saved = store.save_memory(text, scope="global", provenance="user", protected=True,
                              key=behavior.rule_key(applies, text), when=_RULE_WHEN)
    return _saved(store, saved.id)


def _update(rule_id, body: RuleUpdate):
    applies, text = _applies(body.applies_to), _text(body.text)
    store = store_for_owner("local")
    with file_transaction(store._facts.lock):
        old = _find_rule(store, rule_id)
        _require_metadata_version(old, body.expected_metadata_version)
        digest = str(old["key"]).rsplit(".", 1)[-1]
        # Saved by the owner, the rule is kept: the learner reads it but
        # never rewrites it, and it no longer fades.
        saved = store.save_memory(text, memory_id=rule_id, expected_revision=body.expected_revision,
                                  provenance="user", protected=True,
                                  key=f"{behavior.KEY_PREFIX}{applies}.{digest}", when=_RULE_WHEN)
        return _saved(store, saved.id)


def _undo(rule_id, body: RuleVersion):
    """Undo the learner's last change to a rule: bring back the text it
    replaced, or delete a rule the learner added (with a deletion barrier,
    so the same rule is not learned again). Kept rules have nothing to undo."""
    store = store_for_owner("local")
    with file_transaction(store._facts.lock):
        old = _find_rule(store, rule_id)
        _require_metadata_version(old, body.expected_metadata_version)
        if old.get("protected"):
            raise HTTPException(409, "You kept this rule, so there is no learner change to undo. Edit or delete it instead.")
        if time.time() - float(old.get("updated_at") or 0) >= NEW_FOR_SECONDS:
            raise HTTPException(409, "Undo is only available for three days after the learner changes a rule. Edit or delete it instead.")
        history = old.get("history") or []
        if history and history[-1].get("text"):
            # A family move recorded the rule's previous key: restore both.
            store.save_memory(history[-1]["text"], memory_id=rule_id,
                              expected_revision=body.expected_revision, provenance=old.get("provenance"),
                              durable=True, key=history[-1].get("key") or old["key"],
                              when=old.get("when") or _RULE_WHEN)
            return {"restored": _saved(store, rule_id)}
        if not _forget_rule(store, rule_id, body.expected_revision):
            raise HTTPException(404, _NOT_FOUND)
        return {"deleted": rule_id}


def _forget_rule(store, rule_id, expected_revision) -> bool:
    """Delete a rule on every worker and stop it being learned again, without
    purging the learner's unread chat evidence (a memory Forget does that)."""
    return store.delete_memory(rule_id, explicit=True, expected_revision=expected_revision,
                               purge_evidence=False)


def _delete(rule_id, expected_revision, expected_metadata_version):
    store = store_for_owner("local")
    with file_transaction(store._facts.lock):
        _require_metadata_version(_find_rule(store, rule_id), expected_metadata_version)
        if not _forget_rule(store, rule_id, expected_revision):
            raise HTTPException(404, _NOT_FOUND)


@router.get("")
async def learner_overview():
    return await _run(_overview)


@router.post("/rules", status_code=201)
async def create_rule(body: RuleCreate):
    return await _run(_create, body)


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate):
    return await _run(_update, rule_id, body)


@router.post("/rules/{rule_id}/undo")
async def undo_rule(rule_id: str, body: RuleVersion):
    return await _run(_undo, rule_id, body)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: str, expected_revision: Annotated[int, Query(ge=1)],
                      expected_metadata_version: Annotated[str, Query(min_length=1, max_length=_VERSION_CHARS)]):
    await _run(_delete, rule_id, expected_revision, expected_metadata_version)
    return Response(status_code=204)
