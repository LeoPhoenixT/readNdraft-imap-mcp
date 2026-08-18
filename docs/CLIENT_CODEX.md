# Codex and ChatGPT desktop

## Codex plugin installation

Configure the account first:

```console
uvx readndraft-imap-mcp@0.5.0 setup
```

Then let Codex own the integration lifecycle:

```console
codex plugin marketplace add LeoPhoenixT/readNdraft-imap-mcp
codex plugin add readndraft@readndraft
```

Start a new session, run `codex plugin list`, and ask readNdraft to list accounts
and mailboxes. The plugin supplies the MCP definition and `readndraft-email`
skill together, pinned to the same release. It contains no password and exposes
no SMTP/send tool.

For an installation created by 0.3.x or earlier, remove only recognized legacy
state before installing the plugin:

```console
uvx readndraft-imap-mcp@0.5.0 migrate-plugin --client codex
```

The migration refuses unknown/custom MCP entries and modified skills.

ChatGPT desktop is not covered by the plugin marketplace. Generate its manual,
secret-free MCP definition with `configure chatgpt-desktop`.
