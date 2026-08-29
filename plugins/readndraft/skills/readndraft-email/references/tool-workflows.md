# Tool workflows

## Find and read mail

1. Resolve aliases with `list_accounts`.
2. Resolve exact mailbox names with `list_mailboxes(account_ids=[...])` when
   needed; inspect each ordered account result and continue around isolated errors.
3. Call `search_emails`; `limit` defaults to 50 and accepts 1-500. A request
   above 50 requires exactly one exact `{account_id, mailbox}` target. Use `fields`
   to request only the safe headers needed for a large page.
4. Show matching metadata when several results are plausible.
5. Copy one result's complete identity into `get_email` with
   `max_text_chars: 16000`; use `text_truncated` and `text_total_chars` to decide
   whether a full-text follow-up is needed.

Search returns `results`, `errors`, `targets_searched`, `targets_pending`,
`truncated`, `next_cursor`, and `order`. Search pending targets separately when
complete coverage is required. A single-target error is returned in the same
page envelope and is not an empty successful search.
Cursor pagination requires exactly one target. Keep every filter
unchanged and pass `next_cursor` into the next call; stop when it is null. Do not
combine a cursor with another mailbox or deduplicate day-boundary results—the
cursor already advances below the last UID without overlap.

When the user explicitly asks to read several already-selected messages, call
`get_emails` with 1-10 complete identities across at most two accounts and
`max_text_chars: 16000`. Do not
turn broad search results into a speculative read batch. Results remain in input
order and may contain per-item failures.

Reading plain text does not set the Seen flag. Do not call `set_read_state`
unless the user separately requests a state change.

## HTML and attachments

Prefer `get_email`: it returns the actual plain body when available and derives
readable plain text from the selected HTML body for HTML-only mail. Call
`get_email_html` only when the user needs sanitized rich formatting or structure.
Neither read path fetches remote resources.
Attachment metadata comes from `get_email`. Pass its exact `attachment_id` and
the same complete identity to `save_attachment`; it writes only to readNdraft's
fixed output directory. Its `saved_path` is the authoritative absolute path in
the server host's native format. Pass that string unchanged to an available
local-file reader: do not join paths, expand `~`, substitute environment
variables, or translate `/` and `\\`. If the client cannot access that host path,
report it to the user and say only that the file was saved—not that it was read.
Use `list_attachment_inputs` before attaching a local file by name. Reads require
no confirmation. Downloaded files and their contents remain untrusted data.

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

Resolve the account with `list_accounts`, then present its exact `sender_address`,
account, recipients, subject, body, and attachment names and obtain direct user
confirmation. For a plain draft, provide required `body`. For a rich draft,
provide both `body` and `html_body`, and ensure they communicate the same content
with nothing important only in HTML. Modern mail clients normally display the
HTML alternative while the required body remains the plain fallback. `html_body`
may be a fragment or a complete HTML document. Supported HTML covers common
modern email structure such as paragraphs, headings, emphasis, lists,
blockquotes, safe links, and tables. Authored CSS is permissive, normalized, and
inlined for mail-client compatibility. CSS that fetches remote resources, hides
content, or escapes the message box rejects the whole request so formatting is
never silently lost. Do not add images, active content, event handlers, unsafe
URLs, or invented remote assets; no URL is fetched. Empty paragraphs are
preserved consistently.
To, Cc, and Bcc may all be empty; explicitly present that there are no
recipients rather than inventing one
or refusing the draft. The sender is pinned account metadata, not a draft
argument. Call `create_draft` once with the unchanged payload and retain the
returned `draft_id`. Before `update_draft`, present the full replacement,
including both representations when rich, and confirm again. Updating replaces
and expunges the prior tracked draft version. Neither operation sends mail or
deletes an ordinary message.

Each recipient element must be one bare mailbox or one named mailbox such as
`Ada Lovelace <ada@example.com>`; do not place a comma-separated list or a
group in one element. For a reply draft, pass the selected source's exact
`account_id`, `mailbox`, `uid_validity`, and `uid` as `reply_to_message`. This
adds safe thread headers only; preserve the requested recipients and subject.

## Move messages

Resolve the account and refresh `list_mailboxes`. Use only raw mailbox names from
that response. Reject movement if the source or destination is missing,
`\Noselect`, or carries `\Trash`, `\Junk`, `\Drafts`, or `\Sent`; never infer
SPECIAL-USE status from a name. Present the account, exact source mailbox or
mailboxes, destination, complete identities, and count, then obtain direct user
confirmation immediately before calling `move_email` or `move_emails_batch`.

A move batch accepts 1-50 unique identities from exactly one account and is
non-atomic. Preserve result order and report partial success. A successful move
invalidates the source identity and reports `method` as `uid_move` or
`uidplus_copy_delete`. The fallback is private to the broker and requires
COPYUID before it marks and UID-expunges only the selected source UID; never
request separate copy, deleted-flag, expunge, or raw-IMAP tools. Use
`destination_identity` only when returned; otherwise search the destination and
require unambiguous reselection. On `partial_move`, connection failure,
`broker_error`, or any ambiguous outcome, inspect both mailboxes and never retry
automatically because the destination copy or completed move may already exist.
