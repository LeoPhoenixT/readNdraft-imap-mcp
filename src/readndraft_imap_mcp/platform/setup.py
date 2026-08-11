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


def _print_client_setup(
    client: str,
    config: str,
    *,
    skill_target: object | None = None,
) -> None:
    if client == "codex":
        destination = "your Codex config.toml"
    elif client == "chatgpt-desktop":
        destination = "the ChatGPT desktop MCP settings"
    else:
        destination = "the JSON argument to `claude mcp add-json readndraft`"

    print("\nMCP client configuration")
    print(f"Copy only the content between COPY START and COPY END into {destination}.")
    print("\n----- COPY START -----")
    print(config.rstrip("\n"))
    print("----- COPY END -----\n")

    if skill_target is not None:
        print("Agent Skill")
        print(f"Installed at: {skill_target}\n")

    print("Next steps")
    print("1. Add the copied configuration to the client.")
    print("2. Fully restart the client.")
    print("3. Verify from a terminal:")
    print("   uvx readndraft-imap-mcp@latest doctor --online")


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
            config = client_config(client, uvx=True)
            install_requested = args.install_skill
            if not install_requested and client in {"codex", "claude-code"}:
                install_requested = _prompt(
                    input, "Install the readNdraft Agent Skill? (y/N)", "n"
                ).casefold() in {"y", "yes"}
            skill_target = None
            if install_requested and client in {"codex", "claude-code"}:
                skill_target = install_skill(client, paths=current_app_paths())
            _print_client_setup(client, config, skill_target=skill_target)
        else:
            print("\nNext step")
            print("Run: uvx readndraft-imap-mcp@latest doctor --online")
        return 0
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Setup failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
