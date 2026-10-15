"""Acceptance probe for a DISPOSABLE isolated image, never a live install.

Run as root after normal entrypoint startup with AGENT_ISOLATION=on. Uses the
app's real Bash and Monitor producers, but does not create an owner/chat session.
Prints only pass markers; no descriptor, key, credential or database contents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys


# Executed by each agent shell; it receives no database password.
CHILD = r"""
import json, os, sys
import psycopg2
info = json.loads(sys.argv[1])
for name in ('managed-postgres.json', 'database-mode.json', 'preview-access.key',
             '.credential-keys', '.managed-connection', 'postgres'):
    path = '/data/' + name
    try:
        if name in ('.credential-keys', '.managed-connection', 'postgres'):
            os.listdir(path)
        else:
            with open(path, 'rb') as stream:
                stream.read(1)
    except PermissionError:
        pass
    else:
        raise AssertionError('protected entry readable: ' + name)
print('ALL_SIX_DENIED')
for host, user, expected in ((info['host'], info['user'], 'no password supplied'),
                             (info['socket'], 'shuffle_admin', 'Permission denied')):
    try:
        connection = psycopg2.connect(host=host, port=info['port'], user=user,
            dbname=info['database'], connect_timeout=3, passfile='/dev/null', sslmode='disable')
    except psycopg2.OperationalError as error:
        # Reject connection-refused/timeouts/missing sockets as false passes.
        assert expected in str(error), 'unexpected database failure (not an isolation pass)'
    else:
        connection.close()
        raise AssertionError('agent authenticated to the application database')
print('TCP_AUTH_AND_ADMIN_SOCKET_DENIED')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable', action='store_true', required=True)
    parser.add_argument('--agent-uid', type=int, default=99)
    parser.add_argument('--agent-gid', type=int, default=100)
    args = parser.parse_args()
    assert os.geteuid() == 0, 'run only as root in the disposable test container'
    assert os.environ.get('AGENT_ISOLATION') == 'on'
    sys.path.insert(0, '/app')
    import psycopg2
    from app.core import private_storage, process_ledger
    from app.mcp import builtin_tools
    from fluid.tools.builtins.bash import handler
    from fluid.tools.registry import ToolCtx

    descriptor = Path('/data/managed-postgres.json')
    service = descriptor.stat()
    assert service.st_uid not in (0, args.agent_uid)
    connection = json.loads(descriptor.read_text())
    # A healthy authenticated control: errors here must never count as denials.
    options = {key: connection[key] for key in ('host', 'port', 'user', 'password')}
    options['dbname'] = connection['database']
    options['connect_timeout'] = 3
    with psycopg2.connect(**options) as control:
        with control.cursor() as cursor:
            cursor.execute('SELECT 1')
            assert cursor.fetchone() == (1,)
    del options
    info = {key: connection[key] for key in ('host', 'port', 'user', 'database')}
    del connection
    info['socket'] = Path('/data/postgres/data/postmaster.pid').read_text().splitlines()[4]
    assert (Path(info['socket']) / ('.s.PGSQL.' + str(info['port']))).is_socket()
    process_ledger.record_process = lambda *a, **kw: False
    os.environ.update(SHUFFLE_AGENT_ISOLATION='on', SHUFFLE_AGENT_ROOTS='/workspace:/tmp')
    os.environ['LC_ALL'] = 'C'
    os.setgroups([])
    os.setresgid(args.agent_gid, args.agent_gid, service.st_gid)
    os.setresuid(args.agent_uid, args.agent_uid, service.st_uid)
    # Exercise the service file consumer after taking the app's saved-id shape.
    with private_storage.service_files():
        with descriptor.open('rb') as stream:
            assert stream.read(1)
    command = shlex.join([sys.executable, '-c', CHILD, json.dumps(info)])

    async def run():
        result = await handler({'command': command}, ToolCtx(cwd='/workspace'))
        assert not result.is_error, 'Bash probe failed'
        assert ('ALL_SIX_DENIED' in result.text
                and 'TCP_AUTH_AND_ADMIN_SOCKET_DENIED' in result.text), (
                    'Bash isolation probe failed (inspect the disposable fixture)')
        task = await builtin_tools._spawn_background(command, '/workspace', None)
        try:
            await asyncio.wait_for(task._reader_task, 30)
            output = '\n'.join(task.lines)
            assert task.process.returncode == 0
            assert 'ALL_SIX_DENIED' in output and 'TCP_AUTH_AND_ADMIN_SOCKET_DENIED' in output
        finally:
            builtin_tools._background_tasks.pop(task.task_id, None)
        print('BASH_MONITOR_FILES_DATABASE_DENIED_SERVICE_READ_OK', flush=True)

    asyncio.run(run())


if __name__ == '__main__':
    main()
