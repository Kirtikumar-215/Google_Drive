"""Centralized output-root and link safety checks."""

from __future__ import annotations

import os
import re
from pathlib import Path


class SafetyError(RuntimeError):
    """Raised when an application path violates the filesystem policy."""


_input_root: Path | None = None
_allowed_roots: tuple[Path, ...] = ()
_partial_root: Path | None = None
_partial_name = re.compile(r"^[0-9a-fA-F-]{36}\.partial$")


def _normalized(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.normcase(str(Path(path).resolve(strict=False))))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt" and hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != -1 and bool(attributes & 0x400)
    except (AttributeError, OSError):
        return False


def configure(
    input_root: str | os.PathLike[str],
    db_path: str | os.PathLike[str],
    report_dir: str | os.PathLike[str] | None = None,
    log_dir: str | os.PathLike[str] | None = None,
    archive_root: str | os.PathLike[str] | None = None,
) -> None:
    """Set and validate all roots that application code may write to."""
    global _input_root, _allowed_roots, _partial_root
    raw_input = Path(input_root)
    if _is_reparse(raw_input):
        raise SafetyError("input root cannot be a symlink or reparse point")
    source = _normalized(input_root)
    if not source.is_dir():
        raise SafetyError(f"input root is not a directory: {source}")
    roots = [_normalized(db_path).parent]
    roots.extend(_normalized(value) for value in (report_dir, log_dir) if value is not None)
    if archive_root is not None:
        archive = _normalized(archive_root)
        if _inside(source, archive) or _inside(archive, source):
            raise SafetyError("input and archive roots must be disjoint")
        roots.extend((archive, archive / ".partial"))
        _partial_root = archive / ".partial"
    else:
        _partial_root = None
    if any(_inside(source, root) or _inside(root, source) for root in roots):
        raise SafetyError("database, reports, logs, and archive must be outside input root")
    _input_root = source
    _allowed_roots = tuple(dict.fromkeys(roots))


def assert_write_allowed(path: str | os.PathLike[str], purpose: str) -> Path:
    """Return a normalized path only when it is within an allowed root."""
    candidate = _normalized(path)
    if _input_root is None:
        raise SafetyError("safety has not been configured")
    if _inside(candidate, _input_root) or not any(_inside(candidate, root) for root in _allowed_roots):
        raise SafetyError(f"write rejected ({purpose}): {candidate}")
    return candidate


def cleanup_partial(path: str | os.PathLike[str], recorded_names: set[str]) -> None:
    """Remove exactly one recorded application partial file."""
    candidate = Path(path)
    if _partial_root is None or candidate.parent != _partial_root:
        raise SafetyError("partial cleanup outside configured partial directory")
    info = os.lstat(candidate)
    if candidate.is_symlink() or not _partial_root.is_dir() or _is_reparse(_partial_root):
        raise SafetyError("refusing a symlink or reparse point")
    if candidate.name not in recorded_names or not _partial_name.fullmatch(candidate.name):
        raise SafetyError("unrecorded partial file")
    assert_write_allowed(candidate, "partial cleanup")
    if not os.path.isfile(candidate) or info.st_size < 0:
        raise SafetyError("partial is not a regular file")
    candidate.unlink()


def is_link(entry: os.DirEntry[str]) -> bool:
    """Detect POSIX links and Windows junction/reparse points."""
    return _is_reparse(Path(entry.path))
