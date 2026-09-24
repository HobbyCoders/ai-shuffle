"""Shared plumbing for the experiment runners: import paths, JSONL, SSE fakes."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def ensure_import_paths() -> None:
    """Make ``fluid`` and ``scripts`` importable when a runner is started as a
    plain file (``python scripts/experiments/e1_cache_routing.py``)."""
    for path in (REPO, REPO / "fluid"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    if importlib.util.find_spec("fluid") is None:  # pragma: no cover - broken checkout
        raise SystemExit("cannot import fluid; run from the repo root or set PYTHONPATH=fluid")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row


def sse(events: Iterable[tuple[str, dict[str, Any]]]) -> bytes:
    """Encode ``(event_name, data)`` pairs as a Server-Sent Events body."""
    out = []
    for name, data in events:
        out.append(f"event: {name}\ndata: {json.dumps(data)}\n\n")
    return "".join(out).encode()


def responses_sse(
    *,
    response_id: str,
    model: str,
    usage: dict[str, Any] | None,
    text: str | None = "OK",
    extra_items: Iterable[dict[str, Any]] = (),
) -> bytes:
    """A minimal, well-formed Responses stream: created, optional message,
    optional extra output items, completed with ``usage``."""
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"type": "response.created", "response": {
            "id": response_id, "model": model, "status": "in_progress", "output": []}}),
    ]
    index = 0
    if text is not None:
        item = {"id": f"msg_{response_id}", "type": "message", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text", "text": text}]}
        events += [
            ("response.output_item.added", {"type": "response.output_item.added",
             "output_index": index, "item": {**item, "status": "in_progress", "content": []}}),
            ("response.output_text.delta", {"type": "response.output_text.delta",
             "item_id": item["id"], "content_index": 0, "delta": text}),
            ("response.output_item.done", {"type": "response.output_item.done",
             "output_index": index, "item": item}),
        ]
        index += 1
    for item in extra_items:
        events.append(("response.output_item.done", {"type": "response.output_item.done",
                       "output_index": index, "item": item}))
        index += 1
    events.append(("response.completed", {"type": "response.completed", "response": {
        "id": response_id, "model": model, "status": "completed", "output": [],
        "usage": usage or {}}}))
    return sse(events)
