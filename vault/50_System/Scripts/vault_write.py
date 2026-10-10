"""Check revisions and use local writer locks on Windows, macOS and Linux.

Use one writer per note. Editors and external tools
do not use these local locks. Windows flushes file data, but cannot use
the POSIX directory flush through Python.
"""
import errno
import hashlib
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def sync_directory(path: Path) -> None:
    """Flush a directory on POSIX. Windows file data is flushed separately."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def writer_lock(path: Path):
    root = Path(os.environ.get("CONTEXT_VAULT_LOCK_DIR", str(Path.home() / ".cache/context-vault/locks")))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = os.path.normcase(os.path.abspath(path))
    key = hashlib.sha256(identity.encode()).hexdigest()
    lock_path = root / key
    if lock_path.is_symlink():
        raise ValueError("lock file must not be a symlink")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if os.name == "nt":
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
            deadline = time.monotonic() + 30
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("local Vault writer lock timed out") from exc
                    time.sleep(0.05)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def replace_checked(path: Path, data: bytes, expected_sha256: str) -> None:
    path = Path(os.path.abspath(path))
    temporary = None
    with writer_lock(path):
        def check():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
                raise ValueError(f"concurrent edit or symlink; re-read and reconcile: {path}")
        check()
        mode = stat.S_IMODE(path.stat().st_mode)
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                if os.name != "nt":
                    os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            check()
            os.replace(temporary, path)
            temporary = None
            sync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
