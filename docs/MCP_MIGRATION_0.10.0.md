# MCP migration: 0.9.x to 0.10.0

Version 0.10.0 changes the local IPC contract to version 11. Fully close and
restart MCP clients after updating so the frontend starts a compatible broker.
Tool names remain unchanged.

## Search completion and cursors

`search_emails` now returns `target_statuses` in the same order as the requested
targets. Each target is `complete`, `partial`, `error`, or `pending`. A partial
target includes its own cursor; continue it in a later one-target request using
the same filters. An empty result proves that nothing matched only when the
target status is `complete`.

Attachment-filename search now checks bounded MIME BODYSTRUCTURE metadata rather
than a top-level `Content-Disposition` header. One request performs at most 20
server searches and inspects at most 500 MIME candidates. A partial status and
cursor indicate that more candidates remain.

The existing `errors`, `targets_searched`, `targets_pending`, `truncated`, and
single-target `next_cursor` fields remain available for compatibility.

## Selective reads and attachment sizes

When the server provides a usable BODYSTRUCTURE, `get_email`, `get_email_html`,
and `save_attachment` fetch only the required MIME section. Complex or malformed
structures retain the bounded full-message fallback.

Attachment metadata now reports nullable decoded `size` and nullable
transfer-encoded `encoded_size`. The decoded size becomes exact after the
selected attachment is downloaded; `encoded_size` is populated when exact wire
metadata is available.

## Draft update recovery

Draft updates use a per-draft process lock and a durable operation journal.
Callers may receive:

- `draft_busy` when another update owns the draft lock;
- `recovery_required` when the journal needs deliberate recovery;
- `outcome_unknown` when a write response was lost and automatic retry is unsafe.

Never automatically retry these outcomes. Use the human-only draft recovery
commands documented in [DRAFT_RECOVERY.md](DRAFT_RECOVERY.md). A verified absent
operation journal can be cleared only with the exact operation ID; this does not
delete a server message or perform another update.

## Resource limits

Read requests share one end-to-end deadline. The broker bounds active and queued
IMAP work, and batch reads preserve aggregate source and text budgets while
allowing safe work across accounts to overlap.
