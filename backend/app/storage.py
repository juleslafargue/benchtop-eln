"""Saving attached files so that a crash or a power cut can't leave a broken one behind (0.24.2).

Attached files are stored once, under their SHA-256 (attachments/ab/ab12…). Each one is written to a temporary file in
.incoming/ first (outside the attachments folder, so a backup never picks up half a file), forced to disk, and only
then renamed into place, which happens all at once. So a file in the store is either complete or not there.

Before 0.24.2 an upload was written straight into place: Benchtop stopped part-way left half a file there, and
uploading the same file again was skipped as "already stored", so the record pointed at the broken file for good
(found by the pull-the-plug tests, tests/crash_test.py). Now a stored copy is checked before it's trusted, and a
broken one is replaced.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from pathlib import Path
from typing import BinaryIO

from .config import settings


def blob_path(sha: str) -> Path:
    return settings.attachments_dir / sha[:2] / sha


def incoming() -> Path:
    d = settings.data_dir / ".incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_path() -> Path:
    return incoming() / f"{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(6)}.part"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sync_dir(d: Path) -> None:
    """Make a rename in this folder survive a power cut (not every system allows it; then the rename still happened)."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def intact(p: Path, sha: str, size: int | None = None) -> bool:
    try:
        if size is not None and p.stat().st_size != size:
            return False
        return sha256_of(p) == sha
    except OSError:
        return False


def put(tmp: Path) -> tuple[str, int]:
    """Move a finished temporary file (from temp_path(), already on disk) into the store. Returns (sha256, size).
    If the store already has it, the stored copy is checked first; a broken one (a crash part-way through an
    upload, before 0.24.2) is replaced."""
    with open(tmp, "rb") as f:  # on disk before the rename, so a power cut can't leave an empty file under the name
        os.fsync(f.fileno())
    sha, size = sha256_of(tmp), tmp.stat().st_size
    dst = blob_path(sha)
    if dst.is_file() and intact(dst, sha, size):
        tmp.unlink()
        return sha, size
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dst)
    _sync_dir(dst.parent)
    return sha, size


def save(src: BinaryIO, limit: int | None = None) -> tuple[str, int]:
    """Store what a file object holds (an upload). Raises ValueError if it's bigger than `limit` bytes."""
    tmp = temp_path()
    n = 0
    try:
        with open(tmp, "wb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += len(chunk)
                if limit is not None and n > limit:
                    raise ValueError("too big")
                out.write(chunk)
        return put(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def save_bytes(data: bytes) -> tuple[str, int]:
    import io
    return save(io.BytesIO(data))
