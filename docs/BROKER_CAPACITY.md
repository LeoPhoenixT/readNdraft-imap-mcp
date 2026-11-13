# Broker capacity and IPC 13 migration

This unreleased change uses IPC 13 without changing the package version. The
versioned endpoint prevents a newer frontend from reusing an IPC 12 broker.
Update both processes together. MCP tool names and input/output schemas remain
unchanged; the internal health response and structured error reasons expand.

## Task admission

The task bucket capacity is 120, with a refill of two tokens per second. A
single top-level task consumes one token per distinct physical account, including
a batch of fifty items. Multi-account admission remains atomic. Host names are
normalized, ports are exact, and usernames remain case-sensitive; aliases share
both task and session budgets.

Python callers should use `AccountRequestQuota(task_bucket_capacity=...)`.
`requests_per_minute=...` remains a compatibility alias for bucket capacity,
not a per-minute window. Passing both names raises `ValueError`.

## Local execution budgets

`broker/settings.py` provides immutable `CapacitySettings`, injectable into
`BrokerService` and `BrokerRpcServer`. These are local resource/memory budgets,
not claims about any mail provider's quotas:

| Resource | Default | Unit |
| --- | ---: | --- |
| IMAP workers | 8 | Physical executor threads |
| Additional admitted IMAP work | 16 | Lifecycle reservations and scheduler waiters beyond eight |
| Account sessions | 2 | Concurrent client lifecycles per physical account |
| Account waiting work | 4 | Queued lifecycles per physical account |
| Credential workers | 2 | Physical credential operations |
| Credential waiting work | 16 | Queued credential operations |
| IPC connections | 48 | All accepted connections, including authentication |
| Ordinary RPC | 24 | Authenticated ordinary operations |
| Frontend leases | 16 | Authenticated persistent connections |
| Control RPC | 2 | Authenticated health/shutdown operations |
| Authentication deadline | 5 seconds | Complete handshake |
| Initial-frame deadline | 5 seconds | Entire header and payload after authentication |

Account FIFO queues are visited round-robin. Account and global lifecycle
reservations are granted together. A queued hot-account request cannot reserve
a global execution position while it waits for an account session. Account
waiters and granted lifecycles share the total budget of 24; a granted lifecycle
may have work queued in the eight-thread executor. This also permits an
interleaved multi-account batch to retain its clients while waiting for ordered
response-budget allocation, including with only one physical worker.

Four account waiters provide two additional waves at the two-session default;
sixteen global waiting positions provide two waves beyond eight workers. The
synthetic scheduler tests check FIFO/round-robin order, eight simultaneous
physical execution grants in the standalone scheduler, queue saturation, and a
24-call hot-account burst that leaves space for an idle account. Batch tests
check interleaved account reads with one physical worker. These are bounded-load
regressions, not a throughput benchmark or provider-limit calibration.

Credential execution has its own bounded pool. Expired/cancelled queued work
checks its request guard before touching credentials. A response timeout or
caller cancellation cannot cancel physical credential work or release its
lifecycle reservation early. Legacy asynchronous credential adapters run on an
owned worker loop that also drains their `asyncio.to_thread` work; the keyring
adapter runs directly on a credential worker. Shutdown rejects new work and
drains ownership; an operating-system keyring call that never returns cannot
be forcibly killed safely. Such a call retains its bounded worker/reservation.

## IPC admission and health

Accepting a connection does not authenticate it on the accept thread. A bounded
connection worker authenticates it under the broker-wide deadline watchdog.
The watchdog aborts stalled I/O, including partial initial frames. Unix socket
shutdown and Windows named-pipe cancellation interrupt pending operations; the
watchdog has one thread for the whole broker. Lease monitoring uses readiness
and EOF instead of receiving another potentially incomplete frame.

The 48-connection total leaves six positions beyond the ordinary-RPC, lease,
and control budgets combined. Ordinary RPC or lease saturation therefore leaves
room for health/shutdown. A flood of unauthenticated connections can still fill
the total until deadlines expire; requests cannot be classified as control
before they authenticate and provide a valid frame. Saturated unauthenticated
connections are closed without a response. Authenticated category rejections
return `rate_limited` with reason `ipc_connection_capacity`.

New safe reasons are `account_queue_capacity`, `credential_capacity`, and
`ipc_connection_capacity`. Existing `task_rate`, `session_queue_timeout`, and
`imap_worker_capacity` retain their meaning. `resource_usage` adds scheduler
running/waiting lifecycle counts, credential running/queued counts, IPC total
and category usage, and IPC rejection/deadline counters. `active_sessions` and
`queued_session_requests` remain compatibility fields. All data is aggregate,
in-memory, and contains no account identifiers or credential/mail contents.
Completed batch items and `outcome_unknown` write semantics remain unchanged.

The transport regression tests exercise the current platform's real Unix
sockets or Windows named pipes with synthetic keys/data. Unix partial byte-stream
injection is skipped for message-mode named pipes. Windows cancellation and the
Linux runtime must also pass their native CI matrix; passing Unix tests on macOS
does not establish either platform's support.
