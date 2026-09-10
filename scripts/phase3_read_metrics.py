"""Private-data-free Phase 3 read and audit benchmark.

The workload uses the selected checkout's real ``ImapClient`` against a
deterministic in-memory IMAP server. It contains a short text part and an
unrelated attachment, so returned payload bytes come from IMAP responses
rather than a hand-written metric. JSON output contains counters and timing
aggregates only; it never includes account, message, or header data.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import tracemalloc
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

DEFAULT_FIXTURE_MIB = 10
DEFAULT_REPETITIONS = 7
DEFAULT_AUDIT_HISTORIES = (10, 100, 500)


def _select_source_root(source_root: str | Path | None) -> None:
    """Put an exported checkout ahead of this checkout before package imports."""

    if source_root is None:
        return
    root = Path(source_root).resolve()
    package_root = root / "src" if (root / "src").is_dir() else root
    if not (package_root / "readndraft_imap_mcp").is_dir():
        raise ValueError("source root must contain src/readndraft_imap_mcp")
    sys.path.insert(0, str(package_root))


class _SyntheticSocket:
    def settimeout(self, timeout: float) -> None:
        del timeout


class _SyntheticConnection:
    """Minimal deterministic IMAP server that counts actual response bytes."""

    connections = 0
    returned_bytes = 0
    command_counts: Counter[str] = Counter()
    fixture_size = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        type(self).connections += 1
        self.sock = _SyntheticSocket()

    @classmethod
    def reset(cls, fixture_size: int) -> None:
        cls.connections = 0
        cls.returned_bytes = 0
        cls.command_counts = Counter()
        cls.fixture_size = fixture_size

    @classmethod
    def _record(cls, command: str, data: list[object]) -> tuple[str, list[object]]:
        cls.command_counts[command] += 1
        cls.returned_bytes += sum(
            len(value)
            for item in data
            for value in (item if isinstance(item, tuple) else (item,))
            if isinstance(value, bytes)
        )
        return "OK", data

    def login(self, username: str, password: str) -> tuple[str, list[object]]:
        del username, password
        return self._record("LOGIN", [])

    def logout(self) -> tuple[str, list[object]]:
        return self._record("LOGOUT", [])

    def shutdown(self) -> None:
        return None

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[object]]:
        del mailbox
        return self._record("EXAMINE" if readonly else "SELECT", [])

    def response(self, name: str) -> tuple[str, list[bytes]]:
        return (name, [b"42"]) if name == "UIDVALIDITY" else (name, [])

    @classmethod
    def _message_size(cls) -> int:
        return len(_raw_message(0)) + cls.fixture_size

    def uid(self, command: str, uid: str, query: str) -> tuple[str, list[object]]:
        assert (command, uid) == ("FETCH", "7")
        if "BODYSTRUCTURE" in query:
            headers = b"Subject: benchmark\r\nFrom: sender@example.invalid\r\n\r\n"
            structure = (
                b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL) '
                + b'("APPLICATION" "OCTET-STREAM" ("NAME" "fixture.bin") NIL NIL "BASE64" '
                + str(type(self).fixture_size).encode()
                + b' NIL ("ATTACHMENT" ("FILENAME" "fixture.bin")) NIL NIL) "MIXED")'
            )
            head = (
                b"1 (UID 7 FLAGS () RFC822.SIZE "
                + str(type(self)._message_size()).encode()
                + b" BODYSTRUCTURE "
                + structure
                + b" BODY[HEADER.FIELDS] {"
                + str(len(headers)).encode()
                + b"}"
            )
            return self._record("UID FETCH BODYSTRUCTURE", [(head, headers), b")"])
        if query == "(UID FLAGS RFC822.SIZE)":
            head = b"1 (UID 7 FLAGS () RFC822.SIZE " + str(type(self)._message_size()).encode() + b")"
            return self._record("UID FETCH METADATA", [(head, b"")])
        if "BODY.PEEK[1]" in query:
            body = b"short benchmark text\r\n"
            head = b"1 (UID 7 BODY[1] {" + str(len(body)).encode() + b"}"
            return self._record("UID FETCH BODY SECTION", [(head, body), b")"])
        if "BODY.PEEK[]" in query:
            raw = _raw_message(type(self).fixture_size)
            head = b"1 (UID 7 FLAGS () BODY[] {" + str(len(raw)).encode() + b"}"
            return self._record("UID FETCH FULL MESSAGE", [(head, raw), b")"])
        raise AssertionError(f"unexpected synthetic IMAP query: {query!r}")


def _raw_message(attachment_bytes: int) -> bytes:
    boundary = b"phase3-benchmark-boundary"
    return (
        b"Subject: benchmark\r\nFrom: sender@example.invalid\r\n"
        + b"Content-Type: multipart/mixed; boundary=\""
        + boundary
        + b"\"\r\n\r\n--"
        + boundary
        + b"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nshort benchmark text\r\n--"
        + boundary
        + b"\r\nContent-Type: application/octet-stream; name=fixture.bin\r\n"
        + b"Content-Disposition: attachment; filename=fixture.bin\r\n"
        + b"Content-Transfer-Encoding: base64\r\n\r\n"
        + b"A" * attachment_bytes
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )


def _summary(samples: list[dict[str, object]]) -> dict[str, object]:
    numeric_keys = [key for key, value in samples[0].items() if isinstance(value, (int, float))]
    fixed_keys = [key for key in samples[0] if key not in numeric_keys]
    return {
        "repetitions": len(samples),
        "median": {key: statistics.median(sample[key] for sample in samples) for key in numeric_keys},
        "range": {
            key: [min(sample[key] for sample in samples), max(sample[key] for sample in samples)]
            for key in numeric_keys
        },
        "counters": {key: samples[0][key] for key in fixed_keys},
    }


def _read_once(fixture_mib: int) -> dict[str, object]:
    from readndraft_imap_mcp.broker.accounts import AccountConfig
    from readndraft_imap_mcp.imap import client as client_module
    from readndraft_imap_mcp.imap.client import ImapClient
    from readndraft_imap_mcp.imap.models import MessageIdentity

    _SyntheticConnection.reset(fixture_mib * 1024 * 1024)
    original_connection = client_module.imaplib.IMAP4_SSL
    client_module.imaplib.IMAP4_SSL = _SyntheticConnection
    before_threads = threading.active_count()
    tracemalloc.start()
    started = perf_counter()
    try:
        account = AccountConfig("benchmark", "synthetic.invalid", 993, "benchmark@example.invalid")
        with ImapClient(account, "synthetic") as client:
            result = client.get_message(MessageIdentity("benchmark", "INBOX", "42", "7"))
        assert result.identity.uid == "7"
    finally:
        elapsed_ms = (perf_counter() - started) * 1000
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        client_module.imaplib.IMAP4_SSL = original_connection
    return {
        "elapsed_ms": elapsed_ms,
        "peak_bytes": peak_bytes,
        "active_thread_delta": threading.active_count() - before_threads,
        "connections": _SyntheticConnection.connections,
        "imap_commands": sum(_SyntheticConnection.command_counts.values()),
        "imap_command_counts": dict(sorted(_SyntheticConnection.command_counts.items())),
        "transferred_bytes": _SyntheticConnection.returned_bytes,
    }


def run_read_metrics(
    repetitions: int = DEFAULT_REPETITIONS, fixture_mib: int = DEFAULT_FIXTURE_MIB
) -> dict[str, object]:
    if repetitions < 1 or fixture_mib < 1:
        raise ValueError("repetitions and fixture_mib must be positive")
    return _summary([_read_once(fixture_mib) for _ in range(repetitions)])


def _audit_event(index: int):
    from readndraft_imap_mcp.audit import AuditEvent

    return AuditEvent(
        timestamp="2026-01-01T00:00:00+00:00", operation="set_star", account_id="benchmark", mailbox="INBOX",
        uid=str(index + 1), request_size=0, approval_required=False, approval_result="not_required", success=True,
        duration_ms=0,
    )


def _audit_once(history: int) -> dict[str, object]:
    from readndraft_imap_mcp.audit import JsonlAuditSink

    with tempfile.TemporaryDirectory(prefix="readndraft-phase3-") as directory:
        sink = JsonlAuditSink((Path(directory) / "audit.jsonl").resolve())
        for index in range(history):
            sink._record_sync(_audit_event(index))
        tracemalloc.start()
        before_threads = threading.active_count()
        started = perf_counter()
        sink._record_sync(_audit_event(history))
        elapsed_ms = (perf_counter() - started) * 1000
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return {
        "history_events": history, "elapsed_ms": elapsed_ms, "peak_bytes": peak_bytes,
        "active_thread_delta": threading.active_count() - before_threads, "appended_events": 1,
    }


def run_audit_scaling(
    repetitions: int = DEFAULT_REPETITIONS, histories: tuple[int, ...] = DEFAULT_AUDIT_HISTORIES
) -> dict[str, object]:
    if repetitions < 1 or not histories or any(history < 0 for history in histories):
        raise ValueError("repetitions must be positive and histories must be non-negative")
    return {str(history): _summary([_audit_once(history) for _ in range(repetitions)]) for history in histories}


def run(
    repetitions: int = DEFAULT_REPETITIONS,
    fixture_mib: int = DEFAULT_FIXTURE_MIB,
    audit_histories: tuple[int, ...] = DEFAULT_AUDIT_HISTORIES,
    source_root: str | Path | None = None,
) -> dict[str, object]:
    _select_source_root(source_root)
    return {
        "read": run_read_metrics(repetitions, fixture_mib),
        "audit": run_audit_scaling(repetitions, audit_histories),
    }


def _reduction(baseline: dict[str, object], current: dict[str, object]) -> float:
    baseline_bytes = float(baseline["median"]["transferred_bytes"])  # type: ignore[index]
    current_bytes = float(current["median"]["transferred_bytes"])  # type: ignore[index]
    return 100 * (1 - current_bytes / baseline_bytes) if baseline_bytes else 0.0


def _run_exported_read_metrics(source_root: Path, repetitions: int, fixture_mib: int) -> dict[str, object]:
    """Run an exported checkout in a fresh interpreter to avoid module reuse."""

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--source-root",
            str(source_root.resolve()),
            "--repetitions",
            str(repetitions),
            "--fixture-mib",
            str(fixture_mib),
            "--audit-histories",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)["current"]["read"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, help="exported checkout used for this run")
    parser.add_argument("--compare-root", type=Path, help="exported baseline checkout to compare with this checkout")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--fixture-mib", type=int, default=DEFAULT_FIXTURE_MIB)
    parser.add_argument("--audit-histories", type=int, nargs="+", default=DEFAULT_AUDIT_HISTORIES)
    args = parser.parse_args()
    if args.source_root and args.compare_root:
        raise ValueError("--source-root and --compare-root cannot be combined")
    current = run(args.repetitions, args.fixture_mib, tuple(args.audit_histories), args.source_root)
    result: dict[str, Any] = {"current": current}
    if args.compare_root:
        baseline = _run_exported_read_metrics(args.compare_root, args.repetitions, args.fixture_mib)
        result["comparison"] = {
            "baseline_read": baseline,
            "current_read": current["read"],
            "transferred_bytes_reduction_percent": _reduction(baseline, current["read"]),
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
