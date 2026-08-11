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

Tests must not use real credentials, mail, account metadata, or private state.
Keep changes narrowly scoped and update tests and public documentation when
behavior changes.

## Pull requests

- Create a focused branch from `main`.
- Explain the problem, security impact, and verification performed.
- Keep send, submit, ordinary-message deletion, movement, raw IMAP, arbitrary
  flags, account administration, and credential retrieval outside the MCP tool
  surface.
- Wait for all GitHub Actions checks to pass before merging.

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
