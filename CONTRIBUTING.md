# Contributing

Contributions that preserve readNdraft's capability-minimized security model
are welcome. For usage questions or non-sensitive defects, open a GitHub issue
with private mail, account details, credentials, and local state removed.

Report suspected vulnerabilities through
[private vulnerability reporting](https://github.com/LeoPhoenixT/readNdraft-imap-mcp/security/advisories/new),
not a public issue.

## Development setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone the
repository, and create the locked development environment:

```console
uv sync --locked --extra dev
```

Run the same focused quality gates used by CI:

```console
uv run --locked pytest
uv run --locked python scripts/security_check.py
uv run --locked pip-audit --local
uv run --locked python scripts/license_check.py
uv build --no-sources
```

### Codex development MCP

This repository includes a project-scoped Codex MCP entry named
`readndraft_dev`. It runs the current checkout through the locked uv environment
and does not replace a separately installed, release-pinned `readndraft` entry.
The entry is non-blocking during ordinary sessions so a checkout that has not
run `uv sync` does not prevent Codex from starting.
After trusting the repository, start a new Codex session and verify the entry:

```console
codex -C . mcp get readndraft_dev --json
```

Run an end-to-end smoke test through a fresh, ephemeral, read-only Codex CLI
session:

```console
uv run --locked python scripts/codex_dev_mcp_smoke.py
```

The smoke test initializes the MCP locally and verifies its tool catalog without
calling a mail or account-data tool. It then starts an isolated Codex session;
the script temporarily makes the project entry required, so Codex cannot
complete unless the development MCP initializes. The session does not receive
account metadata and requires existing Codex CLI authentication. Local account
and credential state remains in the normal private readNdraft application
directories.

#### Testing broker-side source changes

A fresh MCP frontend reuses a healthy resident broker at the same IPC protocol
endpoint. Therefore, changes under `broker/`, `imap/`, `mime/`, `drafts/`, or
shared broker dependencies can still exhibit old behavior even though
`readndraft_dev` points at the current checkout. The tool-catalog smoke test
proves frontend initialization, not that broker implementation code restarted.

For those changes, follow the guarded broker-refresh procedure in
`.agents/skills/readndraft-local-mcp-test/SKILL.md`, then start a fresh Codex CLI
session and exercise the focused behavior that changed. Identify and stop only
the verified readNdraft development broker; never terminate unrelated Python or
Codex processes. Real account or draft tests require an explicit request and
must remain separate from the default metadata-free smoke test.

Tests must not use real credentials, mail, account metadata, or private state.
Keep changes narrowly scoped and update tests and public documentation when
behavior changes.

## Pull requests

- Create a focused branch from `main`.
- Explain the problem, security impact, and verification performed.
- Keep send, submit, ordinary-message deletion, raw IMAP, arbitrary flags,
  account administration, and credential retrieval outside the MCP tool surface.
  Preserve movement's native UID MOVE preference, broker-private UIDPLUS
  fallback, same-account, ordinary-mailbox, confirmation, and audit restrictions.
- Wait for all GitHub Actions checks to pass before merging.

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
