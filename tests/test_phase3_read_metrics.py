from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "phase3_read_metrics.py"
    spec = importlib.util.spec_from_file_location("phase3_read_metrics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase3_metrics_use_actual_selective_imap_path_and_private_output() -> None:
    result = _module().run(repetitions=1, fixture_mib=1, audit_histories=(2,))

    assert set(result) == {"read", "audit"}
    read = result["read"]
    assert set(read) == {"repetitions", "median", "range", "counters"}
    assert read["repetitions"] == 1
    assert set(read["median"]) == {
        "elapsed_ms", "peak_bytes", "active_thread_delta", "connections", "imap_commands", "transferred_bytes"
    }
    assert read["median"]["connections"] == 1
    assert read["median"]["imap_commands"] == 5
    assert read["median"]["transferred_bytes"] < 10_000
    assert read["counters"]["imap_command_counts"] == {
        "EXAMINE": 1,
        "LOGIN": 1,
        "LOGOUT": 1,
        "UID FETCH BODY SECTION": 1,
        "UID FETCH BODYSTRUCTURE": 1,
    }
    assert set(result["audit"]) == {"2"}
    audit = result["audit"]["2"]
    assert audit["median"]["history_events"] == 2
    assert audit["median"]["appended_events"] == 1
    assert "sender@example.invalid" not in str(result)
    assert "short benchmark text" not in str(result)


def test_reduction_is_computed_from_measured_bytes() -> None:
    module = _module()
    assert module._reduction({"median": {"transferred_bytes": 1000}}, {"median": {"transferred_bytes": 50}}) == 95.0
