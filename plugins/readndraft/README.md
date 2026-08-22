# readNdraft plugin

This self-contained Claude Code and Codex plugin supplies the `readndraft-email`
skill and the local stdio readNdraft MCP server. The server is pinned to the
same release as the plugin and runs through `uv`; it never sends email.

Configure at least one IMAP account separately with:

```text
uvx readndraft-imap-mcp@0.7.1 setup
```

Account configuration, credentials, audit history, downloaded attachments, and
saved drafts remain local readNdraft data. Installing or removing this plugin
does not remove that data.
