"""Export the contract models to JSON Schema for TS codegen.

`main()` writes deterministic schema files under app/contract/schema/. The
frontend regenerates its TS types from those files (npm run contract:gen).
Run: `python -m app.contract.export`
"""

import json
from pathlib import Path

from pydantic import TypeAdapter

from app.contract.commands import Command
from app.contract.events import SessionEvent

SCHEMA_DIR = Path(__file__).parent / "schema"

_DUMP_KWARGS = {"indent": 2, "sort_keys": True}


def build_events_schema() -> dict:
    schema = TypeAdapter(SessionEvent).json_schema()
    schema["title"] = "SessionEvent"
    return schema


def build_commands_schema() -> dict:
    schema = TypeAdapter(Command).json_schema()
    schema["title"] = "Command"
    return schema


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    (SCHEMA_DIR / "events.schema.json").write_text(
        json.dumps(build_events_schema(), **_DUMP_KWARGS) + "\n", encoding="utf-8"
    )
    (SCHEMA_DIR / "commands.schema.json").write_text(
        json.dumps(build_commands_schema(), **_DUMP_KWARGS) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
