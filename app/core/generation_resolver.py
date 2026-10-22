"""Selection and spending policy shared by the four generation tools.

No credentials, catalog entries or saved defaults belong in tool descriptions.
The caller supplies trusted run context; agents cannot mark a job interactive or
supply their own price. Choice storage is deliberately outside this module.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

KINDS = ("image", "video", "audio", "3d")
# Owner-tunable ceiling for an unconfirmed item OR batch; never round floats here.
CONFIRM_ABOVE_USD = Decimal("0.10")
UNKNOWN_PRICE = {"currency": "USD", "unit": "item", "amount": None, "total": None, "status": "unknown"}
SOURCE_NAMES = ("fal", "openai", "xai", "chatgpt", "supergrok")
SOURCE_ALIASES = {"fal.ai": "fal", "fal-ai": "fal", "grok": "supergrok", "chatgpt-plan": "chatgpt", "supergrok-plan": "supergrok"}
# Every failed or unknown-outcome submission carries this, so an unconfirmed
# outcome is never resubmitted (and possibly paid twice) on the agent's own.
DO_NOT_RETRY = "Do not call again without the user's go-ahead."


@dataclass(frozen=True)
class RunContext:
    session_id: str | None = None
    unattended: bool = True
    owner: bool = False
    api_user_id: str | None = None
    chat_mode: bool = False
    remembered_choice: dict | None = None
    job_choice: dict | None = None
    last_choice: dict | None = None


@dataclass
class Candidate:
    source: str
    account: str
    worker: str
    model: str
    kind: str
    verified: bool
    options: dict = field(default_factory=dict)
    price: dict = field(default_factory=lambda: dict(UNKNOWN_PRICE))
    schema: dict | None = None
    available: bool = True
    reason: str | None = None

    @classmethod
    def from_wire(cls, value):
        from dataclasses import fields
        allowed = {f.name for f in fields(cls)}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Invalid generation catalog from the account-holding worker.")
        if any(not isinstance(value.get(key), str) or not 0 < len(value[key]) <= 512
               for key in ("source", "account", "worker", "model")):
            raise ValueError("Invalid generation source identity from the account-holding worker.")
        if (value.get("kind") not in KINDS or type(value.get("verified")) is not bool
                or not isinstance(value.get("options", {}), dict) or not isinstance(value.get("price", {}), dict)
                or type(value.get("available", True)) is not bool):
            raise ValueError("Invalid generation options from the account-holding worker.")
        return cls(**value)

    def choice(self):
        return {"source": self.source, "account": self.account, "worker": self.worker,
                "model": self.model, "options": self.options}

    def view(self):
        return {**self.choice(), "kind": self.kind, "verified": self.verified,
                "price": self.price, "quick_settings": dict(list(self.options.items())[:4]),
                "schema": self.schema, "available": self.available, "reason": self.reason}


class Backend(Protocol):
    async def catalog(self, kind: str, *, source: str | None = None, model: str | None = None, query: str | None = None, limit: int = 15, prompt: str | None = None) -> list[Candidate]: ...
    async def prepare(self, candidate: Candidate, options: dict, prompt: str) -> Candidate: ...
    async def reserve(self, candidate: Candidate, context: RunContext) -> bool: ...
    async def execute(self, candidate: Candidate, prompt: str, context: RunContext) -> dict: ...


def price_total(price: dict) -> Decimal | None:
    if not isinstance(price, dict):
        return None
    try:
        if price.get("currency") != "USD" or price.get("status") not in ("estimated", "quoted"):
            return None
        amount, total = Decimal(str(price["amount"])), Decimal(str(price["total"]))
        if not amount.is_finite() or not total.is_finite() or amount < 0 or total < amount:
            return None
        return total
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None


def matches(candidate: Candidate, choice: dict) -> bool:
    return all(not choice.get(key) or choice[key] == getattr(candidate, key)
               for key in ("source", "account", "worker", "model"))


def normalize_source(source):
    """Case-insensitive source names and aliases; account IDs are kept exact."""
    if not isinstance(source, str) or not source.strip():
        return source or None
    key = source.strip().casefold()
    return SOURCE_ALIASES.get(key, key if key in SOURCE_NAMES else source.strip())


def no_retry(output):
    output = dict(output) if isinstance(output, dict) else {"success": False}
    error = output.get("error") if isinstance(output.get("error"), str) else "Generation did not confirm an outcome."
    output["error"] = error if DO_NOT_RETRY in error else error.rstrip() + " " + DO_NOT_RETRY
    return output


def result(status, *, price=None, **values):
    return {"status": status, "success": status in ("completed", "catalog"), "price": dict(price or UNKNOWN_PRICE), **values}


class Resolver:
    def __init__(self, backend: Backend):
        self.backend = backend
        # Production receipts are durable across tool-server rebuilds. The
        # bounded in-memory fallback supports standalone/test backends only.
        self._confirmations: dict[str, str] = {}
        self.confirmations = getattr(backend, "confirmations", None)

    async def catalog(self, kind, source=None, model=None, query=None, limit=15):
        if kind not in KINDS:
            return result("refused", error="Unknown generation kind.")
        entries = await self.backend.catalog(kind, source=normalize_source(source), model=model, query=query, limit=limit)
        return result("catalog", options=[entry.view() for entry in entries])

    async def generate(self, kind: str, args: dict, context: RunContext, *, exclude_sources=()) -> dict:
        """``exclude_sources``: sources the caller's policy forbids for this call."""
        if kind not in KINDS:
            return result("refused", error="Unknown generation kind.")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            return result("refused", error="A nonempty prompt of at most 32000 characters is required.")
        stored = context.job_choice or context.remembered_choice
        if context.unattended and not stored:
            return result("skipped", reason="no_stored_choice", kind=kind,
                          message="Generation skipped: this unattended run has no remembered or job-stored choice.")
        explicit = bool(args.get("source") or args.get("model"))
        choice = stored if context.unattended or not explicit else None
        if choice is None:
            choice = {"source": args.get("source"), "model": args.get("model"), "worker": args.get("worker")}
        source = normalize_source(choice.get("source") or choice.get("account"))
        try:
            candidates = await self.backend.catalog(kind, source=source, model=choice.get("model"), limit=4, prompt=prompt,
                                                    worker=choice.get("worker"))
            candidates = [c for c in candidates if matches(c, {k: v for k, v in choice.items() if k != "source"})
                          and (not source or source in (c.source, c.account)) and c.source not in exclude_sources]
            if (explicit or stored) and any(c.available for c in candidates):
                # An unreachable or old peer cannot make a named choice ambiguous
                # when a usable candidate matches; it never runs either way.
                candidates = [c for c in candidates if c.available]
            if not candidates:
                return result("refused", reason="no_match", error="No matching source/account and model is available. No other source was used.")
            if context.unattended and len(candidates) != 1:
                return result("skipped", reason="stored_choice_ambiguous", kind=kind,
                              message="Generation skipped: the stored choice does not identify one account/model binding.")
            if (not stored and not explicit) or len(candidates) != 1:
                if context.last_choice:
                    last = context.last_choice
                    if not explicit and not any(matches(c, last) for c in candidates):
                        prior = await self.backend.catalog(kind, source=last.get("account") or last.get("source"), model=last.get("model"), limit=1, prompt=prompt)
                        candidates.extend(c for c in prior if matches(c, last) and c.source not in exclude_sources)
                    for index, candidate in enumerate(candidates):
                        if matches(candidate, last) and candidate.available and last.get("options") != candidate.options:
                            try:
                                candidates[index] = await self.backend.prepare(candidate, {**candidate.options, **(last.get("options") or {})}, prompt)
                            except ValueError:
                                # An invalid old option is not a usable last choice.
                                last = None
                            break
                else:
                    last = None
                # Show different accounts before a second model on one account.
                from collections import defaultdict
                from itertools import zip_longest
                groups = defaultdict(list)
                for candidate in sorted(candidates, key=lambda c: not c.available):
                    groups[(candidate.source, candidate.account, candidate.worker)].append(candidate)
                candidates = [c for row in zip_longest(*groups.values()) for c in row if c is not None]
                if last:
                    candidates.sort(key=lambda c: not matches(c, last))
                views = [c.view() for c in candidates[:4]]
                if last and views and matches(candidates[0], last):
                    views[0]["label"] = "Same as last time"
                return result("choose", kind=kind, options=views)
            candidate = candidates[0]
            if not candidate.available or (candidate.source in ("chatgpt", "supergrok") and not candidate.verified):
                return result("refused", price=candidate.price, reason="unavailable", error=candidate.reason or "Subscription combination is not verified.")
            supplied = args.get("options", {})
            if not isinstance(supplied, dict):
                return result("refused", error="options must be an object.")
            options = {**candidate.options, **(choice.get("options") or {}), **supplied}
            candidate = await self.backend.prepare(candidate, options, prompt)
            total = price_total(candidate.price)
            if total is None:
                return result("refused", price=candidate.price, reason="price_unavailable",
                              error="A reliable price is unavailable; generation was not submitted.", choice=candidate.choice())
            fingerprint = hashlib.sha256(json.dumps(
                [kind, prompt, context.session_id, candidate.choice(), candidate.price],
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            expensive = max(total, Decimal(str(candidate.price["amount"]))) > CONFIRM_ABOVE_USD
            if expensive:
                token = args.get("confirmation_token")
                approved = False
                if args.get("confirmed") is True and isinstance(token, str):
                    approved = (await self.confirmations.consume(token, fingerprint) if self.confirmations
                                else self._confirmations.pop(token, None) == fingerprint)
                if not approved and context.unattended:
                    # Nobody can approve here: never hand back a usable receipt.
                    return result("skipped", price=candidate.price, reason="needs_confirmation", kind=kind,
                                  choice=candidate.choice(), threshold_usd=str(CONFIRM_ABOVE_USD),
                                  message="Generation skipped: its price is above the confirmation threshold and this run is unattended.")
                if not approved:
                    if len(self._confirmations) >= 256:
                        self._confirmations.pop(next(iter(self._confirmations)))
                    token = await self.confirmations.issue(fingerprint) if self.confirmations else secrets.token_urlsafe(24)
                    if not self.confirmations:
                        self._confirmations[token] = fingerprint
                    return result("needs-confirmation", price=candidate.price, kind=kind,
                                  choice=candidate.choice(), threshold_usd=str(CONFIRM_ABOVE_USD),
                                  confirmation_token=token,
                                  retry={"source": candidate.account, "model": candidate.model, "worker": candidate.worker,
                                         "options": candidate.options, "confirmed": True, "confirmation_token": token})
            try:
                reserved = await self.backend.reserve(candidate, context)
            except ValueError as exc:
                # The cap could not be checked; that is never a reason to submit elsewhere.
                return result("refused", price=candidate.price, reason="budget_unavailable", error=str(exc))
            if not reserved:
                return result("refused", price=candidate.price, reason="daily_cap", error="The daily spending cap would be exceeded.")
            output = await self.backend.execute(candidate, prompt, context)
            if not isinstance(output, dict) or not output.get("success"):
                output = no_retry(output)
            reported = output.get("price")
            price = candidate.price
            if (candidate.source == "supergrok" and kind == "video" and isinstance(reported, dict)
                    and reported.get("status") == "reported"
                    and price_total({**reported, "status": "estimated"}) is not None):
                price = reported
            return result("completed" if output.get("success") else "failed", price=price,
                          choice=candidate.choice(), result=output)
        except ValueError as exc:
            # Backends raise only safe validation messages, not upstream bodies.
            return result("refused", reason="invalid_request", error=str(exc))
