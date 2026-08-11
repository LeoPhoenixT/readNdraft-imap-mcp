# Linux installation

## Prerequisites

1. Install uv using the current [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
2. Ensure a Secret Service-compatible keyring is running in the user session.
3. Run `uv --version` in a terminal attached to that desktop/session.
4. Confirm the IMAP provider permits password or app-password authentication.

Headless Linux sessions often lack Secret Service or its D-Bus session. readNdraft
fails closed in that situation and does not store a plaintext fallback.

## Initialize and verify

```console
uvx readndraft-imap-mcp@latest setup --client codex --install-skill
uvx readndraft-imap-mcp doctor --online
```

The on-demand launcher is the default unified MCP path. An always-on systemd user
service remains optional; print/install it with the legacy `readndraft-install`
entry point from an installed development environment when required.
