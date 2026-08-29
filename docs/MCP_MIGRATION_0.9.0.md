# MCP migration: 0.8.x to 0.9.0

The MCP read-only tool contract is intentionally breaking in 0.9.0.

## Search targets

`search_emails` no longer accepts the Cartesian `accounts` and `mailboxes`
parameters. Pass one to twenty explicit, unique targets in the desired order:

```json
{"targets": [{"account_id": "personal", "mailbox": "INBOX"}]}
```

Limits above 50 and cursor pagination require exactly one target.

## Mailbox discovery

`list_mailboxes` now accepts `account_ids`, a unique list of one to ten account
aliases. It returns ordered per-account results with `ok`, `mailboxes`, and a
safe `error`, so a failed account does not discard successful discoveries.

## Text previews

`get_email` and `get_emails` accept optional `max_text_chars` from 1 to 100000.
When omitted or `null`, the bounded complete plain text is returned. Every
successful message now includes `text_total_chars` and `text_truncated`; agents
should normally start with a 16000-character preview and request full text only
after inspecting those fields.
