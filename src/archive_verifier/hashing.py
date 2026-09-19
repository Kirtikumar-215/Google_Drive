"""Race-aware streaming SHA-256 hashing."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HashResult:
    status: str
    sha256: str | None
    bytes_read: int
    size: int
    mtime_ns: int


def _identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (stat.st_dev, getattr(stat, "st_ino", 0), stat.st_size, stat.st_mtime_ns)


def sha256_file(
    path: str | Path, chunk_size: int = 1024 * 1024, chunk_hook: Callable[[int], None] | None = None
) -> HashResult:
    target = Path(path)
    before = target.stat(follow_symlinks=False)
    with target.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(before) != _identity(opened):
            return HashResult("FILE_CHANGED_DURING_HASH", None, 0, before.st_size, before.st_mtime_ns)
        digest = hashlib.sha256()
        bytes_read = 0
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
            bytes_read += len(chunk)
            if chunk_hook is not None:
                chunk_hook(bytes_read)
        final_handle = os.fstat(stream.fileno())
    final_path = target.stat(follow_symlinks=False)
    if _identity(before) != _identity(final_handle) or _identity(before) != _identity(final_path):
        return HashResult("FILE_CHANGED_DURING_HASH", None, bytes_read, final_path.st_size, final_path.st_mtime_ns)
    return HashResult("OK", digest.hexdigest(), bytes_read, before.st_size, before.st_mtime_ns)
