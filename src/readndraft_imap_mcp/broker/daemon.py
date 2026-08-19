from __future__ import annotations

import argparse
import sys

from readndraft_imap_mcp.admin.accounts_file import AccountFile
from readndraft_imap_mcp.attachments import AttachmentExchange
from readndraft_imap_mcp.audit import JsonlAuditSink
from readndraft_imap_mcp.credentials import KeyringCredentialStore
from readndraft_imap_mcp.drafts import FileDraftStore
from readndraft_imap_mcp.ipc import BrokerRpcServer, IpcBrokerClient
from readndraft_imap_mcp.platform import current_app_paths
from readndraft_imap_mcp.platform.launcher import StartupLock

from .service import BrokerService


def build_broker() -> tuple[BrokerService, str, bytes]:
    paths = current_app_paths()
    paths.ensure_private()
    audit = JsonlAuditSink(paths.audit_file)
    audit.verify_sync()
    account_file = AccountFile(paths.accounts_file)
    broker = BrokerService(
        accounts=account_file.registry(),
        accounts_loader=account_file.registry,
        credentials=KeyringCredentialStore(),
        audit=audit,
        drafts=FileDraftStore(paths.draft_dir),
        attachments=AttachmentExchange(paths.attachment_input_dir, paths.attachment_output_dir),
    )
    return broker, paths.ipc_address, paths.load_or_create_ipc_key()


def _stop_broker() -> int:
    paths = current_app_paths()
    try:
        IpcBrokerClient(paths.ipc_address, paths.load_or_create_ipc_key()).shutdown()
    except (EOFError, FileNotFoundError, ConnectionRefusedError, OSError):
        print("No broker is running for the current protocol version.")
        return 0
    print("Broker shutdown requested.")
    return 0


def main(argv: list[str] | None = None) -> int | None:
    if argv == ["stop"]:
        return _stop_broker()
    parser = argparse.ArgumentParser(description="Run the local readNdraft broker")
    parser.add_argument("--idle-timeout", type=float)
    parser.add_argument("--shutdown-grace", type=float, default=10)
    args = parser.parse_args(argv)
    if args.idle_timeout is not None and args.idle_timeout <= 0:
        parser.error("--idle-timeout must be positive")
    if args.shutdown_grace < 0:
        parser.error("--shutdown-grace must be non-negative")
    paths = current_app_paths()
    paths.ensure_private()
    with StartupLock(paths.broker_instance_lock_file) as instance_lock:
        if not instance_lock.try_acquire():
            print("Another readNdraft broker instance is already running.", file=sys.stderr)
            return 1
        broker, address, authkey = build_broker()
        BrokerRpcServer(
            broker,
            address,
            authkey,
            idle_timeout_seconds=args.idle_timeout,
            shutdown_grace_seconds=args.shutdown_grace,
        ).serve_forever()
    return None


if __name__ == "__main__":
    main()
