"""Stable no-follow access to the monitor's managed filesystem tree."""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC
_MAX_JSON_BYTES = 1024 * 1024


class ManagedTree:
    """Access a managed subtree relative to an allowed-to-be-symlinked base."""

    def __init__(self, root: Path):
        self.root = root
        if root.name == "hermes-bots" and root.parent.name == "omarchy":
            self.base = root.parent.parent
            self.root_parts = ("omarchy", "hermes-bots")
        else:
            self.base = root.parent
            self.root_parts = (root.name,)

    def _validate_dir(self, fd: int) -> None:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise OSError(errno.EPERM, "managed directory has unsafe ownership or type")

    def _validate_file(self, fd: int) -> None:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise OSError(errno.EPERM, "managed file has unsafe ownership, type, or links")

    def _open_dir(self, relative: tuple[str, ...] = (), *, create: bool = False) -> int:
        if create:
            self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
        try:
            for component in self.root_parts + relative:
                if not component or component in {".", ".."} or "/" in component:
                    raise ValueError("unsafe managed path component")
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
                try:
                    self._validate_dir(child)
                    if create:
                        os.fchmod(child, 0o700)
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def directory(self, relative: tuple[str, ...] = (), *, create: bool = False) -> Iterator[int]:
        fd = self._open_dir(relative, create=create)
        try:
            yield fd
        finally:
            os.close(fd)

    def ensure_directory(self, relative: tuple[str, ...] = ()) -> None:
        with self.directory(relative, create=True):
            pass

    def list_directories(self, relative: tuple[str, ...]) -> list[str]:
        try:
            with self.directory(relative) as fd:
                names = os.listdir(fd)
                result = []
                for name in names:
                    child = -1
                    try:
                        child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                        self._validate_dir(child)
                    except OSError:
                        if child >= 0:
                            os.close(child)
                        continue
                    else:
                        os.close(child)
                        result.append(name)
                return result
        except FileNotFoundError:
            return []

    def list_regular_files(self, relative: tuple[str, ...], suffix: str = "") -> list[str]:
        try:
            with self.directory(relative) as fd:
                result = []
                for name in os.listdir(fd):
                    if suffix and not name.endswith(suffix):
                        continue
                    child = -1
                    try:
                        child = os.open(
                            name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=fd
                        )
                        self._validate_file(child)
                    except OSError:
                        if child >= 0:
                            os.close(child)
                        continue
                    else:
                        os.close(child)
                        result.append(name)
                return result
        except FileNotFoundError:
            return []

    def read_json(self, relative: tuple[str, ...]) -> object:
        with self.directory(relative[:-1]) as fd:
            child = os.open(
                relative[-1], os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=fd
            )
            try:
                self._validate_file(child)
                chunks = bytearray()
                while len(chunks) <= _MAX_JSON_BYTES:
                    block = os.read(child, min(65536, _MAX_JSON_BYTES + 1 - len(chunks)))
                    if not block:
                        break
                    chunks.extend(block)
                if len(chunks) > _MAX_JSON_BYTES:
                    raise OSError(errno.EFBIG, "managed JSON exceeds size limit")
            finally:
                os.close(child)
        return json.loads(chunks.decode("utf-8"))

    def _validate_existing_leaf(self, fd: int, name: str) -> None:
        try:
            child = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return
        try:
            self._validate_file(child)
        finally:
            os.close(child)

    def atomic_json(self, relative: tuple[str, ...], value: object) -> None:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self.directory(relative[:-1], create=True) as fd:
            self._validate_existing_leaf(fd, relative[-1])
            temporary = f".{relative[-1]}.{secrets.token_hex(8)}"
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=fd,
            )
            try:
                self._validate_file(temp_fd)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(temp_fd, remaining)
                    if written <= 0:
                        raise OSError(errno.EIO, "short write made no progress")
                    remaining = remaining[written:]
                os.fsync(temp_fd)
                os.replace(temporary, relative[-1], src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                os.close(temp_fd)

    @contextmanager
    def lock(self, relative: tuple[str, ...]) -> Iterator[object]:
        with self.directory(relative[:-1], create=True) as fd:
            child = os.open(
                relative[-1],
                os.O_RDWR | os.O_CREAT | os.O_APPEND | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                0o600,
                dir_fd=fd,
            )
            try:
                self._validate_file(child)
                os.fchmod(child, 0o600)
                with os.fdopen(child, "a+", encoding="utf-8") as handle:
                    child = -1
                    yield handle
            finally:
                if child >= 0:
                    os.close(child)

    def unlink_regular(self, relative: tuple[str, ...]) -> None:
        with self.directory(relative[:-1]) as fd:
            self._validate_existing_leaf(fd, relative[-1])
            os.unlink(relative[-1], dir_fd=fd)
