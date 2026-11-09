"""Owner-only management of long-term Remember/Recall facts (not AGENTS.md)."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.api.auth import require_admin
from app.core.fluid_memory import store_for_owner
from app.db.async_database import run_sync
from app.db.projects import get_projects_by_ids
from fluid.codemode import facts
from fluid.codemode.skills import MAX_MEMORY_CHARS
from fluid.codemode.transactions import file_transaction

logger = logging.getLogger(__name__)
_UNAVAILABLE = "Memories are unavailable on this worker. Please try again later."
_NOT_FOUND = "This memory could not be found on this worker."
_CHANGED = "This memory changed since you opened it. Refresh it and try again."


class MemoryRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def plain_validation(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Do not echo rejected input (which might itself be a secret).
                raise HTTPException(422, "Provide memory text, a when line or null, and the version of the memory you opened.") from None

        return plain_validation


router = APIRouter(prefix="/api/v1/memories", tags=["memories"], route_class=MemoryRoute,
                   dependencies=[Depends(require_admin)])


_VERSION_CHARS = 16


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr
    when: StrictStr | None
    expected_revision: int = Field(ge=1, strict=True)
    expected_metadata_version: StrictStr = Field(min_length=1, max_length=_VERSION_CHARS)


def _visible(entry, store_id):
    # memory.coexist.* rows are internal pair answers, not saved memories.
    return (not str(entry.get("key") or "").startswith("memory.coexist.")
            and facts.visible_on(entry, store_id))


def _metadata_version(entry):
    # The store's revision is too coarse for an owner's open form. When-line and
    # always edits replicate by their own stamps and leave it alone; a replica
    # conflict can keep it equal; and a supersession link needs no bump. A save
    # replaces or clears all of these, so a form must have seen them first.
    # Usage counters are left out: a Recall must not make a form stale.
    state = [[facts.metadata_stamp(entry, field) for field in facts.METADATA_FIELDS],
             sorted((entry.get("clock") or {}).items()), bool(entry.get("conflicted")),
             sorted(entry.get("alternatives") or []), entry.get("superseded_by"),
             bool(entry.get("supersession_confirmed"))]
    return facts.fingerprint(repr(state))[:_VERSION_CHARS]


def _require_metadata_version(entry, expected):
    if _metadata_version(entry) != expected:
        raise HTTPException(409, _CHANGED)


def _project_names(entries):
    ids = {entry.get("scope", "global")[8:] for entry in entries
           if entry.get("scope", "global").startswith("project:")}
    return {pid: row["name"] for pid, row in get_projects_by_ids(ids).items()}


def _item(entry, status, project_names):
    scope = entry.get("scope", "global")
    protected = bool(entry.get("protected"))
    return {
        "id": entry["id"],
        "revision": entry.get("revision", 1),
        "metadata_version": _metadata_version(entry),
        "text": entry["text"],
        "when": facts.written_when(entry),
        "status": status,
        "tier": "user" if protected else "agent" if entry.get("durable") else "learned",
        "source": entry.get("provenance", "session"),
        "scope": scope,
        "scope_label": ("All projects" if scope == "global" else
                        project_names.get(scope[8:], "Other project") if scope.startswith("project:")
                        else "Other project"),
        "always": facts.always_applies(entry),
        "needs_review": bool(entry.get("conflicted") or (
            entry.get("superseded_by") and not facts.is_superseded(entry))),
        "this_worker_only": bool(entry.get("worker")),
        "created_at": entry.get("created_at") or None,
        "updated_at": entry.get("updated_at") or None,
        "last_recalled_at": entry.get("last_recalled_at") or None,
        "expires_at": None if protected else entry.get("expires_at") or None,
        "uses": int(entry.get("uses") or 0),
    }


def _list():
    store = store_for_owner("local", initialize=False)
    doc = store._facts.snapshot(read_only=True)
    rows = [(entry, status) for bucket, status in (("entries", "active"), ("archived", "archived"))
            for entry in doc[bucket] if _visible(entry, doc["store_id"])]
    names = _project_names([entry for entry, _ in rows])
    items = [_item(entry, status, names) for entry, status in rows]
    items.sort(key=lambda item: item["updated_at"] or 0, reverse=True)
    return {"items": items, "limits": {"text_chars": MAX_MEMORY_CHARS, "when_chars": facts.MAX_WHEN_CHARS}}


def _find(store, memory_id):
    doc = store._facts.snapshot(read_only=True)
    for entry in doc["entries"] + doc["archived"]:
        if entry["id"] == memory_id and _visible(entry, doc["store_id"]):
            return entry
    raise HTTPException(404, _NOT_FOUND)


def _update(memory_id, body):
    store = store_for_owner("local")
    # Check visibility/control keys and write in one reentrant transaction, so
    # a concurrent replica cannot turn an ordinary fact into a hidden target.
    with file_transaction(store._facts.lock):
        _require_metadata_version(_find(store, memory_id), body.expected_metadata_version)
        saved = store.save_memory(body.text, memory_id=memory_id,
                                  expected_revision=body.expected_revision,
                                  protected=True, provenance="user", when=body.when)
        # The stored row, not the returned MemoryEntry, carries the metadata stamps.
        entry = _find(store, saved.id)
        return _item(entry, "active", _project_names([entry]))


def _delete(memory_id, expected_revision, expected_metadata_version):
    store = store_for_owner("local")
    with file_transaction(store._facts.lock):
        _require_metadata_version(_find(store, memory_id), expected_metadata_version)
        if not store.delete_memory(memory_id, explicit=True, expected_revision=expected_revision):
            raise HTTPException(404, _NOT_FOUND)


def _store_error(exc):
    message = str(exc)
    if message.startswith("Unknown memory ID;"):
        return HTTPException(404, _NOT_FOUND)
    if message.startswith("Memory changed since it was read;"):
        return HTTPException(409, _CHANGED)
    if message in (
        "This fact matches a deletion barrier; an explicit restore request is required",
        "This fact was forgotten; an explicit restore request is required",
    ):
        return HTTPException(409, "This memory matches a previously deleted fact and cannot be saved here.")
    if message == facts.EXPLICIT_FULL_MESSAGE.format(cap=facts.MAX_EXPLICIT):
        return HTTPException(409, "Saved memory is full. Delete a user-saved memory before saving another.")
    if message.startswith("ALWAYS memories in this scope share "):
        return HTTPException(409, "Memories applied to every chat are full. Shorten this memory and try again.")
    validation = {
        f"Memory must contain 1–{MAX_MEMORY_CHARS} characters": f"Memory text must contain 1–{MAX_MEMORY_CHARS} characters.",
        "Memory contains a credential; save its location, not its value": "Memory text looks like a secret. Save its location, not its value.",
        "when contains a credential": "The when line looks like a secret. Save its location, not its value.",
        "when matches an injection pattern": "The when line contains unsafe instructions. Describe when the memory applies instead.",
    }
    if message in validation:
        return HTTPException(422, validation[message])
    if message.startswith(f"when must contain 1–{facts.MAX_WHEN_CHARS} characters:"):
        return HTTPException(422, f"The when line must contain 1–{facts.MAX_WHEN_CHARS} characters.")
    if message.startswith("Memory has too many concurrent writers;"):
        return HTTPException(409, "This memory has conflicting changes. Refresh it and try again.")
    # Never include exception text/tracebacks: a storage decoder or validator
    # may carry a fact's value. Unknown failures are availability errors.
    logger.warning("Memory storage operation failed (%s)", type(exc).__name__)
    return HTTPException(503, _UNAVAILABLE)


async def _run(operation, *args):
    try:
        return await run_sync(operation, *args)
    except HTTPException:
        raise
    except Exception as exc:
        raise _store_error(exc) from None


@router.get("")
async def list_memories():
    return await _run(_list)


@router.put("/{memory_id}")
async def update_memory(memory_id: str, body: MemoryUpdate):
    return await _run(_update, memory_id, body)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, expected_revision: Annotated[int, Query(ge=1)],
                        expected_metadata_version: Annotated[str, Query(min_length=1, max_length=_VERSION_CHARS)]):
    await _run(_delete, memory_id, expected_revision, expected_metadata_version)
    return Response(status_code=204)
