## Summary

Describe the narrowly scoped change and why it is needed.

## Security impact

Explain whether the change affects capabilities, credentials, IPC, IMAP access,
MIME parsing, attachments, drafts, audit records, or untrusted-content handling.

## Verification

- [ ] Relevant tests were added or updated.
- [ ] `uv run --locked pytest` passes.
- [ ] `uv run --locked python scripts/security_check.py` passes.
- [ ] `uv run --locked python scripts/license_check.py` passes.
- [ ] Public documentation was updated when behavior changed.
- [ ] No credentials, real mail, account metadata, IPC keys, or private state are included.

## Capability boundary

- [ ] This change does not add send, submit, ordinary-message deletion, movement,
      raw IMAP, arbitrary flags, account administration, or credential retrieval
      to the MCP tool surface.
