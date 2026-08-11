# Windows installation

## Prerequisites

1. Install uv using the current [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
2. Open a new PowerShell window so the updated `PATH` is loaded.
3. Confirm the IMAP provider permits password or app-password authentication.

Verify uv before continuing:

```powershell
uv --version
```

readNdraft accepts secrets only through a hidden local prompt and requires the
Windows keyring backend. It does not fall back to a file or environment variable.
uv obtains a compatible Python runtime when required; users do not need to clone
the repository or install readNdraft permanently.

## Guided setup

```powershell
uvx readndraft-imap-mcp@latest setup --client codex --install-skill
```

Use `chatgpt-desktop` or `claude-code` for another client. The wizard checks
Windows Credential Manager, reads the IMAP password through a hidden prompt,
tests IMAP before saving the account, prints a secret-free version-pinned client
configuration, and installs the selected Agent Skill.

For Codex, copy the printed `[mcp_servers.readndraft]` table into
`%USERPROFILE%\.codex\config.toml`. For Claude Code, follow the
[Claude Code guide](CLIENT_CLAUDE.md) to add the printed JSON at user scope.

## Verify

```powershell
uvx readndraft-imap-mcp@latest doctor --online
```

Fully restart the target client. Confirm that readNdraft is connected, list its
accounts and mailboxes, search for one harmless message, and verify a plain-text
read did not mark it read.

If the credential is missing or has changed, rotate it through another hidden
prompt:

```powershell
uvx readndraft-imap-mcp@latest account rotate-secret ALIAS
```

Never put an IMAP password in PowerShell history, an environment variable, a
client configuration, an issue, or chat.
