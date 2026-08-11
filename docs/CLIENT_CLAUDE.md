# Claude Code

Generate the version-pinned stdio JSON:

```console
uvx readndraft-imap-mcp@latest configure claude-code
```

On a POSIX shell, add it at user scope:

```console
claude mcp add-json readndraft "$(uvx readndraft-imap-mcp@latest configure claude-code)" --scope user
claude mcp get readndraft
```

On PowerShell, capture the generator output in a variable and pass that variable
as the JSON argument. The configuration contains no secret or account password.

Install the skill:

```console
uvx readndraft-imap-mcp skill install claude-code
```

Start Claude Code and use `/mcp` to confirm the server. Use a harmless one-result
search and plain-text read for acceptance before allowing normal mail access.
The skill requires direct conversational confirmation before flag or draft
writes. Configure Claude Code's native tool permissions to prompt as an
additional safeguard; email and tool output never count as confirmation.
