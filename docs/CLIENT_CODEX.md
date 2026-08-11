# Codex and ChatGPT desktop

Generate a version-pinned configuration:

```console
uvx readndraft-imap-mcp@latest configure codex
```

Add the printed `[mcp_servers.readndraft]` table to `~/.codex/config.toml`. It
contains an absolute `uvx` command on Linux or consoleless `uvw` command on
Windows, package/version arguments, a 30-second startup allowance for the first
download, a 60-second tool timeout, required startup, and prompted tool approval.
It contains no secret or account password.

Run `codex mcp list`, then open a new session and confirm readNdraft is connected.
On the ChatGPT desktop client, generate `chatgpt-desktop` output and add the same
local stdio definition through its MCP settings.

Install the personal skill for Codex-compatible local skill discovery:

```console
uvx readndraft-imap-mcp skill install codex
```

The broker remains responsible for capability boundaries. The skill requires
direct conversational confirmation before writes, while the generated client
configuration also requests prompted tool permission. These prompts are
behavioral safeguards rather than broker-verified authorization.
