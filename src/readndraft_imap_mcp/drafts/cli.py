from __future__ import annotations

import argparse
import asyncio

from readndraft_imap_mcp.admin import AccountFile
from readndraft_imap_mcp.credentials import KeyringCredentialStore
from readndraft_imap_mcp.imap import ImapClient
from readndraft_imap_mcp.platform import current_app_paths

from .store import DraftProvenance, FileDraftStore


def _matches(record: DraftProvenance, accounts, credentials) -> tuple[str, ...]:
    account = accounts.require_enabled(record.account_id)
    secret = asyncio.run(credentials.load_secret(record.account_id))
    with ImapClient(account, secret) as client:
        return client.resolve_draft_uid(record)


def _status(record: DraftProvenance, matches: tuple[str, ...]) -> str:
    if matches == (record.uid,):
        return "ok"
    if not matches:
        return "stale: tracked uid not found"
    if len(matches) == 1:
        return f"stale: replacement uid {matches[0]}"
    return f"ambiguous: {len(matches)} matches"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or repair local draft tracking records")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    repair = commands.add_parser("repair")
    selection = repair.add_mutually_exclusive_group(required=True)
    selection.add_argument("--draft-id")
    selection.add_argument("--all", action="store_true")
    forget = commands.add_parser("forget")
    forget.add_argument("--draft-id", required=True)
    args = parser.parse_args(argv)

    paths = current_app_paths()
    store = FileDraftStore(paths.draft_dir)
    if args.action == "forget":
        removed = store.forget(args.draft_id)
        print("Draft tracking record forgotten." if removed else "Draft tracking record was not found.")
        print("The message remains in the Drafts mailbox; remove it with a mail client if desired.")
        return 0

    records = store.list()
    if args.action == "repair" and not args.all:
        records = tuple(record for record in records if record.draft_id == args.draft_id)
        if not records:
            print("Draft tracking record was not found.")
            return 1
    accounts = AccountFile(paths.accounts_file).registry()
    credentials = KeyringCredentialStore()
    failed = False
    for record in records:
        matches = _matches(record, accounts, credentials)
        status = _status(record, matches)
        if args.action == "list":
            print(
                f"{record.draft_id}\t{record.account_id}\t{record.mailbox}\t"
                f"{record.uid}\t{record.message_id}\t{record.created_at}\t"
                f"{record.updated_at}\t{status}"
            )
            continue
        if matches == (record.uid,):
            print(f"{record.draft_id}: already healthy")
        elif len(matches) == 1:
            repaired = store.update(
                record,
                mailbox=record.mailbox,
                uid_validity=record.uid_validity,
                uid=matches[0],
                message_id=record.message_id,
                attachment_hashes=record.attachment_hashes,
                superseded_uid=record.superseded_uid,
            )
            print(f"{record.draft_id}: {record.uid} -> {repaired.uid}")
        else:
            failed = True
            print(f"{record.draft_id}: refused ({status})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
