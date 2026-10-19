"""Durable local file primitives. Authored by [R2] SRVP2-CODEX.

Locks are advisory and protect cooperating processes on Linux/macOS. They do
not grant authority over external actions or replace a distributed coordinator.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".helix-write-",
                                         delete=False) as output:
            temporary = Path(output.name)
            os.chmod(output.name, mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def atomic_write_json(path, value) -> None:
    atomic_write_bytes(path, (canonical_json(value) + "\n").encode("utf-8"))


@contextmanager
def file_lock(path, timeout: float = 10):
    """Hold an advisory process lock; a timeout is an explicit failure."""
    try:
        import fcntl
    except ImportError as error:
        raise RuntimeError("Process-safe file locks require Linux or macOS") from error
    if timeout < 0:
        raise ValueError("timeout must be nonnegative")
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Memory writer is busy; no write was performed")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
