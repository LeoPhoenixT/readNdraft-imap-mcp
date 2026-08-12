---
name: readndraft-local-mcp-test
description: Verify the repository-local readNdraft development MCP and its Codex integration without exposing account or mail data. Use when asked to test `readndraft_dev`, run the local MCP smoke test, diagnose whether Codex loads this checkout's MCP configuration, or validate local MCP changes before commit or release.
---

# Test the readNdraft Local MCP

Run this workflow only from the readNdraft repository root.

## Preserve local work

1. Inspect `git status -sb` before running anything.
2. Treat modified and untracked files as user-owned. The test workflow must not
   stage, reset, delete, or rewrite them.
3. Confirm `.codex/config.toml` defines `mcp_servers.readndraft_dev` and launches
   the checkout through locked `uv` with `required = false` for ordinary work.

## Run the safe verification sequence

1. Confirm `uv` and `codex` are on `PATH` and Codex CLI authentication is
   available. Do not print authentication files or tokens.
2. Run the focused, CI-safe contract tests:

   ```console
   uv run --locked pytest tests/test_codex_dev_mcp_smoke.py
   ```

3. Confirm Codex resolves the repository-scoped entry:

   ```console
   codex -C . mcp get readndraft_dev --json
   ```

4. Run the local end-to-end smoke test:

   ```console
   uv run --locked python scripts/codex_dev_mcp_smoke.py
   ```

5. Require the exact success signal `Codex development MCP smoke test passed.`
   before reporting success.

The smoke script initializes the local MCP and checks its tool catalog without
calling account or mail tools. It then starts an ephemeral, read-only Codex
session with the development MCP temporarily required. Do not add this live
Codex invocation to GitHub Actions; CI should run only the contract tests.

## Handle failures narrowly

- If `uv run --locked` reports an unsynchronized environment, run
  `uv sync --locked --extra dev`, then retry once.
- If Codex does not load `readndraft_dev`, confirm repository trust, the project
  `.codex/config.toml`, and a fresh Codex session. Do not edit the user's global
  Codex configuration unless explicitly asked.
- If the MCP fails to initialize, run the focused test and inspect the local
  smoke script/configuration before changing application code.
- If Codex CLI or authentication is unavailable, report the live smoke test as
  unverified; do not weaken the test or claim success from unit tests alone.

## Keep live email tests separate

Do not call `list_accounts`, read mail, or create drafts as part of this skill's
default smoke test. If the user explicitly requests a real account/draft test,
switch to the `readndraft-email` skill, obtain the required exact details and
confirmation, and report that a draft is saved but never sent.
