"""Link-safe recursive scanner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import safety


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SkippedLink:
    path: Path


def scan(root: str | os.PathLike[str]) -> tuple[list[ScannedFile], list[SkippedLink]]:
    files: list[ScannedFile] = []
    skipped: list[SkippedLink] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if safety.is_link(entry):
                    skipped.append(SkippedLink(path))
                elif entry.is_dir(follow_symlinks=False):
                    visit(path)
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    files.append(ScannedFile(path, stat.st_size, stat.st_mtime_ns))

    visit(Path(root).resolve())
    return files, skipped
