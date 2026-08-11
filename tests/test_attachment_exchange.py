from __future__ import annotations

import os

import pytest

from readndraft_imap_mcp.attachments import AttachmentExchange


def exchange(tmp_path):
    return AttachmentExchange((tmp_path / "input").resolve(), (tmp_path / "output").resolve())


def test_input_is_filename_only_and_hash_bound(tmp_path) -> None:
    value = exchange(tmp_path)
    (value.input_dir / "report.txt").write_bytes(b"safe")
    listed = value.list_inputs()
    assert listed[0].name == "report.txt"
    assert listed[0].size == 4
    assert len(listed[0].sha256) == 64
    prepared = value.prepare(("report.txt",))
    assert prepared[0].content == b"safe"
    for hostile in ("../secret", "/etc/passwd", r"C:\secret", "a/b", "file:stream"):
        with pytest.raises(ValueError, match="plain filename"):
            value.prepare((hostile,))


def test_input_symlink_is_never_read(tmp_path) -> None:
    value = exchange(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    link = value.input_dir / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert value.list_inputs() == ()
    with pytest.raises(ValueError, match="non-symlink"):
        value.prepare(("link.txt",))


def test_output_never_overwrites_and_returns_digest(tmp_path) -> None:
    value = exchange(tmp_path)
    first = value.save("report.txt", "text/plain", b"one")
    second = value.save("report.txt", "text/plain", b"two")
    assert first.saved_name == "report.txt"
    assert second.saved_name != first.saved_name
    assert (value.output_dir / first.saved_name).read_bytes() == b"one"
    assert (value.output_dir / second.saved_name).read_bytes() == b"two"
    assert len(second.sha256) == 64
    if os.name != "nt":
        assert (value.output_dir / first.saved_name).stat().st_mode & 0o077 == 0


def test_directory_symlink_swap_is_rejected(tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("directory-handle pinning is POSIX-specific")
    value = exchange(tmp_path)
    original = tmp_path / "original-output"
    value.output_dir.rename(original)
    escape = tmp_path / "escape"
    escape.mkdir()
    value.output_dir.symlink_to(escape, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        value.save("report.txt", "text/plain", b"must not escape")
    assert list(escape.iterdir()) == []
