# MCP migration for 0.11.0

The MCP and local broker contract moves to IPC 12. The endpoint name includes
the IPC version, so an IPC 12 frontend cannot accidentally reuse an older
broker.

## Structured errors

Every top-level RPC error and every nested mailbox, search, message, flag, and
move batch error is now a `SafeError` object:

```json
{
  "code": "rate_limited",
  "message": "account task rate limit exceeded",
  "scope": "account",
  "reason": "task_rate",
  "retry_after_seconds": 1
}
```

Clients must branch on `error.code`, not message text. `reason` and
`retry_after_seconds` are nullable. Never automatically retry
`outcome_unknown`; the write may have completed, so reconcile mailbox or draft
state first. A pre-execution `rate_limited` result remains a definite rejection.

## Task admission and concurrency

The broker now charges one token per top-level task for each distinct physical
IMAP account, regardless of whether the task contains one item or fifty. The
bucket starts with 120 tokens and refills at 2 tokens per second. Aliases with
the same normalized host, port, and exact username share this budget and the
two-session concurrency limit.

Global capacity remains eight IMAP workers plus sixteen accepted waiters.
Per-account sessions are granted in FIFO order before credentials are loaded;
waiting does not occupy an IMAP worker thread.

## Deadline and partial-result behavior

The broker request deadline remains 30 seconds, the server watchdog is 35
seconds, and the IPC client deadline remains 45 seconds. Completed batch items
are retained when the deadline expires. Unstarted items receive `timeout`;
interrupted writes whose result cannot be known receive `outcome_unknown`.

Broker health and `doctor` now expose only aggregate `resource_limits` and
`resource_usage`. These counters are memory-only and reset when the broker
restarts; they never contain account aliases, hosts, usernames, credentials, or
mail data.
