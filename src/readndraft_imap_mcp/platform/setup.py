from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable

from readndraft_imap_mcp.admin import AccountFile
from readndraft_imap_mcp.broker import AccountConfig
from readndraft_imap_mcp.credentials import KeyringCredentialStore
from readndraft_imap_mcp.imap import ImapClient
from readndraft_imap_mcp.poc.credentials import require_approved_backend

from .client_config import client_config
from .paths import AppPaths, current_app_paths
from .skill import install_skill


def _prompt(input_fn: Callable[[str], str], label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input_fn(f"{label}{suffix}: ").strip()
    return value or default or ""


async def run_setup(
    paths: AppPaths,
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    credentials: KeyringCredentialStore | None = None,
    client_factory=ImapClient,
    require_backend: Callable[[], object] = require_approved_backend,
) -> tuple[AccountConfig, int]:
    require_backend()
    paths.ensure_private()
    paths.load_or_create_ipc_key()
    account_id = _prompt(input_fn, "Account alias")
    hostname = _prompt(input_fn, "IMAP host")
    port_text = _prompt(input_fn, "IMAP TLS port", "993")
    username = _prompt(input_fn, "IMAP username")
    auth_method = _prompt(input_fn, "Authentication method (login/plain)", "login")
    config = AccountConfig(account_id, hostname, int(port_text), username, auth_method)
    secret = secret_fn("IMAP password/app password (hidden): ")
    if not secret:
        raise ValueError("secret must be non-empty")
    try:
        with client_factory(config, secret) as client:
            mailbox_count = len(client.list_mailboxes())
        store = credentials or KeyringCredentialStore()
        await store.save_secret(account_id, secret)
        try:
            AccountFile(paths.accounts_file).upsert(config)
        except Exception:
            await store.delete_secret(account_id)
            raise
    finally:
        secret = ""
    return config, mailbox_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactively initialize readNdraft")
    parser.add_argument("--client", choices=("codex", "chatgpt-desktop", "claude-code"))
    parser.add_argument("--install-skill", action="store_true")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("Setup requires an interactive terminal; redirected input is refused.")
        return 1
    try:
        account, mailboxes = asyncio.run(run_setup(current_app_paths()))
        print(f"Authenticated {account.account_id}; discovered {mailboxes} mailboxes.")
        client = args.client or _prompt(
            input, "Client (codex/chatgpt-desktop/claude-code/skip)", "skip"
        )
        if client != "skip":
            if client not in {"codex", "chatgpt-desktop", "claude-code"}:
                raise ValueError("unsupported client")
            print("\nAdd this secret-free MCP configuration:\n")
            print(client_config(client, uvx=True), end="")
            install_requested = args.install_skill
            if not install_requested and client in {"codex", "claude-code"}:
                install_requested = _prompt(
                    input, "Install the readNdraft Agent Skill? (y/N)", "n"
                ).casefold() in {"y", "yes"}
            if install_requested and client in {"codex", "claude-code"}:
                target = install_skill(client, paths=current_app_paths())
                print(f"Installed the readNdraft Agent Skill at {target}")
        print("Run: uvx readndraft-imap-mcp doctor --online")
        return 0
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Setup failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
