# Tool workflows

## Find and read mail

1. Resolve aliases with `list_accounts`.
2. Resolve exact mailbox names with `list_mailboxes` when needed.
3. Call `search_emails`; `limit` defaults to 50 and accepts 1-500. A request
   above 50 requires exactly one account and raw mailbox `name`. Use `fields`
   to request only the safe headers needed for a large page.
4. Show matching metadata when several results are plausible.
5. Copy one result's complete identity into `get_email`.

Search returns `results`, `errors`, `targets_searched`, `targets_pending`,
`truncated`, `next_cursor`, and `order`. Search pending targets separately when
complete coverage is required. A single-target error is returned in the same
page envelope and is not an empty successful search.
Cursor pagination requires exactly one account and mailbox. Keep every filter
unchanged and pass `next_cursor` into the next call; stop when it is null. Do not
combine a cursor with another mailbox or deduplicate day-boundary results—the
cursor already advances below the last UID without overlap.

When the user explicitly asks to read several already-selected messages, call
`get_emails` with 1-10 complete identities across at most two accounts. Do not
turn broad search results into a speculative read batch. Results remain in input
order and may contain per-item failures.

Reading plain text does not set the Seen flag. Do not call `set_read_state`
unless the user separately requests a state change.

## HTML and attachments

Prefer `get_email`. Call `get_email_html` only for HTML the user requested.
Attachment metadata comes from `get_email`. Pass its exact `attachment_id` and
the same complete identity to `save_attachment`; it writes only to readNdraft's
fixed output directory. Use `list_attachment_inputs` before attaching a local
file by name. Reads require no confirmation.

## Star and read state

Use the complete identity from a current search or read. Present the identity
and intended state, then obtain direct user confirmation before calling only the
semantic mutation requested. Do not emulate arbitrary IMAP flag editing.

For multiple user-selected messages, prefer `set_star_batch` or
`set_read_state_batch` over repeated single calls. A batch accepts 1-50 unique
identities across at most three accounts and one desired state. Tell the user
the intended count and state and obtain direct user confirmation. The operation
is non-atomic: report each failure, retain successful results, and retry only
failed identities the user still wants changed after confirming the new batch.
Never retry an ambiguous connection or `broker_error` automatically.

## Draft creation and update

Present the exact account, recipients, subject, body, and attachment names and
obtain direct user confirmation. Call `create_draft` once with the unchanged
payload and retain the returned `draft_id`. Before `update_draft`, present the
full replacement and confirm again. Neither operation sends mail.
