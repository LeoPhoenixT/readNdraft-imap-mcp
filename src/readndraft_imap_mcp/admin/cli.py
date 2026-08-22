from __future__ import annotations

import argparse
import asyncio
import getpass

from readndraft_imap_mcp.audit import JsonlAuditSink
from readndraft_imap_mcp.broker.accounts import AccountConfig
from readndraft_imap_mcp.credentials import KeyringCredentialStore
from readndraft_imap_mcp.imap import ImapClient
from readndraft_imap_mcp.platform import AppPaths, current_app_paths

from .accounts_file import AccountFile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Human-only readNdraft administration")
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("account")
    actions = account.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    for name in (
        "disable",
        "enable",
        "delete",
        "test",
        "rotate-secret",
        "clear-sender",
        "clear-sender-name",
    ):
        item = actions.add_parser(name)
        item.add_argument("account_id")
    add = actions.add_parser("add")
    add.add_argument("account_id")
    add.add_argument("--host", required=True)
    add.add_argument("--port", type=int, default=993)
    add.add_argument("--username", required=True)
    add.add_argument("--auth-method", choices=("login", "plain"), default="login")
    add.add_argument("--sender-address")
    add.add_argument("--sender-name")
    sender = actions.add_parser("set-sender")
    sender.add_argument("account_id")
    sender.add_argument("sender_address")
    sender_name = actions.add_parser("set-sender-name")
    sender_name.add_argument("account_id")
    sender_name.add_argument("sender_name")
    audit = commands.add_parser("audit")
    audit_actions = audit.add_subparsers(dest="action", required=True)
    audit_actions.add_parser("verify")
    audit_actions.add_parser("repair")
    return parser


def _repair_audit(paths: AppPaths) -> int:
    sink = JsonlAuditSink(paths.audit_file)
    count, archive, error = sink.repair_sync()
    if archive is not None:
        assert error is not None
        print(f"Audit chain was invalid ({error}).")
        print(f"Archived the old log to {archive}; a fresh chain starts on the next event.")
        return 0
    print(f"Audit integrity verified: {count} chained events. Nothing to repair.")
    return 0


async def _run(args: argparse.Namespace) -> int:
    paths = current_app_paths()
    paths.ensure_private()
    if args.command == "audit":
        if args.action == "repair":
            return _repair_audit(paths)
        count = await JsonlAuditSink(paths.audit_file).verify()
        print(f"Audit integrity verified: {count} chained events.")
        return 0
    accounts = AccountFile(paths.accounts_file)
    credentials = KeyringCredentialStore()
    if args.action == "list":
        for item in accounts.registry().list_safe():
            print(
                f"{item['id']}: {item['username']} at {item['host']}:{item['port']} "
                f"sender={item['sender_address']} sender_name={item['sender_name']!r} "
                f"enabled={item['enabled']}"
            )
        return 0
    if args.action in {"set-sender-name", "clear-sender-name"}:
        sender_name = args.sender_name if args.action == "set-sender-name" else None
        accounts.set_sender_name(args.account_id, sender_name)
        print(
            "Sender display name saved; active brokers reload configuration on the next request."
        )
        return 0
    if args.action in {"set-sender", "clear-sender"}:
        sender_address = (
            args.sender_address if args.action == "set-sender" else None
        )
        accounts.set_sender_address(args.account_id, sender_address)
        print(
            "Sender address saved; active brokers reload configuration on the next request."
        )
        return 0
    if args.action in {"disable", "enable"}:
        accounts.set_enabled(args.account_id, args.action == "enable")
        print(f"Account {args.action}d; active brokers reload configuration on the next request.")
        return 0
    if args.action == "delete":
        phrase = f"DELETE {args.account_id}"
        if input(f"Type {phrase!r} to delete account metadata and its secret: ").strip() != phrase:
            print("Cancelled.")
            return 1
        await credentials.delete_secret(args.account_id)
        accounts.delete(args.account_id)
        return 0
    if args.action == "rotate-secret":
        account = accounts.registry().require_enabled(args.account_id)
        secret = getpass.getpass("New IMAP password/app password (hidden): ")
        if not secret:
            raise ValueError("secret must be non-empty")
        with ImapClient(account, secret):
            pass
        await credentials.save_secret(args.account_id, secret)
        return 0
    if args.action == "test":
        account = accounts.registry().require_enabled(args.account_id)
        secret = await credentials.load_secret(args.account_id)
        try:
            with ImapClient(account, secret) as client:
                print(f"Authenticated; discovered {len(client.list_mailboxes())} mailboxes.")
        finally:
            secret = ""
        return 0
    config = AccountConfig(
        args.account_id,
        args.host,
        args.port,
        args.username,
        args.auth_method,
        sender_address=args.sender_address,
        sender_name=args.sender_name,
    )
    secret = getpass.getpass("IMAP password/app password (hidden): ")
    if not secret:
        raise ValueError("secret must be non-empty")
    await credentials.save_secret(config.account_id, secret)
    try:
        accounts.upsert(config)
    except Exception:
        await credentials.delete_secret(config.account_id)
        raise
    print("Account saved. Restart the broker to reload pinned configuration.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(_parser().parse_args(argv)))
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        print(f"Administration failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
