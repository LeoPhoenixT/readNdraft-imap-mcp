from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from readndraft_imap_mcp.platform.paths import AppPaths
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )


@pytest.fixture
def long_runtime_paths(tmp_path):
    paths = _paths(tmp_path / ("long-runtime-" * 15))
    directory = Path(paths.ipc_address).parent
    yield paths
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        directory.rmdir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX Unix socket paths")
@pytest.mark.parametrize("character", ["a", "郵"])
@pytest.mark.parametrize("length", [103, 104])
def test_unix_socket_path_limit_uses_encoded_bytes(character, length):
    filename = f"broker-v{IPC_PROTOCOL_VERSION}.sock"
    available = length - len(os.fsencode(filename)) - 2
    width = len(os.fsencode(character))
    runtime = Path("/" + character * (available // width) + "a" * (available % width))
    paths = AppPaths(Path("/config"), Path("/state"), runtime)
    original = runtime / filename
    assert len(os.fsencode(original)) == length
    if length == 103:
        assert paths.ipc_address == str(original)
    else:
        assert Path(paths.ipc_address).parent != runtime
        assert len(os.fsencode(paths.ipc_address)) <= 103


@pytest.mark.skipif(os.name == "nt", reason="POSIX Unix sockets")
def test_long_runtime_socket_can_connect_and_keeps_state_and_locks(long_runtime_paths):
    paths = long_runtime_paths
    key = paths.load_or_create_ipc_key()
    directory = Path(paths.ipc_address).parent
    assert directory.parent == Path("/tmp")
    assert directory.stat().st_uid == os.getuid()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert paths.ipc_key_file.parent == paths.state_dir
    assert paths.startup_lock_file.parent == paths.runtime_dir
    assert paths.broker_instance_lock_file.parent == paths.runtime_dir
    assert paths.load_or_create_ipc_key() == key
    assert paths.ipc_address == _paths(paths.runtime_dir.parent).ipc_address
    assert paths.ipc_address != _paths(paths.runtime_dir.parent / "other").ipc_address
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(paths.ipc_address)
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(paths.ipc_address)
                connection, _ = listener.accept()
                with connection:
                    client.sendall(b"test")
                    assert connection.recv(4) == b"test"
        finally:
            Path(paths.ipc_address).unlink(missing_ok=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_fallback_socket_directory_rejects_symlinks(long_runtime_paths, tmp_path):
    directory = Path(long_runtime_paths.ipc_address).parent
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    before = target.stat().st_mode
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError, match="real directory"):
        long_runtime_paths.ensure_private()
    assert target.stat().st_mode == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_fallback_socket_directory_rejects_weak_permissions(long_runtime_paths):
    directory = Path(long_runtime_paths.ipc_address).parent
    directory.mkdir(mode=0o700)
    directory.chmod(0o755)
    with pytest.raises(PermissionError, match="group/world"):
        long_runtime_paths.ensure_private()
    assert directory.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory ownership")
def test_fallback_socket_directory_rejects_other_owner(long_runtime_paths, monkeypatch):
    directory = Path(long_runtime_paths.ipc_address).parent
    directory.mkdir(mode=0o700)
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        metadata = original_lstat(path, *args, **kwargs)
        if path == directory:
            return SimpleNamespace(st_mode=metadata.st_mode, st_uid=metadata.st_uid + 1)
        return metadata

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        with pytest.raises(PermissionError, match="owned by the current user"):
            long_runtime_paths.ensure_private()


def test_ipc_key_is_stable_and_private(tmp_path) -> None:
    paths = _paths(tmp_path)
    first = paths.load_or_create_ipc_key()
    second = paths.load_or_create_ipc_key()

    assert len(first) == 32
    assert first == second
    if os.name != "nt":
        assert paths.ipc_key_file.stat().st_mode & 0o777 == 0o600
        assert paths.runtime_dir.stat().st_mode & 0o777 == 0o700


def test_ipc_key_creation_is_atomic_across_simultaneous_launchers(tmp_path) -> None:
    paths = _paths(tmp_path)
    barrier = threading.Barrier(8)
    keys: list[bytes] = []
    errors: list[BaseException] = []

    def load():
        try:
            barrier.wait()
            keys.append(paths.load_or_create_ipc_key())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=load) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(keys) == 8
    assert len(set(keys)) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe isolation")
def test_windows_ipc_address_is_isolated_by_runtime_directory(tmp_path) -> None:
    first = _paths(tmp_path / "first")
    second = _paths(tmp_path / "second")

    assert first.ipc_address.startswith(r"\\.\pipe\readndraft-broker-v")
    assert first.ipc_address == _paths(tmp_path / "first").ipc_address
    assert first.ipc_address != second.ipc_address


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_ipc_key_rejects_weak_permissions(tmp_path) -> None:
    paths = _paths(tmp_path)
    paths.load_or_create_ipc_key()
    os.chmod(paths.ipc_key_file, 0o644)

    with pytest.raises(PermissionError, match="group/world"):
        paths.load_or_create_ipc_key()
