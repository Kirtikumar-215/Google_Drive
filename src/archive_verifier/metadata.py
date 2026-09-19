"""Same-directory Google metadata discovery and matching."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

NON_MEDIA = re.compile(
    r"^(?:metadata(?:\(\d+\))?|print-subscriptions|shared_album_comments|user-generated-memory-titles)\.json$", re.I
)
EDITED_SUFFIXES = ("-edited",)


@dataclass(frozen=True)
class JsonRecord:
    path: Path
    relative_path: str
    size: int
    sha256: str | None
    kind: str
    raw: str | None
    parsed: dict[str, object] | None


@dataclass(frozen=True)
class MatchResult:
    status: str
    record: JsonRecord | None
    method: str | None
    confidence: str | None
    reason: str | None


def discover_json(root: Path, records: list[tuple[Path, int, str | None]]) -> list[JsonRecord]:
    result: list[JsonRecord] = []
    for path, size, digest in sorted(records, key=lambda item: item[0].relative_to(root).as_posix()):
        raw: str | None = None
        if size > 10 * 1024 * 1024:
            result.append(JsonRecord(path, path.relative_to(root).as_posix(), size, digest, "INVALID_JSON", None, None))
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            value = json.loads(raw)
            parsed = value if isinstance(value, dict) else None
            kind = "NON_MEDIA_JSON" if NON_MEDIA.fullmatch(path.name) else "MEDIA_METADATA"
            if parsed is None:
                kind = "INVALID_JSON"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
            kind = "INVALID_JSON"
        result.append(JsonRecord(path, path.relative_to(root).as_posix(), size, digest, kind, raw, parsed))
    return result


def _numbered(stem: str) -> tuple[str, int | None]:
    match = re.fullmatch(r"(.+)\((\d+)\)", stem)
    return (match.group(1), int(match.group(2))) if match else (stem, None)


def _base_stem(media: Path) -> str:
    stem, _ = _numbered(media.stem)
    for suffix in EDITED_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def match_metadata(media: Path, candidates: list[JsonRecord]) -> MatchResult:
    same_dir = [item for item in candidates if item.path.parent == media.parent and item.kind != "NON_MEDIA_JSON"]
    filename = media.name
    tiers: list[tuple[str, str, list[JsonRecord]]] = []
    exact_supp = f"{filename}.supplemental-metadata.json"
    tiers.append(("SUPPLEMENTAL", "HIGH", [item for item in same_dir if item.path.name == exact_supp]))
    tiers.append(("NORMAL", "HIGH", [item for item in same_dir if item.path.name == f"{filename}.json"]))
    prefixes = [filename, f"{filename}.supplemental-metadata"]
    tiers.append(
        (
            "TRUNCATED",
            "MEDIUM",
            [
                item
                for item in same_dir
                if any(prefix.startswith(item.path.stem) for prefix in prefixes) and len(item.path.stem) >= 40
            ],
        )
    )
    base = _base_stem(media)
    tiers.append(
        (
            "EDITED",
            "MEDIUM",
            [
                item
                for item in same_dir
                if media.stem.endswith(EDITED_SUFFIXES) and item.path.name == f"{base}{media.suffix}.json"
            ],
        )
    )
    numbered_base, number = _numbered(media.stem)
    numbered_names = {f"{numbered_base}{media.suffix}({number}).json"} if number is not None else set()
    numbered_names |= {f"{numbered_base}.supplemental-metadata({number}).json"} if number is not None else set()
    tiers.append(("NUMBERED", "HIGH", [item for item in same_dir if item.path.name in numbered_names]))
    for method, confidence, found in tiers:
        if found:
            if len(found) > 1:
                return MatchResult("AMBIGUOUS", None, method, None, "MULTIPLE_CANDIDATES")
            record = found[0]
            if record.kind == "INVALID_JSON":
                return MatchResult("INVALID_JSON", record, method, None, "INVALID_JSON")
            title = record.parsed.get("title") if record.parsed else None
            if isinstance(title, str) and title and Path(title).stem != _base_stem(media):
                return MatchResult("AMBIGUOUS", record, method, None, "TITLE_MISMATCH")
            return MatchResult("MATCHED", record, method, confidence, None)
    return MatchResult("UNMATCHED", None, None, None, None)


def parsed_fields(record: JsonRecord) -> dict[str, str | None]:
    value = record.parsed or {}

    def text(key: str) -> str | None:
        item = value.get(key)
        return item if isinstance(item, str) else None

    def timestamp(key: str) -> str | None:
        item = value.get(key)
        raw = item.get("timestamp") if isinstance(item, dict) else item
        try:
            import datetime as dt

            return dt.datetime.fromtimestamp(int(str(raw)), dt.UTC).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    return {
        "meta_title": text("title"),
        "meta_description": text("description"),
        "photo_taken_at": timestamp("photoTakenTime"),
        "creation_time": timestamp("creationTime"),
        "modification_time": timestamp("modificationTime"),
        "meta_url": text("url"),
        "meta_origin": json.dumps(value.get("googlePhotosOrigin"), sort_keys=True)
        if value.get("googlePhotosOrigin") is not None
        else None,
    }
