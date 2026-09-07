"""Fail closed on Android instrumentation output, including exit-zero failures."""
import re
import sys
from pathlib import Path


def validate(text):
    fields = {}
    active = set()
    completed = set()
    totals = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("INSTRUMENTATION_STATUS: "):
            key, separator, value = line[len("INSTRUMENTATION_STATUS: "):].partition("=")
            if separator:
                fields[key.strip()] = value.strip()
        elif line.startswith("INSTRUMENTATION_STATUS_CODE:"):
            code = int(line.split(":", 1)[1])
            if code not in (0, 1):
                raise ValueError(f"Failed, skipped or unknown test status: {code}")
            total = int(fields.get("numtests", "0"))
            identity = (fields.get("class"), fields.get("test"))
            if total <= 0 or not all(identity):
                raise ValueError("Missing test identity or expected count")
            totals.add(total)
            if code == 1:
                if identity in active or identity in completed:
                    raise ValueError("Duplicate test start")
                active.add(identity)
            else:
                if identity not in active:
                    raise ValueError("Test ended without matching start")
                active.remove(identity)
                completed.add(identity)
            fields = {}
    summary = re.findall(r"^OK \((\d+) tests?\)\s*$", text, re.MULTILINE)
    terminal = re.findall(r"^INSTRUMENTATION_CODE:\s*(-?\d+)\s*$", text, re.MULTILINE)
    if (active or not completed or totals != {len(completed)}
            or summary != [str(len(completed))] or terminal != ["-1"]
            or re.search(r"FAILURES!!!|INSTRUMENTATION_FAILED|Process crashed|shortMsg=", text)):
        raise ValueError("Incomplete or unsuccessful instrumentation run")
    return len(completed)


if __name__ == "__main__":
    try:
        count = validate(Path(sys.argv[1]).read_text())
    except (ValueError, OSError, IndexError) as error:
        sys.exit(f"Instrumentation verification failed: {error}")
    print(f"Android platform tests: {count} passed; no failed or skipped tests.")
