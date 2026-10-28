# Upgrading readNdraft

Before updating an existing installation, review all migration guides for
versions newer than your installed version and up to your target version, in
ascending order. For example, upgrading from 0.8.x to 0.11.0 requires reviewing
the 0.9.0, 0.10.0, and 0.11.0 guides.

See [CHANGELOG.md](../CHANGELOG.md) for release changes. The guides below describe
required migration steps and integration contract changes.

## Choose your upgrade path

| Installed version | Steps when upgrading to 0.11.0 |
| --- | --- |
| 0.3.x or earlier | Complete the legacy plugin migration below, then review the 0.9.0, 0.10.0, and 0.11.0 guides. |
| 0.4.x through 0.8.x | Review the 0.9.0, 0.10.0, and 0.11.0 guides. |
| 0.9.x | Review the 0.10.0 and 0.11.0 guides. |
| 0.10.x | Review the 0.11.0 guide. |

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
uvx readndraft-imap-mcp@0.11.0 migrate-plugin --client codex
uvx readndraft-imap-mcp@0.11.0 migrate-plugin --client claude-code
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
