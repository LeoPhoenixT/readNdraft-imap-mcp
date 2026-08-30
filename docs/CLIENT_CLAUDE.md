# Claude Code

Configure the account first in a real terminal:

```console
uvx readndraft-imap-mcp@0.9.0 setup
```

Then run these commands inside Claude Code:

```text
/plugin marketplace add LeoPhoenixT/readNdraft-imap-mcp
/plugin install readndraft@readndraft
```

Start a new session and use `/mcp` to confirm `readndraft` is connected. The
plugin supplies the MCP definition and `readndraft-email` skill together and is
pinned to the matching Python runtime release.

For an installation created by 0.3.x or earlier, run this first:

```console
uvx readndraft-imap-mcp@0.9.0 migrate-plugin --client claude-code
```

The migration removes only a recognized user-scoped legacy entry and unmodified
managed skills. It refuses local/project/custom MCP definitions and modified
skills. Account, keyring, audit, attachment, and draft data are unchanged.

Use Claude Code's native plugin commands for updates and uninstall. Start a new
session after either operation.
