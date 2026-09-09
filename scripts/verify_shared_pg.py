"""Opt-in, no-provider verification of real two-process PostgreSQL synchronization.

Creates only a new audit database, runs production pools/LISTEN/NOTIFY/preference
writes, and drops it after matching its manifest, OID and random ownership comment.
Connection secrets stay in memory; neither source rows nor provider APIs are read.
Run from the repository using its Python environment. No action without --execute.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ipaddress
import socket
from contextlib import closing
import logging
import multiprocessing as mp
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

DATABASE = 'shuffle_audit_01a06e6c_20260907'
ROUNDS = 20


class VerificationError(RuntimeError):
    """Safe, static diagnostics without connection details."""



def configure(source_env, runtime, database):
    raise VerificationError(
        'Shared-database verification is retired: each installation now owns its '
        'embedded database. No source data or database configuration was changed.'
    )


class Socket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, **kwargs):
        pass


async def worker_async(pipe, barrier, source_env, runtime, database, worker_id):
    configure(source_env, runtime, database)
    from app.db import base, settings as preferences
    from app.core.global_events import global_events as hub
    from app.core.event_gateway import get_gateway
    base.init_pool(1, 4)
    gateway = get_gateway()
    wires = []
    gateway.add_message_listener('user.invalidation', wires.append)
    hub.start()
    sockets = [Socket() for _ in range(4)]
    principals = [('api_user', 'alice'), ('api_user', 'alice'),
                  ('api_user', 'bob'), ('admin', 'alice')]
    for client_socket, principal in zip(sockets, principals):
        hub.register(client_socket, *principal)
    try:
        deadline = time.monotonic() + 12
        while not gateway.is_connected():
            if time.monotonic() > deadline:
                raise TimeoutError('Listener did not connect')
            await asyncio.sleep(.02)
        await asyncio.sleep(.02)
        pipe.send({'ready': True, 'pid': os.getpid()})
        while True:
            command = await asyncio.to_thread(pipe.recv)
            action = command['action']
            if action == 'stop':
                return
            if action == 'publish':
                hub.preference_changed('api_user', 'alice', 'probe')
                await asyncio.sleep(0)
                await asyncio.wait_for(hub._queue.join(), 10)
                pipe.send({'published': True})
            elif action == 'snapshot':
                pipe.send({'sockets': [socket.sent for socket in sockets], 'wires': list(wires)})
            elif action == 'replay':
                await asyncio.to_thread(hub._notify, command['envelope'])
                pipe.send({'replayed': True})
            elif action == 'cas':
                await asyncio.to_thread(barrier.wait, 10)
                try:
                    row = await asyncio.to_thread(preferences.set_user_preference,
                        'api_user', 'alice', command['key'], {'worker': worker_id},
                        expected_version=command['version'])
                    pipe.send({'outcome': 'won', 'revision': row['revision'], 'worker': worker_id})
                except preferences.PreferenceConflict:
                    pipe.send({'outcome': 'conflict', 'worker': worker_id})
    finally:
        await hub.shutdown()
        gateway.remove_message_listener('user.invalidation', wires.append)
        await asyncio.to_thread(gateway.stop, 6)
        base.close_pool()


def worker(pipe, barrier, source_env, runtime, database, worker_id):
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(worker_async(pipe, barrier, source_env, runtime, database, worker_id))
    except BaseException as error:
        pipe.send({'error_type': type(error).__name__})
        raise SystemExit(1)
    finally:
        pipe.close()


def receive(pipe):
    if not pipe.poll(20):
        raise TimeoutError('Worker response timeout')
    result = pipe.recv()
    if 'error_type' in result:
        raise VerificationError('Worker failed: ' + result['error_type'])
    return result


def request(pipe, action, **kwargs):
    pipe.send(dict(action=action, **kwargs))
    return receive(pipe)


def wait_snapshots(pipes, predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshots = [request(pipe, 'snapshot') for pipe in pipes]
        if predicate(snapshots):
            return snapshots
        time.sleep(.05)
    raise AssertionError('Cross-worker delivery did not settle')


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def run(args):
    import psycopg2
    from psycopg2 import sql
    runtime = args.runtime.resolve()
    manifest_path = runtime / 'manifest.json'
    if manifest_path.exists():
        raise VerificationError('Existing manifest must not be overwritten')
    if not re.fullmatch(r'shuffle_audit_[a-f0-9]+_\d{8}', args.database):
        raise VerificationError('Invalid audit database name')
    connection = configure(args.source_env.resolve(), runtime, args.database)
    admin = psycopg2.connect(**dict(connection, dbname='postgres'))
    admin.autocommit = True
    processes, pipes = [], []
    manifest = {'database': args.database, 'nonce': uuid4().hex, 'state': 'planned',
                'source_rows_read': 0, 'provider_calls': 0}
    allocated = False
    try:
        with admin.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (args.database,))
            if cursor.fetchone():
                raise VerificationError('Existing database must not be touched')
            runtime.mkdir(parents=True, exist_ok=True)
            save(manifest_path, manifest)
            cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(args.database)))
            allocated = True
            cursor.execute(sql.SQL('COMMENT ON DATABASE {} IS %s').format(sql.Identifier(args.database)),
                           ('ai-shuffle audit ' + manifest['nonce'],))
            cursor.execute('SELECT oid FROM pg_database WHERE datname=%s', (args.database,))
            manifest.update(oid=cursor.fetchone()[0], state='allocated')
            save(manifest_path, manifest)
        versions = {}
        with closing(psycopg2.connect(**connection)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                # Same column types/constraints as schema_postgres.user_preferences.
                cursor.execute('''CREATE TABLE user_preferences (
                    id BIGSERIAL PRIMARY KEY, user_type TEXT NOT NULL,
                    user_id TEXT NOT NULL, key TEXT NOT NULL, value JSONB,
                    revision TEXT, updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_type, user_id, key))''')
                for index in range(ROUNDS):
                    key, revision = f'cas-{index}', str(uuid4())
                    versions[key] = revision
                    cursor.execute('''INSERT INTO user_preferences
                        (user_type,user_id,key,value,revision) VALUES ('api_user','alice',%s,%s,%s)''',
                        (key, json.dumps({'worker': 'seed'}), revision))
        context = mp.get_context('spawn')
        barrier = context.Barrier(3)
        for index in range(2):
            parent, child = context.Pipe()
            process = context.Process(target=worker,
                args=(child, barrier, args.source_env.resolve(), runtime, args.database, index))
            process.start()
            child.close()
            processes.append(process)
            pipes.append(parent)
        started = [receive(pipe) for pipe in pipes]
        assert len({entry['pid'] for entry in started}) == 2
        request(pipes[0], 'publish')
        initial = wait_snapshots(pipes, lambda snaps: all(len(s['wires']) >= 1 for s in snaps))
        # Wait beyond listener callbacks to detect bounce/echo loops.
        time.sleep(.25)
        initial = [request(pipe, 'snapshot') for pipe in pipes]
        expected = [{'type': 'sync_ready'}, {'type': 'preferences_changed', 'keys': ['probe']}]
        for snapshot in initial:
            assert snapshot['sockets'][:2] == [expected, expected]
            assert snapshot['sockets'][2:] == [[{'type': 'sync_ready'}]] * 2
            assert len(snapshot['wires']) == 1
        envelope = initial[0]['wires'][0]
        assert initial[1]['wires'][0] == envelope
        request(pipes[0], 'replay', envelope=envelope)
        replayed = wait_snapshots(pipes, lambda snaps: all(len(s['wires']) >= 2 for s in snaps))
        for snapshot in replayed:
            assert snapshot['sockets'][:2] == [expected, expected]
        outcomes = []
        for key, version in versions.items():
            for pipe in pipes:
                pipe.send({'action': 'cas', 'key': key, 'version': version})
            barrier.wait(10)
            results = [receive(pipe) for pipe in pipes]
            assert sorted(result['outcome'] for result in results) == ['conflict', 'won']
            winner = next(result for result in results if result['outcome'] == 'won')
            assert winner['revision'] != version
            with closing(psycopg2.connect(**connection)) as conn:
                conn.autocommit = True
                with conn.cursor() as cursor:
                    cursor.execute("SELECT value,revision FROM user_preferences WHERE key=%s", (key,))
                    value, revision = cursor.fetchone()
                    assert value == {'worker': winner['worker']} and revision == winner['revision']
            outcomes.append({'key': key, 'winner': winner['worker'], 'conflicts': 1})
        final = wait_snapshots(pipes, lambda snaps: all(len(s['wires']) >= ROUNDS + 2 for s in snaps))
        time.sleep(.25)
        final = [request(pipe, 'snapshot') for pipe in pipes]
        for snapshot in final:
            assert len(snapshot['wires']) == ROUNDS + 2  # two probe deliveries + one per successful write
            for socket in snapshot['sockets'][:2]:
                assert len(socket) == ROUNDS + 2  # ready + probe + one per winner
                assert {e['keys'][0] for e in socket[1:]} == {'probe', *versions}
            assert snapshot['sockets'][2:] == [[{'type': 'sync_ready'}]] * 2
        evidence = {'database': args.database, 'worker_pids': [entry['pid'] for entry in started],
                    'cross_process_owner_scoping': 'passed', 'same_name_other_role_excluded': True,
                    'duplicate_delivery_suppressed': True, 'publish_echoes': 0,
                    'cas_rounds': ROUNDS, 'successful_writes': ROUNDS, 'conflicts': ROUNDS,
                    'invalidation_per_successful_write': 1, 'source_rows_read': 0,
                    'provider_calls': 0, 'outcomes': outcomes}
        save(runtime / 'results.json', evidence)
        manifest['verification'] = 'passed'
    finally:
        for pipe, process in zip(pipes, processes):
            if process.is_alive():
                try:
                    pipe.send({'action': 'stop'})
                except (BrokenPipeError, EOFError):
                    pass
        for process in processes:
            process.join(12)
            if process.is_alive():
                process.terminate()  # only the child process created above
                process.join(5)
        for pipe in pipes:
            pipe.close()
        if allocated:
            recorded = json.loads(manifest_path.read_text(encoding='utf-8'))
            assert recorded['database'] == args.database and recorded['nonce'] == manifest['nonce']
            with admin.cursor() as cursor:
                cursor.execute("SELECT oid,shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s", (args.database,))
                oid, comment = cursor.fetchone()
                assert oid == recorded['oid']
                assert comment == 'ai-shuffle audit ' + recorded['nonce']
                cursor.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(args.database)))
                cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (args.database,))
                assert cursor.fetchone() is None
            manifest['state'] = 'cleaned'
            save(manifest_path, manifest)
        admin.close()
    print(json.dumps({key: value for key, value in evidence.items() if key != 'outcomes'} | {'cleanup': manifest['state']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--source-env', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--database', default=DATABASE)
    args = parser.parse_args()
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'fluid')]
    logging.disable(logging.CRITICAL)
    if not args.execute:
        print(json.dumps({'planned_database': args.database, 'executed': False}))
    else:
        try:
            run(args)
        except BaseException as error:
            # Connection exceptions can contain host/user/password fragments.
            print(json.dumps({'verification': 'failed', 'error_type': type(error).__name__, 'reason': str(error) if isinstance(error, VerificationError) else 'See the manifest for allocation status'}))
            raise SystemExit(1)
