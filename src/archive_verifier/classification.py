"""Deterministic file classification for M2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "tiff", "tif", "avif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "3gp", "3g2", "avi", "mkv", "webm", "mpg", "mpeg", "wmv", "mts", "m2ts", "flv"}
HASH_ONLY_EXTENSIONS = {"dng", "cr2", "nef", "arw", "orf", "rw2"}


@dataclass(frozen=True)
class ClassifiedFile:
    path: Path
    relative_path: str
    extension: str
    category: str
    warnings: tuple[str, ...] = ()


def classify_files(root: Path, paths: list[Path]) -> list[ClassifiedFile]:
    suffixes = {path: path.suffix.lower().lstrip(".") for path in paths}
    image_stems = {path.with_suffix("") for path in paths if suffixes[path] in IMAGE_EXTENSIONS}
    result: list[ClassifiedFile] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        extension = suffixes[path]
        if extension == "json":
            category = "JSON"
        elif extension in IMAGE_EXTENSIONS:
            category = "IMAGE"
        elif extension in VIDEO_EXTENSIONS:
            category = "VIDEO"
        elif extension in HASH_ONLY_EXTENSIONS:
            category = "HASH_ONLY"
        elif extension == "mp" and path.with_suffix("") in image_stems:
            category = "VIDEO"
        else:
            category = "UNSUPPORTED"
        warnings = ("MOTION_PHOTO_COMPANION",) if extension == "mp" and category == "VIDEO" else ()
        result.append(ClassifiedFile(path, path.relative_to(root).as_posix(), extension, category, warnings))
    return result
