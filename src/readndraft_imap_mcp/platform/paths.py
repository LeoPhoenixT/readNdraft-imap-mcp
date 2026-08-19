from __future__ import annotations

import os
import secrets
import sys
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path

from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    state_dir: Path
    runtime_dir: Path
    data_dir: Path | None = None

    @property
    def effective_data_dir(self) -> Path:
        return self.data_dir or self.state_dir.parent / "data"

    @property
    def attachment_input_dir(self) -> Path:
        return self.effective_data_dir / "attachments" / "input"

    @property
    def attachment_output_dir(self) -> Path:
        return self.effective_data_dir / "attachments" / "output"

    @property
    def accounts_file(self) -> Path:
        return self.config_dir / "accounts.json"

    @property
    def ipc_key_file(self) -> Path:
        return self.state_dir / "ipc.key"

    @property
    def startup_lock_file(self) -> Path:
        return self.runtime_dir / f"broker-v{IPC_PROTOCOL_VERSION}-startup.lock"

    @property
    def broker_instance_lock_file(self) -> Path:
        return self.runtime_dir / f"broker-v{IPC_PROTOCOL_VERSION}-instance.lock"

    @property
    def draft_dir(self) -> Path:
        return self.state_dir / "drafts"

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def ipc_address(self) -> str:
        if sys.platform == "win32":
            normalized_runtime = os.path.normcase(
                str(self.runtime_dir.resolve(strict=False))
            )
            namespace = sha256(normalized_runtime.encode("utf-8")).hexdigest()[:16]
            return (
                rf"\\.\pipe\readndraft-broker-v{IPC_PROTOCOL_VERSION}-{namespace}"
            )
        return str(self.runtime_dir / f"broker-v{IPC_PROTOCOL_VERSION}.sock")

    def ensure_private(self) -> None:
        for path in (self.config_dir, self.state_dir, self.runtime_dir, self.effective_data_dir):
            path.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(path, 0o700)
        for path in (self.draft_dir, self.attachment_input_dir, self.attachment_output_dir):
            path.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(path, 0o700)

    def load_or_create_ipc_key(self) -> bytes:
        self.ensure_private()
        try:
            key = self.ipc_key_file.read_bytes()
        except FileNotFoundError:
            temporary = self.ipc_key_file.with_suffix(
                f".{secrets.token_hex(8)}.tmp"
            )
            temporary.write_bytes(secrets.token_bytes(32))
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            try:
                os.link(temporary, self.ipc_key_file)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
            key = self.ipc_key_file.read_bytes()
        if len(key) != 32:
            raise RuntimeError("invalid IPC authentication key")
        if os.name != "nt" and self.ipc_key_file.stat().st_mode & 0o077:
            raise PermissionError("IPC authentication key must not be group/world accessible")
        return key


def current_app_paths() -> AppPaths:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise RuntimeError("LOCALAPPDATA is required on Windows")
        root = Path(base) / "readNdraft"
        return AppPaths(root / "config", root / "state", root / "runtime", root)
    if sys.platform.startswith("linux"):
        home = Path.home()
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "readndraft"
        state = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state")) / "readndraft"
        runtime_base = os.environ.get("XDG_RUNTIME_DIR")
        runtime = Path(runtime_base) / "readndraft" if runtime_base else state / "runtime"
        data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "readndraft"
        return AppPaths(config, state, runtime, data)
    raise RuntimeError("readNdraft supports only Windows and Linux")
