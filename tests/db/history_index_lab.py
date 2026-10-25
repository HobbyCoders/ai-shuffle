"""Single-writer probes used only by the opt-in disposable hand-off lab."""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from psycopg2.extras import Json

from app.db import base


class LiveInserts:
    def __init__(self, offset: int = 0) -> None:
        self.offset = offset
        self.stop = threading.Event()
        self.samples: dict[str, list[float]] = {"events": [], "nodes": []}
        self.stages: dict[str, dict[str, list[float]]] = {
            kind: {stage: [] for stage in ("acquire", "execute", "commit")}
            for kind in self.samples
        }
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self.run)

    def run(self) -> None:
        try:
            i = self.offset
            while not self.stop.is_set():
                for kind in self.samples:
                    started = time.monotonic()
                    with base.get_db() as conn:
                        acquired = time.monotonic()
                        cur = conn.cursor()
                        text = "live-latency-probe " + str(i) + " abcdef" * 450
                        if kind == "events":
                            cur.execute("INSERT INTO session_events "
                                        "(id,session_id,seq,event_type,payload) "
                                        "VALUES (%s,'history-live-lab',%s,'message.user',%s)",
                                        (f"history-live-lab-{i}", i, Json({"content": text})))
                        else:
                            cur.execute("INSERT INTO fluid_session_nodes "
                                        "(session_id,node_id,ts,message) VALUES "
                                        "('history-live-lab',%s,0,%s)",
                                        (f"history-live-lab-{i}", Json({"role": "user", "content": [
                                            {"kind": "text", "text": text}]})))
                        executed = time.monotonic()
                    committed = time.monotonic()
                    self.samples[kind].append(committed - started)
                    self.stages[kind]["acquire"].append(acquired - started)
                    self.stages[kind]["execute"].append(executed - acquired)
                    self.stages[kind]["commit"].append(committed - executed)
                i += 1
                self.stop.wait(0.5)
        except BaseException as exc:
            self.errors.append(exc)

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> dict[str, Any]:
        self.stop.set()
        self.thread.join(timeout=120)
        assert not self.thread.is_alive(), "live probe did not finish"
        assert not self.errors, self.errors

        def summary(values: list[float]) -> dict[str, float | int]:
            ordered = sorted(values)
            assert ordered, "live probe collected no samples"
            return {"n": len(values), "median": statistics.median(values),
                    "p95": ordered[max(0, (95 * len(values) + 99) // 100 - 1)],
                    "max": max(values)}

        return {name: {**summary(values), "stages": {
            stage: summary(samples) for stage, samples in self.stages[name].items()
        }, "slowest": [{"sample": i, "total": values[i], **{
            stage: samples[i] for stage, samples in self.stages[name].items()
        }} for i in sorted(range(len(values)), key=values.__getitem__, reverse=True)[:5]]}
            for name, values in self.samples.items()}


def wal_snapshot() -> dict[str, Any]:
    """Cluster-wide diagnostics; only call on the opt-in disposable lab."""
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_stat_clear_snapshot()")
        cur.execute("SELECT pg_current_wal_insert_lsn() AS lsn, wal_records, wal_fpi, "
                    "wal_bytes, wal_buffers_full FROM pg_stat_wal")
        result = dict(cur.fetchone())
        cur.execute("SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time, "
                    "checkpoint_sync_time, buffers_checkpoint FROM pg_stat_bgwriter")
        result.update(cur.fetchone())
        return result


def wal_summary(start: dict[str, Any], phase: str) -> dict[str, Any]:
    """Keep LSN totals and sampled counters distinct; optionally decode retained WAL."""
    end = wal_snapshot()
    result = {key: float(end[key] - value) for key, value in start.items() if key != "lsn"}
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_wal_lsn_diff(%s, %s) AS bytes", (end["lsn"], start["lsn"]))
        result["lsn_bytes"] = int(cur.fetchone()["bytes"])
    result.update(start_lsn=start["lsn"], end_lsn=end["lsn"])
    # WAL retention and the output directory are explicitly supplied by the lab
    # launcher, never inferred from a running worker's database configuration.
    wal_dir = os.environ.get("HANDOFF_LAB_WAL_DIR")
    artifacts = os.environ.get("HANDOFF_LAB_ARTIFACTS")
    if wal_dir and artifacts:
        decoded = subprocess.run(
            ["pg_waldump", "--path", wal_dir, "--start", start["lsn"],
             "--end", end["lsn"], "--stats=record"],
            capture_output=True, text=True, check=True, timeout=120,
        )
        output = Path(artifacts)
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{phase}-waldump.txt").write_text(decoded.stdout, encoding="utf-8")
        (output / f"{phase}-wal.json").write_text(json.dumps(result), encoding="utf-8")
    return result
