"""Local resource budgets, not provider quotas.

Defaults retain eight IMAP workers while limiting a hot physical account to
four queued sessions. IPC budgets leave separate space for leases and control
requests. All values can be injected for deployments and synthetic tests.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapacitySettings:
    task_bucket_capacity: int = 120
    task_refill_per_second: float = 2.0
    account_sessions: int = 2
    imap_workers: int = 8
    waiting_imap_work: int = 16
    waiting_account_work: int = 4
    credential_workers: int = 2
    waiting_credential_work: int = 16
    ipc_connections: int = 48
    ipc_rpc: int = 24
    ipc_leases: int = 16
    ipc_control: int = 2
    ipc_handshake_seconds: float = 5.0
    ipc_frame_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            continuous = name.endswith("_seconds") or name == "task_refill_per_second"
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (not continuous and not isinstance(value, int))
                or value < (0 if name.startswith("waiting_") else 0 if continuous else 1)
                or (continuous and value <= 0)
            ):
                raise ValueError(f"invalid capacity setting: {name}")


DEFAULT_CAPACITY = CapacitySettings()
