"""Owner password maintenance from a shell inside the container.

Running a command inside the container already requires control of the host
(``docker exec``, or Unraid's console), so this is the password-reset path:

    docker exec -it -u appuser ai-shuffle python -m app.owner_password status
    docker exec -it -u appuser ai-shuffle python -m app.owner_password set
    docker exec -it -u appuser ai-shuffle python -m app.owner_password reset
    docker exec -it -u appuser ai-shuffle python -m app.owner_password trusted-lan off

``set`` asks for a new password twice (or reads one line from stdin with
``--stdin``); ``reset`` removes the password so the next browser on your
network is asked to create one. Both sign every browser out. Paired devices
keep working.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.core import owner_auth
from app.db import base, database


def _read_new_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New owner password: ")
    second = getpass.getpass("Repeat it: ")
    if first != second:
        raise ValueError("The two passwords are different.")
    return first


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.owner_password",
                                     description="Show, set or reset the AI Shuffle owner password")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show whether a password is set and whether the LAN is trusted")
    set_cmd = commands.add_parser("set", help="Set a new owner password and sign every browser out")
    set_cmd.add_argument("--stdin", action="store_true", help="Read the password from standard input")
    commands.add_parser("reset", help="Remove the owner password; the next browser on your network creates one")
    lan = commands.add_parser("trusted-lan", help="Turn 'trusted LAN, no login' on or off")
    lan.add_argument("state", choices=("on", "off"))
    args = parser.parse_args(argv)

    try:
        base.init_pool(min_conn=1, max_conn=2)
        database.init_database()
        if args.command == "status":
            print("Owner password:", "set" if owner_auth.password_set(exact=True) else "not set")
            print("Trusted LAN (no login):", "on" if owner_auth.trusted_lan() else "off")
        elif args.command == "set":
            owner_auth.set_password(_read_new_password(args.stdin), source="command line")
            print("Owner password changed. Every browser was signed out; sign in again with the new password.")
        elif args.command == "reset":
            owner_auth.clear_password()
            print("Owner password removed and every browser signed out.")
            print("Open AI Shuffle from your network now and create a new one; until then any browser")
            print("that can reach it can create it.")
        else:
            owner_auth.set_trusted_lan(args.state == "on")
            print("Trusted LAN (no login):", args.state)
    except ValueError as exc:
        print(f"Not changed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a CLI prints an actionable error
        print(f"Could not reach the AI Shuffle database: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            base.close_pool()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
