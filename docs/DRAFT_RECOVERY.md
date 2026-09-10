# Recovering interrupted draft updates

`readndraft-imap-mcp drafts list` reports an outstanding operation journal and
its local `operation_id`. A journal with one verified replacement UID can be
adopted with `readndraft-imap-mcp drafts repair --draft-id ID`.

If `list` reports that the operation marker is absent, inspect the Drafts
mailbox first. To clear only that local operation journal, copy the displayed
identifier and run:

```console
readndraft-imap-mcp drafts repair --draft-id ID --clear-operation OPERATION_ID
```

The command refuses a wrong, ambiguous, or still-present marker. It does not
append, expunge, or delete any server message. A later draft update remains a
separate confirmed operation.
