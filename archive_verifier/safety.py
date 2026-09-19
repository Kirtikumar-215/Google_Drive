"""Filesystem policy and the only deletion helper used by the application."""

from __future__ import annotations

import os
import re
from pathlib import Path


class SafetyError(RuntimeError):
    """Raised when a path or filesystem operation violates the policy."""


_input_root: Path | None = None
_allowed_roots: tuple[Path, ...] = ()
_partial_root: Path | None = None
_partial_name = re.compile(r"^[0-9a-fA-F-]{36}\.partial$")


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def configure(input_root: str | os.PathLike[str], archive_root: str | os.PathLike[str] | None,
              db_path: str | os.PathLike[str], report_dir: str | os.PathLike[str],
              log_dir: str | os.PathLike[str]) -> None:
    """Configure and validate the roots permitted for future writes."""
    global _input_root, _allowed_roots, _partial_root
    source = Path(input_root).resolve()
    if not source.is_dir():
        raise SafetyError(f"input root is not a directory: {source}")
    roots = [Path(db_path).resolve().parent, Path(report_dir).resolve(), Path(log_dir).resolve()]
    if archive_root is not None:
        archive = Path(archive_root).resolve()
        if _key(source) == _key(archive) or _inside(source, archive) or _inside(archive, source):
            raise SafetyError("input and archive roots must be disjoint")
        roots.extend((archive, archive / ".partial"))
        _partial_root = archive / ".partial"
    else:
        _partial_root = None
    if any(_inside(source, root) for root in roots) or any(_inside(root, source) for root in roots):
        raise SafetyError("database, reports, logs, and archive must be outside input root")
    _input_root = source
    _allowed_roots = tuple(dict.fromkeys(roots))


def assert_write_allowed(path: str | os.PathLike[str], purpose: str) -> Path:
    """Return a normalized path only when it is inside an allowed write root."""
    candidate = Path(path).resolve(strict=False)
    if _input_root is None:
        raise SafetyError("safety has not been configured")
    if _inside(candidate, _input_root):
        raise SafetyError(f"input-root write rejected ({purpose}): {candidate}")
    if not any(_inside(candidate, root) for root in _allowed_roots):
        raise SafetyError(f"write outside allowed roots ({purpose}): {candidate}")
    return candidate


def cleanup_partial(path: str | os.PathLike[str], recorded_names: set[str]) -> None:
    """Delete one recorded application partial after checking every boundary."""
    candidate = Path(path)
    if _partial_root is None or candidate.parent != _partial_root:
        raise SafetyError("partial cleanup outside configured partial directory")
    info = os.lstat(candidate)
    if candidate.is_symlink() or not _partial_root.is_dir() or _partial_root.is_symlink():
        raise SafetyError("refusing symlink partial or partial directory")
    if candidate.name not in recorded_names or not _partial_name.fullmatch(candidate.name):
        raise SafetyError("unrecorded partial file")
    assert_write_allowed(candidate, "partial cleanup")
    if not os.path.isfile(candidate) or info.st_size < 0:
        raise SafetyError("partial is not a regular file")
    candidate.unlink()


def is_link(entry: os.DirEntry[str]) -> bool:
    """Detect symlinks and Windows junction/reparse points without following them."""
    if entry.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(Path(entry.path)):
        return True
    is_reparse = getattr(Path(entry.path), "is_junction", None)
    return bool(is_reparse and is_reparse())
