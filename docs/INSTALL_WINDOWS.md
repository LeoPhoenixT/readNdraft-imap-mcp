# Windows installation

## Prerequisites

1. Install uv using the current [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
2. Open a new PowerShell window and run `uv --version`.
3. Confirm the IMAP provider permits password or app-password authentication.

readNdraft accepts secrets only through a hidden local prompt and requires the
Windows keyring backend. It does not fall back to a file or environment variable.

## Initialize

```powershell
uvx readndraft-imap-mcp@latest setup --client codex --install-skill
```

Use `chatgpt-desktop` or `claude-code` for another client. The wizard tests IMAP
before saving account metadata. Copy the printed secret-free configuration to the
client location described in the relevant client guide.

## Verify

```powershell
uvx readndraft-imap-mcp doctor --online
```

Then restart the target client, confirm the ten readNdraft tools are visible,
search for one harmless message, and verify a plain-text read did not mark it
read. Real named-pipe ACL and simultaneous-client acceptance remain mandatory
before declaring a release production-ready.
