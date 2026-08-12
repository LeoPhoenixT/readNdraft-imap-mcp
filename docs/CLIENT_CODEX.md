# Codex and ChatGPT desktop

Generate a version-pinned configuration:

```console
uvx readndraft-imap-mcp@latest configure codex
```

Add the printed `[mcp_servers.readndraft]` table to `~/.codex/config.toml`. It
contains an absolute `uvx` command on Linux or consoleless `uvw` command on
Windows, package/version arguments, a 30-second startup allowance for the first
download, a 60-second tool timeout, required startup, and the no-popup `approve`
tool mode. It contains no secret or account password.

Run `codex mcp list`, then open a new session and confirm readNdraft is connected.
On the ChatGPT desktop client, generate `chatgpt-desktop` output and add the same
local stdio definition through its MCP settings.

Install both personal skills for Codex-compatible local skill discovery:

```console
uvx readndraft-imap-mcp skill install codex
uvx readndraft-imap-mcp skill install readndraft-update codex
```

This installs the skill for Codex only. The package has no managed ChatGPT
desktop skill target; ChatGPT desktop setup and `configure chatgpt-desktop`
provide MCP configuration only. Do not use `--install-skill` when running setup
for ChatGPT desktop.

To check or apply a later recognized user-level update, use:

```console
uvx readndraft-imap-mcp@latest update check --client codex
uvx readndraft-imap-mcp@latest update apply --client codex
```

The check is read-only. Apply backs up and replaces only the recognized
`[mcp_servers.readndraft]` table, refreshes both managed skills, and requires a
full Codex restart. It refuses unrecognized MCP entries and locally modified
skills by default.

The broker remains responsible for capability boundaries. The skill requires
direct conversational confirmation before writes. Generated Codex
configurations disable native tool-approval popups; the skill confirmation is a
behavioral safeguard rather than broker-verified authorization.
