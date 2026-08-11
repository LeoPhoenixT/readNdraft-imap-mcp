from __future__ import annotations

import sys


HELP = """usage: readndraft-imap-mcp COMMAND [OPTIONS]

Commands:
  setup       interactively initialize an account and client
  account     add, list, test, enable, disable, rotate, or delete accounts
  configure   print a version-pinned uvx MCP client configuration
  doctor      check local configuration and optional IMAP connectivity
  skill       install, inspect, or remove the packaged Agent Skill
  attachments show or open the fixed attachment exchange directories
  audit       verify the integrity-chained local audit log
  mcp         run the on-demand broker and stdio MCP frontend
  broker      run the broker directly for service-managed deployments
"""


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] in {"-h", "--help", "help"}:
        print(HELP, end="")
        return 0
    command, rest = values[0], values[1:]
    if command == "setup":
        from readndraft_imap_mcp.platform.setup import main as target
    elif command == "account":
        from readndraft_imap_mcp.admin.cli import main as admin_main
        return admin_main(["account", *rest])
    elif command == "configure":
        from readndraft_imap_mcp.platform.client_config import main as configure_main
        return configure_main(rest, default_uvx=True)
    elif command == "doctor":
        from readndraft_imap_mcp.platform.doctor import main as target
    elif command == "skill":
        from readndraft_imap_mcp.platform.skill import main as target
    elif command == "attachments":
        from readndraft_imap_mcp.attachments.cli import main as target
    elif command == "audit":
        from readndraft_imap_mcp.admin.cli import main as admin_main
        return admin_main(["audit", "verify", *rest])
    elif command == "mcp":
        from readndraft_imap_mcp.platform.launcher import main as target
    elif command == "broker":
        from readndraft_imap_mcp.broker.daemon import main as target
    else:
        print(f"Unknown command: {command}\n\n{HELP}", file=sys.stderr, end="")
        return 2
    result = target(rest)
    return 0 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main())
