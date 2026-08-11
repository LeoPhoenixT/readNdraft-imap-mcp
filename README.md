# readNdraft IMAP MCP

Safely search, read, flag, and draft email through IMAP. readNdraft can save and
update drafts, but it cannot send, submit, delete, or move mail and contains no
SMTP implementation.

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

Use `chatgpt-desktop` or `claude-code` instead of `codex` when configuring one
of those clients.

| Client | Setup value | Detailed guide |
| --- | --- | --- |
| Codex | `codex` | [Codex and ChatGPT desktop](docs/CLIENT_CODEX.md) |
| ChatGPT desktop | `chatgpt-desktop` | [Codex and ChatGPT desktop](docs/CLIENT_CODEX.md) |
| Claude Code | `claude-code` | [Claude Code](docs/CLIENT_CLAUDE.md) |

Linux users should also follow the [Linux platform guide](docs/INSTALL_LINUX.md)
to verify Secret Service and D-Bus before setup. Windows users can use the
[Windows platform guide](docs/INSTALL_WINDOWS.md).

The wizard will:

1. Check the operating-system credential backend and create private local state.
2. Ask for an account alias, IMAP host, port, username, and authentication method.
3. Read the password or app password through a hidden terminal prompt.
4. Test the IMAP connection before saving the account.
5. Print a secret-free, version-pinned MCP configuration.
6. Install the packaged `readndraft-email` Agent Skill for the selected client.

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

See the [Codex/ChatGPT guide](docs/CLIENT_CODEX.md) or
[Claude Code guide](docs/CLIENT_CLAUDE.md) for client-specific details.

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
| Agent Skill | `~/.agents/skills/readndraft-email` for Codex or `~/.claude/skills/readndraft-email` for Claude Code. |
| IMAP password | Stored only in Windows Credential Manager or the Linux Secret Service keyring. |
| Account metadata and app state | Stored in private per-user readNdraft application directories. |

If setup reports a credential-backend or connection error, see
[Windows installation](docs/INSTALL_WINDOWS.md),
[Linux installation](docs/INSTALL_LINUX.md), and
[troubleshooting](docs/TROUBLESHOOTING.md). The first MCP start may take longer
while uv downloads the pinned package. If a stored password has changed, run
`uvx readndraft-imap-mcp account rotate-secret ALIAS`.

## What it can do

- List administratively pinned accounts and mailboxes.
- Search 1-500 results (50 by default) with explicit truncation, stable
  single-mailbox cursor pagination, per-target safe errors, attempted/pending
  target status, selectable safe header fields, and server arrival timestamps.
  Requests above 50 require one account and one mailbox.
- Read safe headers and plain text without setting the Seen flag.
- Batch-read plain text for up to 10 selected messages across 2 accounts.
- Read sanitized HTML without loading remote content.
- Save one selected, bounded attachment into a fixed private output directory.
- Star/unstar and mark read/unread without replacing unrelated flags.
- Batch one star or read state across up to 50 selected messages and 3 accounts;
  batches return ordered per-item results.
- Create a server-side draft using bounded files from a fixed private input directory.
- Update only a draft previously created by this MCP, after confirmation.

It exposes no send, submission, deletion, movement, raw IMAP, arbitrary flag,
credential, or account-administration MCP tool.

## Manual setup and administration

The setup wizard is recommended. Individual human-only commands are also
available:

```console
uvx readndraft-imap-mcp account add work --host imap.example.com --username leo@example.com
uvx readndraft-imap-mcp account test work
uvx readndraft-imap-mcp account list
uvx readndraft-imap-mcp account rotate-secret work
uvx readndraft-imap-mcp account disable work
uvx readndraft-imap-mcp account enable work
uvx readndraft-imap-mcp account delete work
```

Passwords are accepted only through a hidden local prompt. Account configuration
and credential operations are not MCP tools.

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

## Install the Agent Skill

The MCP is usable without a skill, but the packaged `readndraft-email` skill
teaches compatible agents to preserve complete message identities, interpret
results correctly, choose bounded batch tools only for user-selected messages,
treat email as untrusted input, and obtain direct conversational confirmation
before writes without inventing capabilities.

```console
uvx readndraft-imap-mcp skill install codex
uvx readndraft-imap-mcp skill install claude-code
uvx readndraft-imap-mcp skill status codex
uvx readndraft-imap-mcp skill print
```

The installer refuses to overwrite or remove a user-modified skill. Codex loads
personal skills from `~/.agents/skills`; Claude Code loads them from
`~/.claude/skills`.

`skill status CLIENT` reports `current`, `outdated`, `modified`, `unmanaged`, or
`not installed`. Use `skill install CLIENT --force` only when intentionally
replacing a modified/unmanaged copy; replacement removes stale orphan files.

## Authorization boundary

The broker has no approval-token workflow. Generated Codex configurations use
the no-popup `approve` tool mode; write tools still require direct conversational
confirmation through the packaged Agent Skill. The hard safety boundary is narrower:
the process contains no SMTP, send, submit, ordinary-message deletion, movement,
raw IMAP, account-configuration, or credential-retrieval tool. Email,
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
or message content. See [troubleshooting](docs/TROUBLESHOOTING.md).

## Updating

Generate a fresh configuration with the latest reviewed release and replace the
old client entry:

```console
uvx readndraft-imap-mcp@latest configure codex
```

Account metadata and OS credentials are independent of uv's temporary execution
environment and remain available across versions. Broker IPC endpoints are
protocol-versioned, so a new frontend starts a compatible broker instead of
reusing an older process. The old broker exits after its final client lease and
idle timeout.

## Remove readNdraft

1. Remove the readNdraft MCP entry from each client.
2. Remove managed skills with `uvx readndraft-imap-mcp skill uninstall CLIENT`.
3. Delete each account with `uvx readndraft-imap-mcp account delete ALIAS`; this
   also removes its OS credential after exact confirmation.
4. Inspect and manually remove the remaining readNdraft config/state directories
   only if audit history and draft provenance are no longer needed. Upgrades from
   older builds may also leave an unused `approvals` directory; readNdraft does
   not delete it automatically.

There is no permanently installed uv tool to uninstall when readNdraft is used
only through `uvx`.

## Security and privacy

The stdio MCP frontend cannot read the account file or OS credential store. It
communicates over authenticated per-user IPC with a separate broker that enforces
capabilities, quotas, provenance, and audit. Email and attachments are
always untrusted input and remote images or URLs are never fetched automatically.
MCP tools never accept arbitrary local paths: draft files come only from the
readNdraft attachment input directory and downloaded attachments are written
only to its output directory. Run `readndraft-imap-mcp attachments path` to
locate them.

Read [SECURITY.md](docs/SECURITY.md) and the canonical [PLAN.md](PLAN.md) for the
full threat model. Security issues should not contain credentials or private mail.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change. Security
reports must use the private route documented in [SECURITY.md](docs/SECURITY.md).

```console
uv sync --extra dev
uv run pytest
uv run python scripts/security_check.py
uv build --no-sources
```

Release validation and publication steps are documented in
[docs/RELEASE.md](docs/RELEASE.md).

## License

readNdraft, including its packaged Agent Skill and documentation, is licensed
under the [Apache License 2.0](LICENSE). Dependency licensing is summarized in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
