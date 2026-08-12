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

Configure the account's visible address with `account set-sender ALIAS ADDRESS`.
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

Run `doctor`, then `account test ALIAS`. Confirm the generated client command
points to an existing absolute `uvx` executable on Linux or `uvw.exe` on Windows
and allows 30 seconds for the first package download. If `uvw.exe` is missing,
update uv with its official installer and generate the configuration again.
Broker failures are printed to stderr, never MCP stdout.

## A console window appears with the Windows MCP

Regenerate the client configuration with the current readNdraft release. Windows
configurations use `uvw.exe tool run`, and the direct `readndraft-mcp.exe` and
`readndraft-launch.exe` entry points use the GUI subsystem. Reinstall a local
development checkout with `uv sync --reinstall-package readndraft-imap-mcp`
before testing its direct executables.

## Skill does not appear

Run `skill status CLIENT` and confirm `SKILL.md` exists under the printed personal
skill directory. Reopen the client's skill view/session after first creating a
top-level skill directory. The MCP tools work even without the skill.

If status is `outdated`, run `skill install CLIENT`. If it is `modified` or
`unmanaged`, inspect the directory first; use `--force` only when you intend to
replace it completely. A forced replacement removes orphan reference files.

For a complete recognized user-level MCP and skill refresh, run `update check
--client CLIENT` first, then `update apply --client CLIENT`. An `unrecognized`
MCP result is intentionally not overwritten. A modified or unmanaged skill
requires separate review before `--force-skill`; private configuration backups
are stored under readNdraft's state directory.

## Old behavior remains after an upgrade

Current broker endpoints include the IPC protocol version. Reconnect the MCP so
the launcher starts the matching broker. An older broker on a previous endpoint
cannot be reused and exits after its leases and idle timeout. If a response still
mentions `readndraft-approve`, verify the client command points to the updated
package and run `skill status CLIENT`; the current protocol has no approval ID.

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
