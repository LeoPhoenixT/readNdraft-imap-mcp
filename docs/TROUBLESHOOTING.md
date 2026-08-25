# Troubleshooting

Start with:

```console
uvx readndraft-imap-mcp doctor
uvx readndraft-imap-mcp doctor --online
```

## Credential backend fails

Windows must use the Windows keyring backend. Linux must have an active Secret
Service-compatible backend and D-Bus user session. readNdraft intentionally has
no plaintext fallback.

## IMAP authentication fails

Check the provider's IMAP hostname, implicit-TLS port, username, IMAP access
setting, and app-password requirements. Rotate the stored secret locally; never
paste it into chat or add it to an environment variable.

## A draft shows the IMAP login as its sender

Configure the account's visible address with `account set-sender ALIAS ADDRESS` and
its optional display name with `account set-sender-name ALIAS NAME`.
Use `account list` to verify the effective sender. Authentication continues to
use the original IMAP username; `account clear-sender ALIAS` restores that
username as the draft sender fallback.

## A client cannot open a downloaded attachment

`save_attachment` returns an absolute `saved_path` in the MCP server host's
native format. Use it verbatim rather than translating separators or rebuilding
the attachment directory. A remote or sandboxed MCP client may not have access
to that host path; in that case use `attachments path` locally and do not assume
the client has read the saved file.

## MCP startup times out

Run `doctor`, then `account test ALIAS`. Confirm `uv` is available on the
client's `PATH`; the plugin runs `uv tool run` with the release-pinned package.
The first package download can take longer than later starts. If `uv` is
missing, update it with its official installer and start a new client session.
Broker failures are printed to stderr, never MCP stdout.

## A console window appears with the Windows MCP

Report the client, client version, uv version, and plugin version. The plugin
uses the cross-platform `uv tool run` stdio command. Do not work around this by
restoring direct client configuration mutation; use the windowless installed
`readndraft-launch` fallback only if a supported client is proven to open a
console window.

## Skill does not appear

Confirm `readndraft@readndraft` is installed and enabled through the client's
plugin manager, then start a new session. For a pre-0.4.0 installation, run
`migrate-plugin --client CLIENT`; it refuses unknown MCP entries and modified
legacy skills rather than deleting them.

## Old behavior remains after an upgrade

Reconnect the MCP. The launcher checks the authenticated broker's package, IPC
protocol, and Python runtime, waits for any incompatible broker to stop and
release its singleton endpoint, and exposes the MCP frontend only after the
replacement passes the same compatibility check. Run `doctor` to compare the
frontend and broker versions, Python runtimes, and compatibility result.

Service-managed installations can stop a broker with either supported command:

```console
readndraft-broker stop
readndraft-imap-mcp broker stop
```

Normal upgrades do not require Python snippets, manual daemon cleanup, or a
retry after a transient endpoint error.

For repository development, a source edit does not change the IPC protocol
endpoint. A new `readndraft_dev` frontend can therefore reuse a healthy broker
that was started from older source. Follow the guarded refresh instructions in
`.agents/skills/readndraft-local-mcp-test/SKILL.md`, restart only the verified
development broker, and test the changed behavior itself. The tool-catalog smoke
test alone cannot prove broker-side code was refreshed.

## An old approvals directory remains after updating

Phase 17 retired command-line approvals. Existing files are intentionally left
untouched during upgrade. After confirming no older readNdraft version is still
running, you may manually remove only the `approvals` child of the printed
readNdraft state directory. Do not remove the audit log, IPC key, or `drafts`
directory as part of that cleanup.

## Search results or flags look wrong

Record only sanitized metadata and the server product/version. Do not include
message bodies or credentials. readNdraft separately fetches UID/FLAGS metadata
for compatibility with IMAP servers that omit flags from combined summaries.
Search order is descending mailbox UID for one target, not sender `Date`.
`received_at` is the server's normalized IMAP INTERNALDATE. Use `next_cursor`
with unchanged filters for subsequent pages rather than overlapping date ranges.
