# Upgrading readNdraft

Before updating an existing installation, review all migration guides for
versions newer than your installed version and up to your target version, in
ascending order. For example, upgrading from 0.8.x to 0.11.0 requires reviewing
the 0.9.0, 0.10.0, and 0.11.0 guides.

See [CHANGELOG.md](../CHANGELOG.md) for release changes. The guides below describe
required migration steps and integration contract changes.

## Choose your upgrade path

| Installed version | Steps when upgrading to 0.12.2 |
| --- | --- |
| 0.3.x or earlier | Complete the legacy plugin migration below, then review the 0.9.0, 0.10.0, and 0.11.0 guides. |
| 0.4.x through 0.8.x | Review the 0.9.0, 0.10.0, and 0.11.0 guides. |
| 0.9.x | Review the 0.10.0 and 0.11.0 guides. |
| 0.10.x | Review the 0.11.0 guide and the update notes below. |
| 0.11.x through 0.12.1 | Review the update notes below. |

## Updates after 0.11.0

Versions 0.11.x and 0.12.x require no integration contract migration when
upgrading to 0.12.2. Review these release-specific update notes:

### Upgrading to 0.12.2

Version 0.12.2 fixes IPC validation and read-result model consistency, and
extracts MIME selection helpers while preserving existing decoding and retrieval
limits. MCP tool schemas, IPC 12, authentication, and mail permissions are
unchanged; no integration migration is required. Update both the runtime and
marketplace plugin, then fully close and restart the client so its frontend
leases end and the launcher-owned broker can exit before the new version starts.

Development and CI use uv 0.13.4 for updated CA trust and pin the audit cache
dependencies filelock to 4.2.1 and msgpack to 1.2.3. These development-tool changes
do not alter the published runtime dependency pins.

### Upgrading to 0.12.1

Version 0.12.1 updates MCP to 1.30.0 and certifi to 2026.10.19 for dependency
security fixes, and pins development pip to 26.2.2 to update its separate vendored
CA bundle. Update both the runtime and marketplace plugin, then reconnect the
client. MCP tool schemas, IPC 12, credentials, and read/draft permissions are
unchanged; no integration migration is required.

This does not update Python, the OS CA store used by IMAP TLS, or other
environments. Keep those current through normal system maintenance.

### Upgrading to 0.12.0

Version 0.12.0 fixes batch-read scheduling and per-account failure isolation,
including cancellation-safe session cleanup. The MCP tool schemas and IPC 12
wire contract are unchanged, so existing tool calls need no migration. Update
both the runtime and marketplace plugin, then reconnect the client to use the
new package version.

## MCP integration migration guides

| Target version | Contract changes | Guide |
| --- | --- | --- |
| 0.9.0 | Explicit search targets, per-account mailbox results, and bounded text previews. | [0.9.0 MCP migration](MCP_MIGRATION_0.9.0.md) |
| 0.10.0 | IPC 11, per-target search status, selective BODYSTRUCTURE reads, attachment-filename search, and draft recovery. | [0.10.0 MCP migration](MCP_MIGRATION_0.10.0.md) |
| 0.11.0 | IPC 12, structured errors, per-account task rate limits, and deadline results for batches and writes. | [0.11.0 MCP migration](MCP_MIGRATION_0.11.0.md) |

## Legacy plugin migration: 0.3.x or earlier

Installing a plugin does not automatically remove an older user-scoped MCP
entry, and that entry can override the plugin. First run the one-time migration
for the client you previously configured. Run only the applicable command, or
both if you previously configured both clients:

```console
uvx readndraft-imap-mcp@0.12.2 migrate-plugin --client codex
uvx readndraft-imap-mcp@0.12.2 migrate-plugin --client claude-code
```

The migration removes only a legacy MCP invocation recognized as having been
created by readNdraft and only unmodified, managed legacy skill directories. It
refuses unknown/custom MCP entries and modified or unmanaged skills. It never
touches accounts, OS keyring credentials, audit history, attachments, drafts,
or old `update-backups`. After migration, install the native marketplace plugin
using the [Codex guide](CLIENT_CODEX.md) or [Claude Code guide](CLIENT_CLAUDE.md)
and start a new session.

ChatGPT desktop retains its separate manual configuration path; it is not
covered by the Claude Code/Codex marketplace migration. See the
[ChatGPT desktop configuration guide](CLIENT_CODEX.md).

## Update and verify

Use the client's native marketplace/plugin update command. Plugin release,
Claude manifest, Codex manifest, and pinned PyPI runtime versions are validated
as one compatibility unit. Fully close and restart the client after updating,
then run:

```console
uvx readndraft-imap-mcp@latest doctor --online
```

Account metadata and OS credentials remain independent of the plugin lifecycle.
If old behavior remains, see [troubleshooting](TROUBLESHOOTING.md#old-behavior-remains-after-an-upgrade).
