"""Read-only Pillow image verification with source identity checks."""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile, ImageSequence


@dataclass(frozen=True)
class ImageResult:
    status: str
    reason: str | None
    last_error: str | None
    detected_format: str | None
    width: int | None
    height: int | None
    frame_count: int | None


def _identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (stat.st_dev, getattr(stat, "st_ino", 0), stat.st_size, stat.st_mtime_ns)


def verify_image(path: Path, max_frames: int = 10000, max_pixels: int = 500000000) -> ImageResult:
    before = path.stat(follow_symlinks=False)
    if before.st_size == 0:
        return ImageResult("FAILED_VERIFICATION", "ZERO_BYTE", None, None, None, None, None)
    if path.suffix.lower() in {".heic", ".heif", ".avif"}:
        try:
            from pillow_heif import register_heif_opener  # type: ignore[import-not-found]
        except ImportError:
            return ImageResult("UNVERIFIED", "DECODER_NOT_AVAILABLE", None, None, None, None, None)
        register_heif_opener()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                opened = os.stat(path, follow_symlinks=False)
                if _identity(before) != _identity(opened):
                    return ImageResult("FAILED_VERIFICATION", "FILE_CHANGED_DURING_HASH", None, None, None, None, None)
                width, height = image.size
                if width * height > max_pixels:
                    return ImageResult("UNVERIFIED", "IMAGE_TOO_LARGE", None, image.format, width, height, None)
                frame_count = getattr(image, "n_frames", 1)
                if frame_count > max_frames:
                    return ImageResult("UNVERIFIED", "TOO_MANY_FRAMES", None, image.format, width, height, frame_count)
                for frame in ImageSequence.Iterator(image):
                    frame.load()
                detected = image.format
        after = path.stat(follow_symlinks=False)
        if _identity(before) != _identity(after):
            return ImageResult(
                "FAILED_VERIFICATION", "FILE_CHANGED_DURING_HASH", None, detected, width, height, frame_count
            )
        return ImageResult("VERIFIED", None, None, detected, width, height, frame_count)
    except Image.DecompressionBombWarning as error:
        return ImageResult("UNVERIFIED", "DECOMPRESSION_BOMB", str(error), None, None, None, None)
    except Image.DecompressionBombError as error:
        return ImageResult("UNVERIFIED", "DECOMPRESSION_BOMB", str(error), None, None, None, None)
    except (OSError, SyntaxError, ValueError) as error:
        return ImageResult("FAILED_VERIFICATION", "TRUNCATED_OR_CORRUPT_IMAGE", str(error), None, None, None, None)


assert ImageFile.LOAD_TRUNCATED_IMAGES is False
