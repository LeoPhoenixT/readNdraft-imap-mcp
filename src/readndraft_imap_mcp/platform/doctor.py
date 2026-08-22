from __future__ import annotations

import argparse
import asyncio
import os
from importlib.metadata import PackageNotFoundError, version

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.admin import AccountFile
from readndraft_imap_mcp.audit import JsonlAuditSink
from readndraft_imap_mcp.credentials import KeyringCredentialStore
from readndraft_imap_mcp.credentials.backend import inspect_backend
from readndraft_imap_mcp.imap import ImapClient

from .launcher import broker_health
from .paths import current_app_paths


async def _doctor(online: bool) -> list[tuple[str, bool, str]]:
    paths = current_app_paths()
    checks: list[tuple[str, bool, str]] = []
    paths.ensure_private()
    paths.load_or_create_ipc_key()
    private = os.name == "nt" or all(
        path.stat().st_mode & 0o077 == 0
        for path in (paths.config_dir, paths.state_dir, paths.runtime_dir, paths.ipc_key_file)
    )
    checks.append(("private paths", private, str(paths.state_dir)))
    checks.append(("attachment input", paths.attachment_input_dir.is_dir(), str(paths.attachment_input_dir)))
    checks.append(("attachment output", paths.attachment_output_dir.is_dir(), str(paths.attachment_output_dir)))
    backend = inspect_backend()
    checks.append(("credential backend", backend.approved, backend.implementation))
    for package in ("readndraft-imap-mcp", "mcp", "keyring"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "not installed"
        checks.append((f"runtime {package}", installed != "not installed", installed))
    try:
        accounts = AccountFile(paths.accounts_file).load()
        checks.append(("account metadata", bool(accounts), f"{len(accounts)} configured"))
    except (OSError, ValueError) as exc:
        accounts = ()
        checks.append(("account metadata", False, str(exc)))
    credentials = KeyringCredentialStore()
    for account in accounts:
        try:
            secret = await credentials.load_secret(account.account_id)
            checks.append((f"credential {account.account_id}", bool(secret), "present"))
            if online:
                try:
                    with ImapClient(account, secret) as client:
                        count = len(client.list_mailboxes())
                    checks.append((f"IMAP {account.account_id}", True, f"{count} mailboxes"))
                except Exception as exc:
                    checks.append((f"IMAP {account.account_id}", False, type(exc).__name__))
            secret = ""
        except Exception as exc:
            checks.append((f"credential {account.account_id}", False, type(exc).__name__))
    try:
        events = await JsonlAuditSink(paths.audit_file).verify()
        checks.append(("audit chain", True, f"{events} events"))
    except (OSError, RuntimeError, ValueError) as exc:
        checks.append(("audit chain", False, str(exc)))
    broker = broker_health(paths)
    if broker is None:
        checks.append(("broker state", True, "not running (normal for on-demand mode)"))
    else:
        frontend_version = __version__
        broker_version = str(broker.get("package_version", "unknown"))
        checks.append(
            (
                "broker state",
                broker_version == frontend_version,
                f"healthy; package {broker_version} (frontend {frontend_version})",
            )
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check readNdraft local configuration")
    parser.add_argument("--online", action="store_true", help="Test IMAP authentication")
    args = parser.parse_args(argv)
    checks = asyncio.run(_doctor(args.online))
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
