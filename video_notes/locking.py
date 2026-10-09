"""OS file locks and the directory-sync capability used by durable receipts."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time

_WINDOWS = os.name == "nt"
_CONTENTION = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
_UNSUPPORTED_SYNC = {errno.EINVAL, errno.ENOTSUP}


def _acquire(stream, blocking):
    if not _WINDOWS:
        import fcntl
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(stream, flags)
        return
    import msvcrt
    # Windows byte locks require a byte to lock. This is a dedicated lock file,
    # never the JSON/JSONL artifact being protected.
    while True:
        stream.seek(0)
        try:
            # Two first callers may both observe an empty file. If one locks
            # the byte before the other writes, initialization has the same
            # conflict/wait behavior as acquisition.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in _CONTENTION:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, "File lock is held by another command") from None
            time.sleep(0.05)


def _release(stream):
    if _WINDOWS:
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def file_lock(path, *, blocking=True):
    """Hold an exclusive lock; a nonblocking conflict raises BlockingIOError.

    All callers must use the same dedicated lock path. The descriptor remains
    open for the entire protected operation and closes even on acquisition or
    body failure. Lock files remain in place so concurrent callers share an inode.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        _acquire(stream, blocking)
        try:
            yield
        finally:
            _release(stream)


def sync_directory(path):
    """Flush replaced directory entries where supported; return that capability.

    Windows Python cannot fsync directory handles. Callers still fsync the file
    before replacement. Unsupported POSIX filesystems are treated alike; other
    I/O failures propagate so an API dispatch never hides a durability failure.
    """
    if _WINDOWS:
        return False
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
        return True
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_SYNC:
            raise
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
