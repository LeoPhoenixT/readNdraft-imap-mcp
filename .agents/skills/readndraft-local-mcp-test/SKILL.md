---
name: readndraft-local-mcp-test
description: Configure, refresh, and verify the repository-local readNdraft development MCP and its Codex integration without exposing account or mail data. Use when asked to set up or test `readndraft_dev`, run the local MCP smoke test, make Codex CLI use this checkout, refresh the local broker after source changes, diagnose stale local behavior, or validate local MCP changes before commit or release.
---

# Test the readNdraft Local MCP

Run this workflow only from the readNdraft repository root.

## Preserve local work

1. Inspect `git status -sb` before running anything.
2. Treat modified and untracked files as user-owned. The test workflow must not
   stage, reset, delete, or rewrite them.
3. Confirm `.codex/config.toml` defines `mcp_servers.readndraft_dev` and launches
   the checkout through locked `uv` with `required = false` for ordinary work.

## Confirm the repository configuration

Use this repository-scoped configuration:

```toml
[mcp_servers.readndraft_dev]
command = "uv"
args = ["run", "--locked", "--no-sync", "readndraft-imap-mcp", "mcp"]
cwd = "."
startup_timeout_sec = 10
tool_timeout_sec = 60
required = false
default_tools_approval_mode = "approve"
```

Run Codex with `codex -C .` from the repository root; MCP `cwd` is resolved from
that Codex working directory, so use `cwd = "."`. Confirm the resolved entry
with `codex -C . mcp get readndraft_dev --json`; do not edit the user's global
Codex configuration. If a required MCP session closes during initialization,
inspect the resolved `cwd` first and temporarily pin it to the absolute
repository root to distinguish a path problem from a server problem.

## Refresh changed local code

Do not rebuild or reinstall for ordinary Python source edits. `uv run` executes
this checkout. Run `uv sync --locked --extra dev` only when the environment is
missing, unsynchronized, or dependency/lock metadata changed.

Distinguish the two processes before testing:

- A new Codex CLI session starts a fresh MCP frontend from the checkout.
- The frontend reuses any healthy broker at the current IPC protocol endpoint.
- Therefore, frontend-only changes are picked up by a new CLI session, but
  broker-side changes under `broker/`, `imap/`, `mime/`, `drafts/`, or shared
  code require a fresh broker process.

Before refreshing the broker, close or disconnect active readNdraft MCP
frontends when practical so their leases end. On Windows, inspect only processes
whose command line contains `readndraft_imap_mcp.broker.daemon`; do not stop a
process based only on `python.exe`. Show the exact PID, executable, and command
line, explain that connected clients will briefly lose readNdraft access, and
obtain direct user confirmation before stopping that exact process. Never stop
unrelated Python or Codex processes.

After the old broker exits, start a new Codex CLI session from the repository:

```console
codex -C .
```

The `readndraft_dev` launcher then starts a broker using the updated checkout.
Verify the broker through the focused behavior that changed; a tool-catalog
smoke test alone cannot prove broker implementation code was refreshed.

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
- If a source change still behaves like the old implementation, determine
  whether it is broker-side. A fresh Codex CLI frontend does not replace a
  healthy broker at the same IPC protocol endpoint; follow the guarded broker
  refresh procedure above.
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
confirmation, and report that a draft is saved but never sent. Run that live
test only after refreshing broker-side code when applicable.
