#!/usr/bin/env python3
"""Read-only Linux process/descendant resource sampler; JSONL, no command lines.

CPU is percentage of one core. Network counters are namespace-wide, NOT per
process (children normally share them). PIDs include start ticks to detect reuse.
No environment, socket peers, paths, or transcript contents are collected.
"""
import argparse
import json
import os
from pathlib import Path
import time


def process(pid):
    base = Path('/proc') / str(pid)
    stat = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    status = {}
    for line in (base / 'status').read_text().splitlines():
        key, _, value = line.partition(':')
        if key in ('VmRSS', 'RssAnon', 'RssFile', 'RssShmem', 'Threads'):
            status[key] = int(value.split()[0])
    status.update(pid=pid, ppid=int(stat[1]), start_ticks=int(stat[19]),
                  cpu_ticks=int(stat[11]) + int(stat[12]))
    try:
        status['fds'] = len(list((base / 'fd').iterdir()))
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        status['fds'] = None
    try:
        for line in (base / 'smaps_rollup').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('Pss', 'Private_Dirty', 'Anonymous', 'Swap'):
                status['smaps_' + key] = int(value.split()[0])
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        status['smaps_unavailable'] = True
    return status


def sample(pid):
    processes = {}
    for path in Path('/proc').iterdir():
        if path.name.isdecimal():
            try:
                stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                processes[int(path.name)] = int(stat[1])
            except (OSError, ValueError):
                continue
    descendants = {pid}
    while True:
        found = {p for p, parent in processes.items() if parent in descendants}
        if found <= descendants:
            break
        descendants |= found
    rows = []
    for child in sorted(descendants):
        try:
            rows.append(process(child))
        except (OSError, ValueError):
            continue
    network = {}
    for line in (Path('/proc') / str(pid) / 'net/dev').read_text().splitlines()[2:]:
        name, values = line.split(':')
        fields = values.split()
        network[name.strip()] = dict(rx_bytes=int(fields[0]), tx_bytes=int(fields[8]))
    return dict(time=time.time(), monotonic=time.monotonic(), processes=rows,
                network_scope='network namespace; not process-attributed', network=network)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--interval', type=float, default=10)
    args = parser.parse_args()
    if args.seconds < 0 or args.interval <= 0:
        parser.error('seconds must be nonnegative and interval positive')
    start = time.monotonic()
    previous = {}
    identity = None
    previous_time = start
    while True:
        row = sample(args.pid)
        parent = next((p for p in row['processes'] if p['pid'] == args.pid), None)
        if parent is None:
            raise SystemExit('Target process exited or its status is unavailable')
        if identity is not None and parent['start_ticks'] != identity:
            raise SystemExit('PID changed; refusing to mix process lifetimes')
        identity = parent['start_ticks']
        elapsed = row['monotonic'] - previous_time
        for p in row['processes']:
            key = (p['pid'], p['start_ticks'])
            p['cpu_percent'] = ((p['cpu_ticks'] - previous[key]) / os.sysconf('SC_CLK_TCK')
                                / elapsed * 100) if key in previous else None
        previous = {(p['pid'], p['start_ticks']): p['cpu_ticks'] for p in row['processes']}
        previous_time = row['monotonic']
        print(json.dumps(row), flush=True)
        remaining = args.seconds - (time.monotonic() - start)
        if remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))


if __name__ == '__main__':
    main()
