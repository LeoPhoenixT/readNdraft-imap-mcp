from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from .credentials import (
    CredentialBackendError,
    load_secret,
    run_round_trip_probe,
    save_secret,
)
from .imap_probe import ImapProbe, ProbeError, base_report
from .ipc import run_ipc_probe


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    if value:
        return value
    if default is not None:
        return default
    raise ValueError(f"{label} is required")


def _confirm(phrase: str, explanation: str) -> bool:
    print(explanation)
    return input(f"Type {phrase!r} to continue: ").strip() == phrase


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Redacted report written to {path}")


def run_imap(output: Path, auth_method: str = "login") -> int:
    account_id = _prompt("Local account alias")
    host = _prompt("IMAP hostname")
    port = int(_prompt("TLS IMAP port", "993"))
    username = _prompt("IMAP username")

    secret: str | None = None
    try:
        secret = load_secret(account_id)
    except CredentialBackendError as exc:
        print(f"Credential store unavailable: {exc}", file=sys.stderr)

    if secret is None:
        secret = getpass.getpass("IMAP password/app password (hidden): ")
        if not secret:
            raise ValueError("A password or app password is required")
        if _confirm("STORE", "Optionally store this secret in the approved OS credential backend."):
            save_secret(account_id, secret)

    report: dict[str, Any]
    with ImapProbe(host, port, username, secret, auth_method=auth_method) as probe:
        report = base_report(probe)
        report["account_id"] = account_id

        mailbox = input("Mailbox for optional BODY.PEEK test [skip]: ").strip()
        if mailbox:
            subject = input(
                "Unique ASCII subject to locate [enter UID manually]: "
            ).strip()
            if subject:
                uid = probe.find_unique_uid_by_subject(mailbox, subject)
                print(f"Located dedicated test message UID {uid}")
            else:
                uid = _prompt("UID of a dedicated test message")
            report["body_peek"] = probe.peek_message(mailbox, uid)

            if _confirm(
                f"FLAG {uid}",
                "The probe will toggle only \\Flagged on this UID and immediately restore it.",
            ):
                report["star_restore"] = probe.probe_star_restore(mailbox, uid)

        draft_mailboxes = report["draft_mailboxes"]
        if draft_mailboxes:
            print("Discovered draft mailbox(es): " + ", ".join(draft_mailboxes))
            draft_mailbox = input("Draft mailbox to test [skip]: ").strip()
            if draft_mailbox:
                if draft_mailbox not in draft_mailboxes:
                    raise ProbeError("Draft mailbox must be selected from discovered \\Drafts mailboxes")
                if _confirm(
                    "APPEND DRAFT",
                    "A harmless draft addressed to the same account will be appended. Nothing is sent.",
                ):
                    report["draft_append"] = probe.append_test_draft(draft_mailbox)
        else:
            report["draft_append"] = {
                "status": "not_run",
                "reason": "No mailbox advertised the \\Drafts special-use flag.",
            }

    _write_report(output, report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Human-operated Phase 0 probes")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("credential", help="test the approved OS credential backend")
    subparsers.add_parser("ipc", help="test the current platform's local IPC adapter")
    imap = subparsers.add_parser("imap", help="probe a real custom IMAP server")
    imap.add_argument(
        "--output",
        type=Path,
        default=Path("phase0-report.json"),
        help="redacted JSON report path",
    )
    imap.add_argument(
        "--auth",
        choices=("login", "plain"),
        default="login",
        help="IMAP authentication mechanism (default: login)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "credential":
            print(json.dumps(run_round_trip_probe(), indent=2, sort_keys=True))
            return 0
        if args.command == "ipc":
            print(json.dumps(run_ipc_probe(), indent=2, sort_keys=True))
            return 0
        return run_imap(args.output, args.auth)
    except (CredentialBackendError, ProbeError, ValueError, OSError) as exc:
        print(f"Phase 0 probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


