# Performance measurements

`scripts/phase3_read_metrics.py` is a private-data-free, reproducible Phase 3 benchmark. It drives the checkout's real `ImapClient` through a deterministic in-memory IMAP connection. The fixture contains a short text body and a 10 MiB unrelated attachment. The report contains only aggregate timing, allocation, thread, connection, command, and transferred-byte counters.

Run the current checkout with seven repetitions (the default):

```powershell
uv run --locked python scripts/phase3_read_metrics.py
```

To measure the original full-message implementation at commit `65a4438`, make a disposable export outside this repository, then run the same script with that source root. `git archive` is read-only with respect to the working tree:

```powershell
$baseline = Join-Path $env:TEMP "readndraft-65a4438"
New-Item -ItemType Directory -Force $baseline | Out-Null
git archive 65a4438 | tar -x -C $baseline
uv run --locked python scripts/phase3_read_metrics.py --source-root $baseline
```

To emit both measurements and the byte-reduction percentage in one report:

```powershell
uv run --locked python scripts/phase3_read_metrics.py --compare-root $baseline
```

The current implementation should issue `EXAMINE`, `UID FETCH BODYSTRUCTURE`, and `UID FETCH` for the selected text section. The baseline issues `EXAMINE`, metadata `UID FETCH`, and full-message `UID FETCH`. With the standard 10 MiB fixture, compare `read.median.transferred_bytes`; the selective-path acceptance target is at least 90% less than the baseline. Do not infer a timing speedup without recording both runs on the same machine.

The same report measures one real `JsonlAuditSink._record_sync` append after histories of 10, 100, and 500 synthetic safe audit events. It reports the median and min/max append duration and `tracemalloc` peak. The current audit implementation verifies the complete chain on every append, so an append is O(N) in history length and constructing a history is O(N²). That behavior is intentional for the present integrity design; optimizing it is deferred.
