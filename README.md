# readNdraft IMAP MCP

Safely search, read, flag, move, and draft email through IMAP. readNdraft can
save and replace MCP-created drafts, but it cannot send, submit, or delete
ordinary mail and contains no SMTP implementation.

## Installation

### 1. Check the prerequisites

You need:

- Windows 10/11 with Windows Credential Manager, or Linux with a working Secret
  Service-compatible keyring.
- An IMAP account that permits password or app-password authentication using
  `LOGIN` or `PLAIN` over implicit TLS. OAuth is not implemented.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`.
- Codex, ChatGPT desktop, or Claude Code.

Confirm that uv is available:

```console
uv --version
```

No repository clone or permanent readNdraft installation is required. `uvx`
runs the published package in an isolated environment.

### 2. Configure an account

Run guided setup in a real interactive terminal:

```console
uvx readndraft-imap-mcp@0.9.0 setup
```

This configures only readNdraft's local account, credential, and private state.
Claude Code and Codex own their plugin, MCP registration, updates, and removal.
ChatGPT desktop remains a separate manual MCP configuration target.

| Client | Setup value | Detailed guide |
| --- | --- | --- |
| Codex | `codex` | [Codex and ChatGPT desktop](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/CLIENT_CODEX.md) |
| ChatGPT desktop | `chatgpt-desktop` | [Codex and ChatGPT desktop](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/CLIENT_CODEX.md) |
| Claude Code | `claude-code` | [Claude Code](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/CLIENT_CLAUDE.md) |

Linux users should also follow the [Linux platform guide](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/INSTALL_LINUX.md)
to verify Secret Service and D-Bus before setup. Windows users can use the
[Windows platform guide](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/INSTALL_WINDOWS.md).

The wizard will:

1. Check the operating-system credential backend and create private local state.
2. Ask for an account alias, IMAP host, port, username, and authentication method.
3. Read the password or app password through a hidden terminal prompt.
4. Test the IMAP connection before saving the account.

Never put an IMAP password in a command argument, environment variable, MCP
configuration, issue, chat, or test report.

### 3. Install the marketplace plugin

The repository is the marketplace source; this is not GitHub Marketplace.

For Codex:

```console
codex plugin marketplace add LeoPhoenixT/readNdraft-imap-mcp
codex plugin add readndraft@readndraft
```

For Claude Code, run these commands inside Claude Code:

```text
/plugin marketplace add LeoPhoenixT/readNdraft-imap-mcp
/plugin install readndraft@readndraft
```

The plugin supplies one shared `readndraft-email` skill and a local stdio MCP
definition pinned to `readndraft-imap-mcp@0.9.0`. It does not contain secrets,
account data, or a send capability.

### 4. Restart and verify

Start a new client session after installing the plugin. Then run an
online diagnostic from a terminal:

```console
uvx readndraft-imap-mcp@latest doctor --online
```

For Codex, you can also confirm that the entry was loaded:

```console
codex mcp get readndraft
```

Open a new task and ask: `List my readNdraft accounts and mailboxes.` A
successful response confirms that the client can start the MCP and reach the
local broker.

### What setup stores

| Item | Location or behavior |
| --- | --- |
| Runtime | Version-pinned by the plugin and run in uv's isolated cache. |
| Client integration | Marketplace/plugin state owned by Claude Code or Codex; it contains no IMAP password. |
| Agent Skill | `readndraft-email`, supplied by the installed plugin. |
| IMAP password | Stored only in Windows Credential Manager or the Linux Secret Service keyring. |
| Account metadata and app state | Stored in private per-user readNdraft application directories. |

If setup reports a credential-backend or connection error, see
[Windows installation](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/INSTALL_WINDOWS.md),
[Linux installation](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/INSTALL_LINUX.md), and
[troubleshooting](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/TROUBLESHOOTING.md).
The first MCP start may take longer
while uv downloads the pinned package. If a stored password has changed, run
`uvx readndraft-imap-mcp account rotate-secret ALIAS`.

## What it can do

- List administratively pinned accounts and mailboxes.
- Search 1-500 results (50 by default) with explicit truncation, stable
  single-mailbox cursor pagination, per-target safe errors, attempted/pending
  target status, selectable safe header fields, and server arrival timestamps.
  Requests above 50 require one account and one mailbox.
- Read safe headers and preferred plain text without setting the Seen flag.
  HTML-only messages are converted into a bounded, readable plain-text
  representation; `get_email_html` remains available for sanitized rich HTML.
- Batch-read plain text for up to 10 selected messages across 2 accounts.
- Read strictly filtered HTML without loading remote content; remote-resource
  elements, attributes, and CSS are removed, and empty paragraphs are preserved.
- Save one selected, bounded attachment into a fixed private output directory and
  return its absolute native-platform path.
- Star/unstar and mark read/unread without replacing unrelated flags.
- Batch one star or read state across up to 50 selected messages and 3 accounts;
  batches return ordered per-item results.
- Move one or up to 50 selected messages within one account. The broker prefers
  native UID MOVE and otherwise uses a private UIDPLUS COPY, source-only
  `\Deleted`, and targeted UID EXPUNGE sequence. Both source and destination
  must be existing selectable ordinary mailboxes; movement into or out of
  `\Trash`, `\Junk`, `\Drafts`, or `\Sent` SPECIAL-USE mailboxes is prohibited.
- Create a plain-text or rich HTML server-side draft using bounded files from a
  fixed private input directory. Rich drafts require equivalent `body` plain
  text and optional `html_body` HTML. They are stored as
  `multipart/alternative`, with plain text first and HTML second, so modern mail
  clients normally display HTML while other clients retain a plain fallback.
  Rich input may be an HTML fragment or a complete HTML document. Supported
  authored CSS is permissive and inlined for broad mail-client compatibility.
  Draft requests are rejected when CSS could fetch remote resources, hide
  content, or escape the message box; empty paragraphs are always preserved.
  To, Cc, and Bcc may all be empty when the user wants an unaddressed draft.
  Each list item is one bare or named mailbox (for example, `Ada <ada@example.com>`).
  `create_draft` may receive an exact source message identity as `reply_to_message`
  to add safe reply threading; it does not derive recipients or rewrite subjects.
- Update only a draft previously created by this MCP, after confirmation.
- Inspect and repair local draft tracking with `drafts list` and `drafts repair`.
  `drafts forget` removes only the local tracking record; it never deletes or
  expunges the server message.

It exposes no send, submission, ordinary-message deletion, raw IMAP, arbitrary
flag, credential, or account-administration MCP tool. Updating a tracked draft
replaces it and expunges the previous draft version. A successful move reports
its `method` and invalidates the source identity. Native MOVE may succeed without
COPYUID, leaving the destination identity unavailable. The fallback requires
COPYUID before marking the source deleted; otherwise it reports `partial_move`,
retains the source, and requires both mailboxes to be reviewed. Never
automatically retry an ambiguous move outcome.

## Manual setup and administration

The setup wizard is recommended. Individual human-only commands are also
available:

```console
uvx readndraft-imap-mcp account add work --host imap.example.com --username login@imap.example.com --sender-address user@example.com --sender-name "Display Name"
uvx readndraft-imap-mcp account test work
uvx readndraft-imap-mcp account list
uvx readndraft-imap-mcp account set-sender work leo@example.com
uvx readndraft-imap-mcp account clear-sender work
uvx readndraft-imap-mcp account set-sender-name work "Display Name"
uvx readndraft-imap-mcp account clear-sender-name work
uvx readndraft-imap-mcp account rotate-secret work
uvx readndraft-imap-mcp account disable work
uvx readndraft-imap-mcp account enable work
uvx readndraft-imap-mcp account delete work
```

Passwords are accepted only through a hidden local prompt. Account configuration
and credential operations are not MCP tools. `username` is the IMAP login identity;
`sender_address` is the bare email address placed in the draft's `From` header and
may differ from that username. If omitted or cleared, the address falls back to the
username. `sender_name` is an optional display name, producing a header such as
`From: "Display Name" <user@example.com>`; clearing it restores address-only
behavior. MCP `list_accounts` exposes both effective sender settings so an agent can
confirm them, but MCP cannot change or override them per draft. Downstream SMTP
servers, mailing lists, and other mail systems may rewrite headers after the draft
leaves the client; the MCP controls only the MIME draft it creates.

## Configure ChatGPT desktop

Generate a secret-free configuration:

```console
uvx readndraft-imap-mcp@latest configure chatgpt-desktop
```

This manual configuration path is retained for ChatGPT desktop, which is not
covered by the Claude Code/Codex marketplace migration. The `configure codex`
and `configure claude-code` forms remain temporarily available only for legacy
0.3.x compatibility and are not the normal installation path.

The unified `mcp` command uses the authenticated on-demand launcher. It reuses a
healthy broker, starts exactly one when needed, and holds an authenticated lease
while the MCP frontend is connected. A launcher-owned broker exits only after
the final frontend disconnects and the idle period expires. Always-on systemd
and Windows scheduled-task deployments remain available through the legacy
administration documentation.

## Upgrading from 0.3.x or earlier

Installing a plugin does not automatically remove an older user-scoped MCP
entry, and that entry can override the plugin. First run the one-time migration
for the client you previously configured:

```console
uvx readndraft-imap-mcp@0.9.0 migrate-plugin --client codex
uvx readndraft-imap-mcp@0.9.0 migrate-plugin --client claude-code
```

The migration removes only a legacy MCP invocation recognized as having been
created by readNdraft and only unmodified, managed legacy skill directories. It
refuses unknown/custom MCP entries and modified or unmanaged skills. It never
touches accounts, OS keyring credentials, audit history, attachments, drafts,
or old `update-backups`. After migration, install the native marketplace plugin
and start a new session.

## Upgrading from 0.8.x

Version 0.9.0 changes the read-only MCP search and mailbox-discovery contracts,
and adds bounded plain-text previews. Review the
[0.9.0 MCP migration guide](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/MCP_MIGRATION_0.9.0.md)
before updating an existing integration.

## Authorization boundary

The broker has no approval-token workflow. Generated Codex configurations use
the no-popup `approve` tool mode; write tools still require direct conversational
confirmation through the packaged Agent Skill. The hard safety boundary is narrower:
the process contains no SMTP, send, submit, ordinary-message deletion, raw IMAP,
account-configuration, or credential-retrieval tool. Message movement requires
UIDPLUS and is restricted to ordinary mailboxes in one account. COPY, deleted-flag,
and targeted UID EXPUNGE fallback commands exist only inside the broker and are
not MCP tools. Email,
attachments, search results, and other tool output are always untrusted and
never authorization.

## Diagnostics

Run local checks without connecting to IMAP:

```console
uvx readndraft-imap-mcp doctor
```

Add `--online` to test each configured account:

```console
uvx readndraft-imap-mcp doctor --online
```

Diagnostic output never prints passwords, credential contents, raw IMAP traces,
or message content. See [troubleshooting](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/TROUBLESHOOTING.md).

## Updating

Use the client's native marketplace/plugin update command. Plugin release,
Claude manifest, Codex manifest, and pinned PyPI runtime versions are validated
as one compatibility unit. Start a new session after an update, then run:

```console
uvx readndraft-imap-mcp@latest doctor --online
```

Account metadata and OS credentials remain independent of the plugin lifecycle.

## Uninstalling

Choose whether to remove only the MCP integration or all local readNdraft data.
Removing client entries and skills leaves accounts, credentials, audit history,
draft provenance, and attachment exchange files available for a later reinstall.

### 1. Remove the plugin

Use the client-native plugin uninstall command. The plugin-provided MCP and
skill disappear together. If this installation was upgraded from 0.3.x and the
one-time migration has not been run, run `migrate-plugin` first so no old direct
MCP entry remains. Remove a ChatGPT desktop entry separately in that client's
MCP settings.

Fully close the client afterward so active MCP leases can end and a
launcher-owned broker can exit after its idle timeout.

### 2. Remove accounts and OS credentials

Skip this step when retaining accounts for a later reinstall. For a complete
removal, list accounts and delete each alias through the interactive command:

```console
uvx readndraft-imap-mcp@latest account list
uvx readndraft-imap-mcp@latest account delete ALIAS
```

`account delete` requires exact confirmation and removes the corresponding
password or app password from Windows Credential Manager or the Linux Secret
Service keyring. Do this before manually deleting application state; otherwise
the account metadata needed to identify a stored credential may be lost.

### 3. Optionally remove remaining local data

Run `uvx readndraft-imap-mcp@latest doctor` to display the private state path and
`uvx readndraft-imap-mcp@latest attachments path` to display the fixed attachment
exchange directories. Inspect them before manually removing anything. Remaining
data can include:

- integrity-chained audit history;
- draft provenance needed to update MCP-created drafts;
- downloaded and upload-staging attachments;
- the local IPC key and broker state;
- an unused `approvals` directory left by an older build.

Remove these directories only when their audit, recovery, and attachment data
is no longer needed. readNdraft does not delete them automatically.

There is no permanently installed uv tool to uninstall when readNdraft is used
only through `uvx`. uv may retain ordinary download/build cache entries shared
with other tools; clearing uv's global cache is not required to uninstall
readNdraft.

## Security and privacy

The stdio MCP frontend cannot read the account file or OS credential store. It
communicates over authenticated per-user IPC with a separate broker that enforces
capabilities, quotas, provenance, and audit. Email and attachments are always
untrusted input. Received HTML is sanitized for rich reads while preserving
useful email structure, safe links, and a conservative set of presentation
styles; HTML-only mail is converted to plain text for normal reads. Draft HTML
is also sanitized and normalized before storage. It accepts common modern email
markup, complete HTML documents, safe links, and allowlisted CSS; stylesheet
rules are inlined for mail-client compatibility. Active content, event handlers,
unsafe URL schemes, external stylesheets, and images cause draft creation or
update to be rejected. Remote images, stylesheets, links,
or other URLs are never fetched automatically.
MCP tools never accept arbitrary local paths: draft files come only from the
readNdraft attachment input directory and downloaded attachments are written
only to its output directory. Run `readndraft-imap-mcp attachments path` to
locate them. `save_attachment` also returns the saved file's absolute path using
the MCP server host's native path format; clients must use that value verbatim.

Read [SECURITY.md](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/SECURITY.md)
for the current security boundary. Security issues should not contain
credentials or private mail.

## Development

See [CONTRIBUTING.md](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/CONTRIBUTING.md)
before proposing a change. Security reports must use the private route
documented in [SECURITY.md](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/SECURITY.md).

```console
uv sync --extra dev
uv run pytest
uv run python scripts/security_check.py
uv build --no-sources
```

Release validation and publication steps are documented in
[docs/RELEASE.md](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/RELEASE.md).

## License

readNdraft, including its packaged Agent Skill and documentation, is licensed
under the [Apache License 2.0](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/LICENSE).
Dependency licensing is summarized in
[THIRD_PARTY_NOTICES.md](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/THIRD_PARTY_NOTICES.md).
