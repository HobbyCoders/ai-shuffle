"""Single-writer probes used only by the opt-in disposable hand-off lab."""
from __future__ import annotations

import statistics
import threading
import time
from typing import Any

from psycopg2.extras import Json
from app.db import base


class LiveInserts:
    def __init__(self, offset: int = 0) -> None:
        self.offset = offset
        self.stop = threading.Event()
        self.samples: dict[str, list[float]] = {"events": [], "nodes": []}
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self.run)

    def run(self) -> None:
        try:
            i = self.offset
            while not self.stop.is_set():
                for kind in self.samples:
                    started = time.monotonic()
                    with base.get_db() as conn:
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
                    self.samples[kind].append(time.monotonic() - started)
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
        result = {}
        for name, values in self.samples.items():
            ordered = sorted(values)
            result[name] = {"n": len(values), "median": statistics.median(values),
                            "p95": ordered[max(0, (95 * len(values) + 99) // 100 - 1)],
                            "max": max(values)}
        return result
