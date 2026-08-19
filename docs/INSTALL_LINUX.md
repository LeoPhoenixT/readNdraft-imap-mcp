# Linux installation

## Prerequisites

readNdraft supports Linux desktop sessions with an unlocked
[Secret Service](https://specifications.freedesktop.org/secret-service-spec/latest/)-compatible
keyring. Common implementations include GNOME Keyring and KDE Wallet when its
Secret Service integration is enabled.

Before setup:

1. Install uv using the current [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
2. Install and unlock the desktop's Secret Service keyring.
3. Open a terminal inside that same logged-in desktop session.
4. Confirm the IMAP provider permits password or app-password authentication
   with `LOGIN` or `PLAIN` over implicit TLS.

Verify uv and the desktop D-Bus session:

```console
uv --version
test -n "$DBUS_SESSION_BUS_ADDRESS" && echo "D-Bus session detected"
```

Headless Linux sessions often lack Secret Service or its D-Bus session. readNdraft
fails closed in that situation and does not store a plaintext fallback.

## Guided setup

```console
uvx readndraft-imap-mcp@0.5.1 setup
```

The wizard verifies the credential backend, reads the IMAP password through a
hidden prompt, and tests the account before saving it. It does not edit Codex or
Claude configuration. Install the native marketplace plugin using the
[Codex](CLIENT_CODEX.md) or [Claude Code](CLIENT_CLAUDE.md) guide.

## Verify

Run the offline diagnostic first, then test configured accounts:

```console
uvx readndraft-imap-mcp@latest doctor
uvx readndraft-imap-mcp@latest doctor --online
```

The credential-backend line must report a working Secret Service keyring. Fully
restart the client, confirm readNdraft is connected, list accounts and
mailboxes, then perform a harmless one-result search and plain-text read.

## Linux troubleshooting

- Run setup and the MCP client as the same Linux user and in the same desktop
  session. A different SSH, `sudo`, container, cron, or system-service session
  may not have access to the user's D-Bus or unlocked keyring.
- If `DBUS_SESSION_BUS_ADDRESS` is empty, open a terminal from the desktop
  session. Do not work around the failure by storing the password in a file or
  environment variable.
- If the keyring exists but is locked, unlock it through the desktop's keyring
  application and rerun `doctor`.
- If the stored password changed, run
  `uvx readndraft-imap-mcp@latest account rotate-secret ALIAS`.
- The first client start can be slower while uv downloads the pinned package;
  generated configurations allow 30 seconds for startup.

The on-demand launcher is the recommended MCP path. An always-on systemd user
service is an advanced deployment and still requires a usable per-user Secret
Service session.
