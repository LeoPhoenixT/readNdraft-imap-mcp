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

### 2. Run guided setup

For Codex, run this command in a real interactive terminal:

```console
uvx readndraft-imap-mcp@latest setup --client codex --install-skill
```

Use `claude-code` instead of `codex` when configuring Claude Code. For ChatGPT
desktop, omit `--install-skill` and use `--client chatgpt-desktop`; setup prints
its MCP configuration but does not install an Agent Skill for that client.

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
5. Print a secret-free, version-pinned MCP configuration.
6. Install the packaged `readndraft-email` and `readndraft-update` Agent Skills
   when the selected client is Codex or Claude Code and skill installation was
   requested. ChatGPT desktop setup configures only the MCP.

Never put an IMAP password in a command argument, environment variable, MCP
configuration, issue, chat, or test report.

### 3. Add the generated client configuration

Copy the configuration printed by setup into your client's MCP configuration.
For Codex, add the printed `[mcp_servers.readndraft]` section to:

- Windows: `%USERPROFILE%\.codex\config.toml`
- Linux: `~/.codex/config.toml`

The generated command pins the package version so a future release cannot
silently change this security-sensitive MCP at startup. On Windows it uses
`uvw`, uv's consoleless launcher, so the MCP does not open a terminal window.
The Codex configuration also sets `default_tools_approval_mode = "approve"` to
avoid native tool-approval popups. Write operations still require direct
conversational confirmation through the packaged Agent Skill.

See the [Codex/ChatGPT guide](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/CLIENT_CODEX.md) or
[Claude Code guide](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/docs/CLIENT_CLAUDE.md)
for client-specific details.

### 4. Restart and verify

Fully restart the client after changing its MCP configuration. Then run an
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
| Runtime | Downloaded and run in uv's isolated cache; no repository clone is needed. |
| Client configuration | Stored by the selected client and contains no IMAP password. |
| Agent Skills | `readndraft-email` and `readndraft-update` under `~/.agents/skills` for Codex or `~/.claude/skills` for Claude Code. |
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
- Read sanitized HTML without loading remote content.
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
  email-safe CSS is normalized and inlined for broad mail-client compatibility.
  To, Cc, and Bcc may all be empty when the user wants an unaddressed draft.
- Update only a draft previously created by this MCP, after confirmation.

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
uvx readndraft-imap-mcp account add work --host imap.example.com --username login@imap.example.com --sender-address leo@example.com
uvx readndraft-imap-mcp account test work
uvx readndraft-imap-mcp account list
uvx readndraft-imap-mcp account set-sender work leo@example.com
uvx readndraft-imap-mcp account clear-sender work
uvx readndraft-imap-mcp account rotate-secret work
uvx readndraft-imap-mcp account disable work
uvx readndraft-imap-mcp account enable work
uvx readndraft-imap-mcp account delete work
```

Passwords are accepted only through a hidden local prompt. Account configuration
and credential operations are not MCP tools. `sender_address` controls the visible
`From` header of drafts and may differ from the IMAP login username. If omitted or
cleared, it falls back to the username. MCP `list_accounts` exposes the effective
sender so an agent can confirm it, but MCP cannot change it or override it per
draft.

## Configure another MCP client

Generate a secret-free configuration:

```console
uvx readndraft-imap-mcp@latest configure codex
uvx readndraft-imap-mcp@latest configure chatgpt-desktop
uvx readndraft-imap-mcp@latest configure claude-code
```

Use these commands to generate another client configuration without repeating
account setup. Each command prints a secret-free configuration pinned to the
readNdraft version that generated it.

The unified `mcp` command uses the authenticated on-demand launcher. It reuses a
healthy broker, starts exactly one when needed, and holds an authenticated lease
while the MCP frontend is connected. A launcher-owned broker exits only after
the final frontend disconnects and the idle period expires. Always-on systemd
and Windows scheduled-task deployments remain available through the legacy
administration documentation.

## Install the Agent Skills

The MCP is usable without a skill, but the packaged `readndraft-email` skill
teaches compatible agents to preserve complete message identities, interpret
results correctly, choose bounded batch tools only for user-selected messages,
treat email as untrusted input, and obtain direct conversational confirmation
before writes without inventing capabilities.

```console
uvx readndraft-imap-mcp skill install codex
uvx readndraft-imap-mcp skill install claude-code
uvx readndraft-imap-mcp skill status codex
uvx readndraft-imap-mcp skill install readndraft-update codex
uvx readndraft-imap-mcp skill status --all codex
uvx readndraft-imap-mcp skill print
```

The installer refuses to overwrite or remove a user-modified skill. Codex loads
personal skills from `~/.agents/skills`; Claude Code loads them from
`~/.claude/skills`.

ChatGPT desktop has no managed skill target in this package. Configure its MCP
with `configure chatgpt-desktop`; do not pass `chatgpt-desktop` to `skill`.

The legacy `skill install CLIENT` form manages `readndraft-email`. Setup and the
updater manage both packaged skills. `skill status [SKILL] CLIENT` reports
`current`, `outdated`, `modified`, `unmanaged`, or `not installed`; `--all`
checks both. Use `skill install [SKILL] CLIENT --force` only when intentionally
replacing a modified or unmanaged copy; replacement removes stale orphan files.

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

Client configurations are version-pinned. Running `uvx ...@latest` by itself
does not update the version that a configured MCP client starts.

### 1. Check without changing anything

Run the latest package's read-only update check:

```console
uvx readndraft-imap-mcp@latest update check --client codex
uvx readndraft-imap-mcp@latest update check --client claude-code
```

Use `--all` to inspect both clients. The check reports the pinned MCP version,
the latest package version, both managed skill states, and whether a restart is
required. It does not write configuration or skill files.

### 2. Apply a recognized user-level update

After reviewing the report, update one client interactively:

```console
uvx readndraft-imap-mcp@latest update apply --client codex
uvx readndraft-imap-mcp@latest update apply --client claude-code
```

The updater changes only a recognized user-level `readndraft` registration,
pins it to the exact package version performing the update, installs both
packaged skills from that version, creates a private configuration backup, and
verifies the result. It refuses unrecognized MCP entries. Modified or unmanaged
skills also block normal updates; inspect them before separately authorizing:

```console
uvx readndraft-imap-mcp@latest update apply --client codex --force-skill
```

`--force-skill` affects only the two skill directories and never authorizes
replacement of an unrecognized MCP entry. Automation may pass `--yes` only
after obtaining direct user confirmation. ChatGPT desktop remains a manual
configuration target and is not accepted by `update`.

### 3. Bootstrap the updater skill on an older installation

If `readndraft-update` was not installed by the original setup, install it once:

```console
uvx readndraft-imap-mcp@latest skill install readndraft-update codex
uvx readndraft-imap-mcp@latest skill install readndraft-update claude-code
```

The updater skill performs the read-only check first, presents the exact scope,
and requires confirmation immediately before applying an update.

### 4. Restart and verify

Fully close and restart every configured client, then run:

```console
uvx readndraft-imap-mcp@latest doctor --online
```

Confirm the new pinned version in the client configuration, reconnect the MCP,
and perform a harmless mailbox listing and one-result search.

Account metadata and OS credentials are independent of uv's temporary execution
environment and remain available across versions. Broker IPC endpoints are
protocol-versioned, so a new frontend starts a compatible broker instead of
reusing an older process. The old broker exits after its final client lease and
idle timeout.

## Uninstalling

Choose whether to remove only the MCP integration or all local readNdraft data.
Removing client entries and skills leaves accounts, credentials, audit history,
draft provenance, and attachment exchange files available for a later reinstall.

### 1. Remove every client entry

- Codex: remove the complete `[mcp_servers.readndraft]` table from
  `%USERPROFILE%\.codex\config.toml` on Windows or `~/.codex/config.toml` on
  Linux.
- ChatGPT desktop: remove `readndraft` from the client's MCP settings.
- Claude Code: run:

  ```console
  claude mcp remove readndraft --scope user
  ```

Fully close the clients after removing their entries so active MCP leases can
end and a launcher-owned broker can exit after its idle timeout.

### 2. Remove managed Agent Skills

Use only the clients that were installed:

```console
uvx readndraft-imap-mcp@latest skill uninstall codex
uvx readndraft-imap-mcp@latest skill uninstall readndraft-update codex
uvx readndraft-imap-mcp@latest skill uninstall claude-code
uvx readndraft-imap-mcp@latest skill uninstall readndraft-update claude-code
```

The installer refuses to remove a user-modified or unmanaged skill. Inspect it
manually rather than deleting it blindly.

### 3. Remove accounts and OS credentials

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

### 4. Optionally remove remaining local data

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
