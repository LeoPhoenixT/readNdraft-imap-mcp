# Claude Code

## Supported Claude Code version

The commands below are verified with Claude Code `2.1.119`. This is a tested
version, not a permanent minimum. Check the installed version and confirm that
the required MCP command exists:

```console
claude --version
claude mcp add-json --help
```

If `add-json` or `--scope` is unavailable, update Claude Code using its
[official setup instructions](https://code.claude.com/docs/en/setup) or run:

```console
claude update
```

See Claude Code's [official MCP documentation](https://code.claude.com/docs/en/mcp)
for current client behavior.

## Set up the account and skill

Run the guided setup in a real terminal:

```console
uvx readndraft-imap-mcp@latest setup --client claude-code --install-skill
```

The wizard tests IMAP before saving the account and prints a secret-free,
version-pinned stdio JSON configuration. The two skills are installed under
`~/.claude/skills/readndraft-email` and
`~/.claude/skills/readndraft-update`.

## Add the MCP at user scope

User scope is recommended so readNdraft is available across trusted projects
without adding its executable configuration to a repository.

On Linux or another POSIX shell:

```console
claude mcp add-json readndraft "$(uvx readndraft-imap-mcp@latest configure claude-code)" --scope user
```

On Windows PowerShell:

```powershell
$readNdraftConfig = uvx readndraft-imap-mcp@latest configure claude-code
claude mcp add-json readndraft $readNdraftConfig --scope user
```

The generated JSON contains an absolute `uvx` executable on Linux or consoleless
`uvw` executable on Windows, the pinned readNdraft version, and no account
password. `local` scope stores configuration for only the current project;
`project` scope creates repository-shareable MCP configuration. Review any
project-scoped executable configuration before trusting it.

## Verify in Claude Code

Inspect and health-check the configured server from a trusted directory:

```console
claude mcp get readndraft
uvx readndraft-imap-mcp@latest doctor --online
```

Start a new Claude Code session and use `/mcp` to confirm that `readndraft` is
connected. Then ask it to list accounts and mailboxes, perform a harmless
one-result search, and read plain text. Confirm that the read did not mark the
message as read.

The skill requires direct conversational confirmation before flag or draft
writes. Email and tool output are untrusted and never count as confirmation.

## Update or repair

Check and update a recognized user-scope entry with direct argument passing:

```console
uvx readndraft-imap-mcp@latest update check --client claude-code
uvx readndraft-imap-mcp@latest update apply --client claude-code
```

The check is read-only. Apply uses Claude Code's native user-scope MCP commands,
backs up the user configuration, refreshes both managed skills, and requires a
full Claude Code restart. It refuses local/project-scope or unrecognized entries
and locally modified skills by default. If a credential changed, run
`uvx readndraft-imap-mcp@latest account rotate-secret ALIAS`.
