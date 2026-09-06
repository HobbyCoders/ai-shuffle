"""Local credential custody/recovery. Passwords are read from the terminal only."""
import argparse
import getpass
import json

from app.core import credential_keys, encryption
from app.db import database


def inspect_status():
    """Read-only verification, including every encrypted store."""
    with database.get_db() as conn:
        metadata = credential_keys._read_state(conn.cursor())
        values = credential_keys._encrypted_values(conn.cursor())
        if metadata is None or metadata['wrapped_dek'] is None:
            return {'ready': False, 'state': 'legacy_unlock_required' if values else 'uninitialized'}
        key = credential_keys._unwrap(metadata, credential_keys._wrapping_key(metadata['database_id']))
        credential_keys._verify(key, values)
        return {'ready': True, 'state': 'ready', 'database_id': metadata['database_id']}


def execute(args):
    if args.command == 'status':
        return inspect_status()
    if args.command in ('adopt', 'import'):
        from scripts.rekey import _server_appears_up
        if _server_appears_up():
            raise RuntimeError('Stop the app before local credential recovery')
    if args.command == 'adopt':
        state = inspect_status()
        password = getpass.getpass('Existing legacy encryption password: ') if state['state'] == 'legacy_unlock_required' else None
        if not encryption.initialize_server_key(database, legacy_password=password):
            raise credential_keys.CredentialKeyError(encryption.credential_key_status()['state'])
    elif args.command == 'export':
        password = getpass.getpass('Recovery passphrase (at least 12 characters): ')
        if password != getpass.getpass('Confirm recovery passphrase: '):
            raise ValueError('Recovery passphrases do not match')
        credential_keys.export_recovery(database, args.file, password)
    elif args.command == 'import':
        credential_keys.import_recovery(database, args.file, getpass.getpass('Recovery passphrase: '))
    return inspect_status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Verify custody and all stored ciphertext without changing state')
    commands.add_parser('adopt', help='Initialize an empty database or adopt the existing legacy key; app must be stopped')
    for command in ('export', 'import'):
        child = commands.add_parser(command, help='Encrypted portable recovery ' + command)
        child.add_argument('file', help='Local recovery file (export never overwrites)')
    args = parser.parse_args(argv)
    from app.db.base import close_pool, init_pool
    try:
        init_pool(min_conn=1, max_conn=2)
        result = execute(args)
        print(json.dumps(result))
        return 0 if result['ready'] else 1
    except credential_keys.CredentialKeyError as error:
        print(json.dumps({'ready': False, 'state': error.state}))
        return 1
    except Exception:
        # Never print database exceptions, credentials, or decoded recovery data.
        print(json.dumps({'ready': False, 'state': 'operation_failed',
                          'action': 'Check stopped app, configured database, file access and recovery passphrase'}))
        return 1
    finally:
        close_pool()


if __name__ == '__main__':
    raise SystemExit(main())
