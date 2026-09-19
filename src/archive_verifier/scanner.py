"""Deterministic, link-safe directory accounting."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import safety


@dataclass(frozen=True)
class Entry:
    path: Path
    kind: str
    reason: str | None = None


@dataclass(frozen=True)
class ScanResult:
    files: tuple[Entry, ...]
    directories: tuple[Entry, ...]
    skipped: tuple[Entry, ...]
    entries: tuple[Entry, ...]


def scan(root: str | os.PathLike[str]) -> ScanResult:
    if safety._is_reparse(Path(root)):
        raise safety.SafetyError("input root cannot be a symlink or reparse point")
    source = Path(root).resolve()
    files: list[Entry] = []
    directories: list[Entry] = [Entry(source, "DIRECTORY")]
    skipped: list[Entry] = []

    def visit(directory: Path) -> bool:
        try:
            with os.scandir(directory) as raw_entries:
                entries = sorted(raw_entries, key=lambda item: Path(item.path).relative_to(source).as_posix())
                for raw in entries:
                    path = Path(raw.path)
                    if safety.is_link(raw):
                        skipped.append(Entry(path, "LINK", "REPARSE_POINT"))
                        continue
                    try:
                        if raw.is_dir(follow_symlinks=False):
                            if visit(path):
                                directories.append(Entry(path, "DIRECTORY"))
                            else:
                                skipped.append(Entry(path, "SKIPPED", "PERMISSION_ERROR"))
                        elif raw.is_file(follow_symlinks=False):
                            files.append(Entry(path, "REGULAR_FILE"))
                        else:
                            skipped.append(Entry(path, "SKIPPED", "UNSUPPORTED_ENTRY"))
                    except PermissionError:
                        skipped.append(Entry(path, "SKIPPED", "PERMISSION_ERROR"))
        except PermissionError:
            return False
        return True

    if not visit(source):
        skipped.append(Entry(source, "SKIPPED", "PERMISSION_ERROR"))
    entries = tuple(files + directories[1:] + skipped)
    return ScanResult(tuple(files), tuple(directories), tuple(skipped), entries)
