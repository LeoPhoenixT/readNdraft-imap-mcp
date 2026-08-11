from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from .paths import current_app_paths


def linux_unit() -> str:
    executable = shlex.quote(sys.executable)
    return f"""[Unit]
Description=readNdraft local IMAP broker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} -m readndraft_imap_mcp.broker.daemon
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.config/readndraft %h/.local/state/readndraft %t/readndraft

[Install]
WantedBy=default.target
"""


def windows_task_command() -> str:
    executable = str(Path(sys.executable).resolve())
    action = f'"{executable}" -m readndraft_imap_mcp.broker.daemon'
    return (
        "schtasks /Create /TN readNdraftBroker /SC ONLOGON /RL LIMITED "
        f'/TR "{action}" /F'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize readNdraft local deployment")
    parser.add_argument("action", choices=("init", "print-linux-unit", "install-linux-unit", "print-windows-task"))
    args = parser.parse_args(argv)
    paths = current_app_paths()
    if args.action == "init":
        paths.ensure_private()
        paths.load_or_create_ipc_key()
        print("Initialized private readNdraft directories and IPC authentication key.")
        return 0
    if args.action == "print-linux-unit":
        print(linux_unit(), end="")
        return 0
    if args.action == "install-linux-unit":
        if not sys.platform.startswith("linux"):
            raise RuntimeError("systemd user units are supported only on Linux")
        target = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "readndraft-broker.service"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(linux_unit(), encoding="utf-8")
        print(f"Wrote {target}; run: systemctl --user daemon-reload && systemctl --user enable --now readndraft-broker")
        return 0
    print(windows_task_command())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
