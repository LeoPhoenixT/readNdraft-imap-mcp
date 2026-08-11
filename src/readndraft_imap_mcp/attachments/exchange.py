from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_SAFE_NAME = re.compile(r"^[^/\\:\x00-\x1f\x7f]{1,255}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class InputAttachment:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SavedAttachment:
    saved_name: str
    original_name: str
    content_type: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PreparedAttachment:
    filename: str
    size: int
    sha256: str
    content: bytes


def _validate_name(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError("attachment name must be a plain filename")
    if value in {".", ".."} or value != value.strip() or value.endswith("."):
        raise ValueError("attachment name must be a plain filename")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise ValueError("attachment name is reserved")
    return value


class AttachmentExchange:
    """The broker's only local-file attachment capability."""

    def __init__(self, input_dir: Path, output_dir: Path) -> None:
        if not input_dir.is_absolute() or not output_dir.is_absolute():
            raise ValueError("attachment directories must be absolute")
        if input_dir == output_dir:
            raise ValueError("attachment input and output directories must differ")
        for directory in (input_dir, output_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("attachment directory must be a real directory")
            if os.name != "nt":
                os.chmod(directory, 0o700)
        self.input_dir = input_dir
        self.output_dir = output_dir

    @staticmethod
    def _open_directory(directory: Path) -> int | None:
        """Pin a real directory on POSIX so a later symlink swap cannot escape it."""
        if os.name == "nt":
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("attachment directory must be a real directory")
            return None
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(directory, flags)
        except OSError as exc:
            raise ValueError("attachment directory must be a real directory") from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("attachment directory must be a real directory")
        return descriptor

    @classmethod
    def _read_regular(cls, path: Path) -> bytes:
        if path.is_symlink():
            raise ValueError("attachment must be a regular non-symlink file")
        directory_descriptor = cls._open_directory(path.parent)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = (
                os.open(path, flags)
                if directory_descriptor is None
                else os.open(path.name, flags, dir_fd=directory_descriptor)
            )
        except OSError as exc:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
            raise ValueError("attachment must be a regular non-symlink file") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("attachment must be a regular non-symlink file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(MAX_ATTACHMENT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds the 25 MB limit")
        return content

    def list_inputs(self) -> tuple[InputAttachment, ...]:
        result: list[InputAttachment] = []
        for path in sorted(self.input_dir.iterdir(), key=lambda item: item.name.casefold()):
            try:
                name = _validate_name(path.name)
                content = self._read_regular(path)
            except ValueError:
                continue
            result.append(InputAttachment(name, len(content), hashlib.sha256(content).hexdigest()))
        return tuple(result)

    def prepare(self, names: tuple[str, ...]) -> tuple[PreparedAttachment, ...]:
        if len(set(names)) != len(names):
            raise ValueError("duplicate attachment name")
        result: list[PreparedAttachment] = []
        for raw_name in names:
            name = _validate_name(raw_name)
            content = self._read_regular(self.input_dir / name)
            result.append(PreparedAttachment(name, len(content), hashlib.sha256(content).hexdigest(), content))
        return tuple(result)

    def save(self, original_name: str, content_type: str, content: bytes) -> SavedAttachment:
        safe = original_name.replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".") or "attachment"
        safe = _validate_name(safe[:255])
        stem, suffix = Path(safe).stem, Path(safe).suffix
        directory_descriptor = self._open_directory(self.output_dir)
        try:
            for attempt in range(100):
                name = safe if attempt == 0 else f"{stem}-{secrets.token_hex(3)}{suffix}"
                path = self.output_dir / name
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                try:
                    descriptor = (
                        os.open(path, flags, 0o600)
                        if directory_descriptor is None
                        else os.open(name, flags, 0o600, dir_fd=directory_descriptor)
                    )
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    if directory_descriptor is None:
                        path.unlink(missing_ok=True)
                    else:
                        os.unlink(name, dir_fd=directory_descriptor)
                    raise
                return SavedAttachment(name, safe, content_type, len(content), hashlib.sha256(content).hexdigest())
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
        raise RuntimeError("unable to allocate a unique attachment output name")
