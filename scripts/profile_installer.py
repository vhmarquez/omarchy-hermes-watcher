#!/usr/bin/env python3
"""Descriptor-relative installer for profile observer managed paths."""
from __future__ import annotations

import argparse
import errno
import os
import re
import secrets
import stat

from contextlib import contextmanager
from pathlib import Path

NF = getattr(os, "O_NOFOLLOW", 0)
CE = getattr(os, "O_CLOEXEC", 0)
NB = getattr(os, "O_NONBLOCK", 0)
DF = os.O_RDONLY | os.O_DIRECTORY | NF | CE
FF = os.O_RDONLY | NF | CE | NB
ROOT = "vhm.hermes-bots"

PAYLOADS = {
    "hermes-plugin": {
        "__init__.py": 0o644,
        "omarchy_bot_status.py": 0o644,
        "plugin.yaml": 0o644,
        "hermes_proc.py": 0o644,
        "secure_paths.py": 0o644,
    },
    "scripts": {
        "remove-profiles": 0o755,
        "cleanup-observer": 0o755,
        "profile_installer.py": 0o755,
    },
}


def valid(name):
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError("unsafe managed path component")


def valid_dir(fd):
    s = os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid():
        raise OSError(errno.EPERM, "unsafe managed directory")


def valid_file(fd):
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_nlink != 1:
        raise OSError(errno.EPERM, "unsafe managed file")


@contextmanager
def base(path, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | CE)
    try:
        # Derive the target from the held descriptor, not a second pathname lookup.
        yield fd, Path(os.path.realpath(f"/proc/self/fd/{fd}"))
    finally:
        os.close(fd)


def child_dir(parent, name, create=False):
    valid(name)
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, DF, dir_fd=parent)
    try:
        valid_dir(fd)
        if create:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def managed_root(data_parent, create=False):
    with base(data_parent, create) as (parent, canonical):
        root = child_dir(parent, ROOT, create)
        try:
            yield root, canonical
        finally:
            os.close(root)


def read_source(source_root, directory, name):
    fd = os.open(source_root / directory / name, FF)
    try:
        valid_file(fd)
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
    finally:
        os.close(fd)


def validate_leaf(parent, name):
    try:
        fd = os.open(name, FF, dir_fd=parent)
    except FileNotFoundError:
        return
    try:
        valid_file(fd)
    finally:
        os.close(fd)


def atomic_file(parent, name, payload, mode):
    valid(name)
    validate_leaf(parent, name)
    temporary = f".{name}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NF | CE,
                 mode, dir_fd=parent)
    try:
        valid_file(fd)
        os.fchmod(fd, mode)
        remaining = memoryview(payload)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError(errno.EIO, "short write")
            remaining = remaining[count:]
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)


def install_data(data_parent, source_root):
    with managed_root(data_parent, True) as (root, canonical):
        for directory, files in PAYLOADS.items():
            destination = child_dir(root, directory, True)
            try:
                expected_modules = {
                    Path(name).stem for name in files if name.endswith(".py")
                }
                if expected_modules:
                    remove_python_cache(destination, expected_modules)
                for name, mode in files.items():
                    atomic_file(destination, name,
                                read_source(source_root, directory, name), mode)
            finally:
                os.close(destination)
    return canonical / ROOT / "hermes-plugin"


def initialized(data_parent):
    try:
        with managed_root(data_parent) as (root, _):
            try:
                fd = os.open(".initialized", FF, dir_fd=root)
            except FileNotFoundError:
                return False
            try:
                valid_file(fd)
                return True
            finally:
                os.close(fd)
    except FileNotFoundError:
        return False


def mark(data_parent):
    with managed_root(data_parent) as (root, _):
        atomic_file(root, ".initialized", b"", 0o600)


@contextmanager
def profile_plugins(profile_home, *, create=False):
    if create:
        profile_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = os.open(profile_home, DF)
    try:
        valid_dir(profile)
        if create:
            try:
                os.mkdir("plugins", 0o700, dir_fd=profile)
            except FileExistsError:
                pass
        plugins = child_dir(profile, "plugins")
        try:
            yield plugins
        finally:
            os.close(plugins)
    finally:
        os.close(profile)


def install_profile_link(profile_home, observer_source):
    target = str(observer_source)
    with profile_plugins(profile_home, create=True) as plugins:
        try:
            metadata = os.stat("omarchy-bot-status", dir_fd=plugins, follow_symlinks=False)
        except FileNotFoundError:
            os.symlink(target, "omarchy-bot-status", dir_fd=plugins)
            return "installed"
        if not stat.S_ISLNK(metadata.st_mode):
            raise OSError(errno.EPERM, "refusing unrelated profile plugin path")
        if os.readlink("omarchy-bot-status", dir_fd=plugins) != target:
            raise OSError(errno.EPERM, "refusing unrelated profile plugin symlink")
        return "present"


def remove_profile_link(profile_home, observer_source):
    target = str(observer_source)
    try:
        with profile_plugins(profile_home) as plugins:
            try:
                metadata = os.stat("omarchy-bot-status", dir_fd=plugins, follow_symlinks=False)
            except FileNotFoundError:
                return "absent"
            if not stat.S_ISLNK(metadata.st_mode):
                return "unrelated"
            if os.readlink("omarchy-bot-status", dir_fd=plugins) != target:
                return "unrelated"
            os.unlink("omarchy-bot-status", dir_fd=plugins)
            return "removed"
    except FileNotFoundError:
        return "absent"


def profile_link_status(profile_home, observer_source):
    target = str(observer_source)
    try:
        with profile_plugins(profile_home) as plugins:
            try:
                metadata = os.stat("omarchy-bot-status", dir_fd=plugins, follow_symlinks=False)
            except FileNotFoundError:
                return "absent"
            if not stat.S_ISLNK(metadata.st_mode):
                return "unrelated"
            if os.readlink("omarchy-bot-status", dir_fd=plugins) != target:
                return "unrelated"
            return "managed"
    except FileNotFoundError:
        return "absent"


def unlink_regular(parent, name):
    fd = os.open(name, FF, dir_fd=parent)
    try:
        valid_file(fd)
    finally:
        os.close(fd)
    os.unlink(name, dir_fd=parent)


def remove_python_cache(parent, expected_modules):
    try:
        cache = child_dir(parent, "__pycache__")
    except FileNotFoundError:
        return
    try:
        entries = set(os.listdir(cache))
        allowed = re.compile(
            rf"^(?:{'|'.join(re.escape(name) for name in sorted(expected_modules))})"
            r"\.[A-Za-z0-9_.-]+\.pyc$"
        )
        unknown = {name for name in entries if allowed.fullmatch(name) is None}
        if unknown:
            raise OSError(errno.EPERM, f"unmanaged __pycache__ entries: {sorted(unknown)!r}")
        for name in entries:
            unlink_regular(cache, name)
    finally:
        os.close(cache)
    os.rmdir("__pycache__", dir_fd=parent)


def remove_data(data_parent):
    try:
        with base(data_parent) as (parent, _):
            root = child_dir(parent, ROOT)
            try:
                allowed_root = set(PAYLOADS) | {".initialized"}
                unknown = set(os.listdir(root)) - allowed_root
                if unknown:
                    raise OSError(errno.EPERM, f"unmanaged data-root entries: {sorted(unknown)!r}")
                for directory, allowed_modes in PAYLOADS.items():
                    try:
                        current = child_dir(root, directory)
                    except FileNotFoundError:
                        continue
                    try:
                        entries = set(os.listdir(current))
                        expected_modules = {
                            Path(name).stem for name in allowed_modes if name.endswith(".py")
                        }
                        if "__pycache__" in entries and expected_modules:
                            remove_python_cache(current, expected_modules)
                            entries.remove("__pycache__")
                        unknown = entries - set(allowed_modes)
                        if unknown:
                            raise OSError(errno.EPERM,
                                          f"unmanaged {directory} entries: {sorted(unknown)!r}")
                        for name in entries:
                            unlink_regular(current, name)
                    finally:
                        os.close(current)
                    os.rmdir(directory, dir_fd=root)
                try:
                    unlink_regular(root, ".initialized")
                except FileNotFoundError:
                    pass
            finally:
                os.close(root)
            os.rmdir(ROOT, dir_fd=parent)
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install-data")
    install.add_argument("data_parent", type=Path)
    install.add_argument("source_root", type=Path)
    install_link = commands.add_parser("install-profile-link")
    install_link.add_argument("profile_home", type=Path)
    install_link.add_argument("observer_source", type=Path)
    remove_link = commands.add_parser("remove-profile-link")
    remove_link.add_argument("profile_home", type=Path)
    remove_link.add_argument("observer_source", type=Path)
    link_status = commands.add_parser("profile-link-status")
    link_status.add_argument("profile_home", type=Path)
    link_status.add_argument("observer_source", type=Path)

    for command in ("is-initialized", "mark-initialized", "remove-data"):
        sub = commands.add_parser(command)
        sub.add_argument("data_parent", type=Path)
    args = parser.parse_args()
    if args.command == "install-data":
        print(install_data(args.data_parent, args.source_root))
    elif args.command == "install-profile-link":
        print(install_profile_link(args.profile_home, args.observer_source))
    elif args.command == "remove-profile-link":
        print(remove_profile_link(args.profile_home, args.observer_source))
    elif args.command == "profile-link-status":
        print(profile_link_status(args.profile_home, args.observer_source))
    elif args.command == "is-initialized":
        return 0 if initialized(args.data_parent) else 1
    elif args.command == "mark-initialized":
        mark(args.data_parent)
    else:
        remove_data(args.data_parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
