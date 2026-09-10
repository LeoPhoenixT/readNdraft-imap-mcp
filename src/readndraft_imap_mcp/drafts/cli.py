from __future__ import annotations

import argparse
import asyncio
import secrets

from readndraft_imap_mcp.admin import AccountFile
from readndraft_imap_mcp.credentials import KeyringCredentialStore
from readndraft_imap_mcp.imap import ImapClient
from readndraft_imap_mcp.platform import current_app_paths

from .store import (
    DraftProvenance,
    DraftRecoveryRequiredError,
    FileDraftStore,
)


def _matches(record: DraftProvenance, accounts, credentials) -> tuple[str, ...]:
    account = accounts.require_enabled(record.account_id)
    secret = asyncio.run(credentials.load_secret(record.account_id))
    with ImapClient(account, secret) as client:
        return client.resolve_draft_uid(record)


def _operation_matches(
    record: DraftProvenance, operation: dict, accounts, credentials
) -> tuple[str, ...]:
    account = accounts.require_enabled(record.account_id)
    secret = asyncio.run(credentials.load_secret(record.account_id))
    with ImapClient(account, secret) as client:
        return client.resolve_draft_operation(record, operation["operation_id"])


def _recover_operation(
    store: FileDraftStore,
    record: DraftProvenance,
    operation: dict,
    matches: tuple[str, ...],
    accounts,
    credentials,
) -> DraftProvenance:
    """Adopt one verified replacement and remove only its verified predecessor."""
    hashes = store.validate_operation(record, operation)
    if len(matches) != 1 or operation.get("new_uid", matches[0]) != matches[0]:
        raise DraftRecoveryRequiredError("draft update recovery is required")
    replacement = store.update(
        record,
        mailbox=operation["mailbox"],
        uid_validity=operation["uid_validity"],
        uid=matches[0],
        message_id=record.message_id,
        attachment_hashes=hashes,
        superseded_uid=operation["old_uid"],
    )
    account = accounts.require_enabled(record.account_id)
    secret = asyncio.run(credentials.load_secret(record.account_id))
    with ImapClient(account, secret) as client:
        client.expunge_superseded_draft(replacement, operation["old_uid"])
    repaired = store.update(
        replacement,
        mailbox=replacement.mailbox,
        uid_validity=replacement.uid_validity,
        uid=replacement.uid,
        message_id=replacement.message_id,
        attachment_hashes=hashes,
        superseded_uid=None,
    )
    store.clear_operation(record.draft_id)
    return repaired


def _status(record: DraftProvenance, matches: tuple[str, ...]) -> str:
    if matches == (record.uid,):
        return "ok"
    if not matches:
        return "stale: tracked uid not found"
    if len(matches) == 1:
        return f"stale: replacement uid {matches[0]}"
    return f"ambiguous: {len(matches)} matches"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or repair local draft tracking records",
        epilog=(
            "For an absent operation marker, first run `drafts list` and inspect the "
            "Drafts mailbox. To clear only that local journal, deliberately run "
            "`drafts repair --draft-id ID --clear-operation OPERATION_ID`. This never "
            "deletes a server message and a later update still needs its own confirmation."
        ),
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    repair = commands.add_parser("repair")
    selection = repair.add_mutually_exclusive_group(required=True)
    selection.add_argument("--draft-id")
    selection.add_argument("--all", action="store_true")
    repair.add_argument(
        "--clear-operation",
        metavar="OPERATION_ID",
        help="clear one verified absent operation journal; requires --draft-id",
    )
    forget = commands.add_parser("forget")
    forget.add_argument("--draft-id", required=True)
    args = parser.parse_args(argv)
    if args.action == "repair" and args.clear_operation is not None and args.draft_id is None:
        parser.error("--clear-operation requires --draft-id")

    paths = current_app_paths()
    store = FileDraftStore(paths.draft_dir)
    if args.action == "forget":
        with store.operation_lock(args.draft_id):
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
        with store.operation_lock(record.draft_id):
            record = store.get(record.draft_id, record.account_id)
            try:
                operation = store.get_operation(record.draft_id)
            except DraftRecoveryRequiredError:
                status = "recovery_required: invalid operation journal"
                if args.action == "list":
                    print(
                        f"{record.draft_id}\t{record.account_id}\t{record.mailbox}\t"
                        f"{record.uid}\t{record.message_id}\t{record.created_at}\t"
                        f"{record.updated_at}\t{status}"
                    )
                else:
                    failed = True
                    print(f"{record.draft_id}: refused ({status})")
                continue
            if operation is not None:
                operation_validated = False
                marker_query_performed = False
                operation_matches: tuple[str, ...] = ()
                try:
                    store.validate_operation(record, operation)
                    operation_validated = True
                    operation_matches = _operation_matches(record, operation, accounts, credentials)
                    marker_query_performed = True
                    if (
                        len(operation_matches) == 1
                        and operation.get("new_uid", operation_matches[0]) == operation_matches[0]
                    ):
                        status = f"recovery: verified replacement uid {operation_matches[0]}"
                    elif not operation_matches:
                        status = "recovery_required: operation marker not found"
                    else:
                        status = f"recovery_required: operation marker is ambiguous ({len(operation_matches)} matches)"
                except DraftRecoveryRequiredError:
                    status = "recovery_required: invalid or legacy operation journal"
                except Exception:
                    status = "recovery_required: operation marker lookup failed"
                if args.action == "list":
                    print(
                        f"{record.draft_id}\t{record.account_id}\t{record.mailbox}\t"
                        f"{record.uid}\t{record.message_id}\t{record.created_at}\t"
                        f"{record.updated_at}\t{status}\toperation_id={operation['operation_id']}"
                    )
                    continue
                if args.clear_operation is not None:
                    if not operation_validated or not marker_query_performed:
                        failed = True
                        print(f"{record.draft_id}: refused ({status})")
                    elif not secrets.compare_digest(args.clear_operation, operation["operation_id"]):
                        failed = True
                        print(f"{record.draft_id}: refused (operation id does not match journal)")
                    elif operation_matches:
                        failed = True
                        print(f"{record.draft_id}: refused ({status})")
                    else:
                        store.clear_operation(record.draft_id)
                        print(
                            f"{record.draft_id}: cleared absent operation journal; "
                            "inspect the Drafts mailbox before a separately confirmed update"
                        )
                    continue
                try:
                    repaired = _recover_operation(
                        store, record, operation, operation_matches, accounts, credentials
                    )
                except DraftRecoveryRequiredError:
                    failed = True
                    print(f"{record.draft_id}: refused ({status})")
                else:
                    print(f"{record.draft_id}: recovered {record.uid} -> {repaired.uid}")
                continue

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
