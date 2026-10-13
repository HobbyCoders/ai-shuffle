"""Create a controller pairing grant from a trusted local shell."""

from __future__ import annotations

import argparse
import sys

from app.core import cluster, pairing
from app.core.config import settings
from app.db import base, database


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a one-time AI Shuffle pairing code")
    parser.add_argument(
        "--url",
        help="Reachable base URL to encode (defaults to this install's advertised URL)",
    )
    parser.add_argument(
        "--join",
        metavar="CODE",
        help="Enroll this configured runner with an admin-created one-time code",
    )
    args = parser.parse_args()

    try:
        base.init_pool(min_conn=1, max_conn=2)
        database.init_database()
        if args.join:
            # Registration creates a pending row on populated clusters. The
            # one-time runner grant approves exactly this stable instance id;
            # no database/admin secret is transported in the code.
            cluster.register_self()
            grant = database.consume_grant_for_runner(
                token_hash=pairing.hash_pairing_code(args.join),
                install_id=settings.effective_instance_id,
            )
            if grant is None:
                raise ValueError("runner code is invalid, expired, already used, or revoked")
            print()
            print(f"Runner {settings.effective_instance_id} approved.")
            print("Start AI Shuffle normally to begin accepting work.")
            print()
            return 0
        endpoint = args.url
        if not endpoint:
            endpoint, _ = cluster.advertise_url()
        result = pairing.create_grant(
            endpoint_url=endpoint,
            created_by="local-shell",
        )
    except Exception as exc:  # noqa: BLE001 - CLI must print an actionable error
        print(f"Could not create pairing code: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            base.close_pool()
        except Exception:
            pass

    print()
    print("AI Shuffle device pairing")
    print(f"Code: {result['display_code']}")
    print("Expires in 5 minutes and can be used once.")
    if result["pairing_url"]:
        print(f"Link: {result['pairing_url']}")
        if result["qr_svg"]:
            print()
            print(pairing.terminal_qr(result["pairing_url"]))
    else:
        print("No non-loopback address is configured; enter the code manually in a configured client.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
