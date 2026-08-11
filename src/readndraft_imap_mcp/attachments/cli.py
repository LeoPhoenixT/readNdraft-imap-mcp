from __future__ import annotations

import argparse
import os
import subprocess
import sys

from readndraft_imap_mcp.platform.paths import current_app_paths

from .exchange import AttachmentExchange


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage fixed attachment exchange directories")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("path")
    listing = commands.add_parser("list")
    listing.add_argument("side", choices=("input", "output"), default="input", nargs="?")
    opening = commands.add_parser("open")
    opening.add_argument("side", choices=("input", "output"))
    args = parser.parse_args(argv)
    paths = current_app_paths()
    paths.ensure_private()
    exchange = AttachmentExchange(paths.attachment_input_dir, paths.attachment_output_dir)
    if args.action == "path":
        print(f"input: {exchange.input_dir}")
        print(f"output: {exchange.output_dir}")
        return 0
    directory = exchange.input_dir if args.side == "input" else exchange.output_dir
    if args.action == "list":
        if args.side == "input":
            for item in exchange.list_inputs():
                print(f"{item.name}\t{item.size}\t{item.sha256}")
        else:
            for item in sorted(directory.iterdir(), key=lambda value: value.name.casefold()):
                if item.is_file() and not item.is_symlink():
                    print(f"{item.name}\t{item.stat().st_size}")
        return 0
    if sys.platform == "win32":
        os.startfile(directory)  # type: ignore[attr-defined]
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", str(directory)], close_fds=True)
    else:
        raise RuntimeError("unsupported platform")
    return 0
