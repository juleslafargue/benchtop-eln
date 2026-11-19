"""One Benchtop at a time on a data folder (0.36.3).

While Benchtop runs it holds a lock on benchtop.lock in its data folder (the file says which process holds it). A
second Benchtop started on the same data folder, on whatever port, refuses to start instead of the two writing over
each other's work; ./benchtop update, upgrade, rollback and restore look at the same lock, so they never swap files
or data under a running Benchtop. The operating system lets go of the lock when the process ends, however it ends
(a crash, the power going), so a lock can't be left behind to block the next start.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # not on Windows itself (Benchtop runs there through WSL, which has it)
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("benchtop.datalock")
NAME = "benchtop.lock"
_held: dict[str, int] = {}  # lock file -> open file descriptor, while this process holds it


class InUse(RuntimeError):
    pass


def acquire(data_dir: Path, port: int | str = "") -> None:
    """Hold the data folder's lock, or raise InUse when another Benchtop holds it."""
    if fcntl is None:
        return
    path = str(Path(data_dir) / NAME)
    if path in _held:  # this process already holds it (the tests start the app more than once)
        return
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:  # a read-only data folder: nothing to protect against, and nothing else would work anyway
        log.warning("Couldn't make %s: %s", path, exc)
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            other = (os.read(fd, 200).decode(errors="replace").split() or ["?"])[0]
        finally:
            os.close(fd)
        raise InUse(f"Another Benchtop (process {other}) is already running with this data folder ({data_dir}), "
                    "perhaps on another port. Use that one, or stop it first: two at once would get in each "
                    "other's way.") from None
    except OSError as exc:  # a file system without locks (some network drives): carry on as before 0.36.3
        os.close(fd)
        log.warning("Couldn't lock %s: %s", path, exc)
        return
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {port}\n".encode())
    _held[path] = fd


def release(data_dir: Path) -> None:
    fd = _held.pop(str(Path(data_dir) / NAME), None)
    if fd is not None:
        os.close(fd)  # closing lets go of the lock; the file stays (removing it could race with the next start)
