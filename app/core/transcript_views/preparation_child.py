"""Lightweight private CPU entrypoint; stdin EOF owns lifetime on Linux/Windows.

The reader is also the watchdog: it observes parent death even while the main
thread is computing/waiting. No multiprocessing helper or application startup.
"""
from __future__ import annotations

import contextlib
import os
import pickle
import queue
import struct
import sys
import threading
from pathlib import Path

# Python -I drops cwd/PYTHONPATH; only this installation's code may be imported.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
MAX_PACKET_BYTES = 32 * 1024 * 1024


def main() -> None:
    source, destination = sys.stdin.buffer, sys.stdout.buffer
    pending: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            while True:
                header = source.read(4)
                if len(header) != 4:
                    os._exit(0)
                size, = struct.unpack("!I", header)
                if not 0 < size <= MAX_PACKET_BYTES:
                    os._exit(70)
                data = source.read(size)
                if len(data) != size:
                    os._exit(0)
                pending.put(data)
        except BaseException:
            os._exit(70)

    threading.Thread(target=read, name="projection-parent-stdin", daemon=True).start()
    with open(os.devnull, "w") as silent:
        while True:
            data = pending.get()
            # No import/helper diagnostics or transcript text on the protocol.
            with contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
                sequence, call, args, kwargs, environment = pickle.loads(data)
                for name, value in environment.items():
                    os.environ[name] = value
                try:
                    response = (sequence, True, call(*args, **kwargs))
                except Exception as error:
                    response = (sequence, False, error)
                packet = pickle.dumps(response, protocol=pickle.HIGHEST_PROTOCOL)
            if len(packet) > MAX_PACKET_BYTES:
                os._exit(70)
            destination.write(struct.pack("!I", len(packet)) + packet)
            destination.flush()


if __name__ == "__main__":
    main()
