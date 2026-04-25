#!/usr/bin/env python3
"""CLI helper for issuing, listing, and revoking API keys.

Talks directly to the SQLite database via the keystore module. This
exists so we have a working key-management story before the proper
admin HTTP endpoints land in Tier 2 Item 6.

Run from the project root:

    # Issue a new key
    python -m scripts.manage_keys issue --user alice --name "Alice's iOS"

    # List all keys (no plaintexts shown - they cannot be recovered)
    python -m scripts.manage_keys list

    # Revoke a key by its ID
    python -m scripts.manage_keys revoke 3

The plaintext shown by `issue` is the only place that key value will
ever appear. Save it immediately. If lost, revoke and create a new one.
"""

import argparse
import asyncio
import json
import os
import sys


def _ensure_settings_loadable() -> None:
    """The Settings class requires API_BEARER_TOKEN to be set. The CLI
    doesn't actually need it, but pydantic-settings validates on import.
    Provide a placeholder if not already in env so the CLI runs cleanly.
    """
    if not os.environ.get("API_BEARER_TOKEN"):
        os.environ["API_BEARER_TOKEN"] = "cli-placeholder-not-used"


async def _issue(user_id: str, name: str) -> None:
    from app.keys import get_keystore

    issued = await get_keystore().create(user_id=user_id, name=name)
    print(
        json.dumps(
            {
                "key_id": issued.key_id,
                "user_id": issued.user_id,
                "name": issued.name,
                "created_at": issued.created_at,
                "api_key": issued.plaintext,
                "warning": (
                    "This key will not be shown again. Save it now. "
                    "If lost, revoke and re-issue."
                ),
            },
            indent=2,
        )
    )


async def _list() -> None:
    from app.keys import get_keystore

    keys = await get_keystore().list()
    print(json.dumps(keys, indent=2))


async def _revoke(key_id: int) -> None:
    from app.keys import get_keystore

    revoked = await get_keystore().revoke(key_id)
    if revoked:
        print(json.dumps({"revoked": True, "key_id": key_id}, indent=2))
    else:
        print(
            json.dumps(
                {"revoked": False, "key_id": key_id, "reason": "not found"}, indent=2
            )
        )
        sys.exit(1)


def main() -> None:
    _ensure_settings_loadable()

    parser = argparse.ArgumentParser(description="Manage API keys.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue", help="Issue a new key")
    p_issue.add_argument("--user", required=True, help="user_id (free-form)")
    p_issue.add_argument(
        "--name", required=True, help='Human label, e.g. "Alice\'s iOS"'
    )

    sub.add_parser("list", help="List all keys (hashes only, no plaintexts)")

    p_revoke = sub.add_parser("revoke", help="Revoke a key by id")
    p_revoke.add_argument("key_id", type=int)

    args = parser.parse_args()

    if args.cmd == "issue":
        asyncio.run(_issue(args.user, args.name))
    elif args.cmd == "list":
        asyncio.run(_list())
    elif args.cmd == "revoke":
        asyncio.run(_revoke(args.key_id))


if __name__ == "__main__":
    main()
