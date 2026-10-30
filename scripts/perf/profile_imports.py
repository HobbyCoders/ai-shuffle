"""Import timing/module-count rig, isolated from the worker's environment.

Run: python -m scripts.perf.profile_imports --runs 10 --out /tmp/imports.json
No startup, listener or model request; each import uses a fresh interpreter.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_IMPORT = re.compile(r"import time:\s+(\d+)\s*\|\s+(\d+)\s*\|(.*)")


def parse_imports(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = _IMPORT.fullmatch(line)
        if match:
            name = match[3]
            rows.append({"self_us": int(match[1]), "cumulative_us": int(match[2]),
                         "module": name.strip(), "depth": len(name) - len(name.lstrip())})
    return rows


def summarize(values: list[float]) -> dict:
    values = sorted(values)
    return {"n": len(values), "p75": values[math.ceil(len(values) * .75) - 1],
            "min": values[0], "max": values[-1]} if values else {
                "n": 0, "p75": None, "min": None, "max": None}


_PROBE = """import json, sys
exec('import ' + sys.argv[1])
print('@PERF_IMPORT@' + json.dumps({'module_count': len(sys.modules),
      'modules': sorted(sys.modules)}))
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--module", default="app.main")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.runs < 1 or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", args.module):
        parser.error("positive --runs and a dotted Python --module are required")
    from app.preview_run import free_port, preview_environment

    result = {"meta": {"module": args.module, "kind": "lab", "python": sys.version,
                       "setup": "fresh interpreters; isolated env; warm OS caches; no startup"},
              "runs": []}
    with tempfile.TemporaryDirectory(prefix="shuffle-perf-imports-") as directory:
        port = free_port()
        env = preview_environment(directory, port, free_port(exclude=(port,)))
        for number in range(args.runs):
            start = time.perf_counter()
            try:
                process = subprocess.run(
                    [sys.executable, "-X", "importtime", "-c", _PROBE, args.module],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                result["failed"] = "import timed out"
                break
            timing = parse_imports(process.stderr)
            count = next((json.loads(line.removeprefix("@PERF_IMPORT@"))
                          for line in process.stdout.splitlines() if line.startswith("@PERF_IMPORT@")), {})
            row = {"elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                   "exit_code": process.returncode, **count,
                   "target": next((m for m in timing if m["module"] == args.module), None),
                   "top_self": sorted(timing, key=lambda m: m["self_us"], reverse=True)[:30]}
            result["runs"].append(row)
            print(json.dumps({key: value for key, value in row.items()
                              if key not in ("modules", "top_self")}), flush=True)
            if process.returncode:
                # Isolated environment: retain useful errors, never URLs/credentials.
                error = "\n".join(line for line in process.stderr.splitlines()
                                  if not line.startswith("import time:"))[-3000:]
                result["failed"] = re.sub(r"https?://\S+", "[URL REDACTED]", error)
                print(result["failed"], file=sys.stderr, flush=True)
                break
    result["summary"] = {key: summarize([row[key] for row in result["runs"] if key in row])
                         for key in ("elapsed_ms", "module_count")}
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
