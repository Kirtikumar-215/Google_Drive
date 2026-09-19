"""Sequential M2 scan, metadata, image verification, duplicates, and reports."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from . import db, hashing, reporting, safety
from .classification import classify_files
from .image_verify import verify_image
from .metadata import discover_json, match_metadata, parsed_fields
from .scanner import scan


def _update(connection: sqlite3.Connection, media_id: int, **fields: object) -> None:
    assignments = ", ".join(f"{key}=?" for key in fields)
    with db.transaction(connection):
        connection.execute(f"UPDATE media SET {assignments} WHERE media_id=?", (*fields.values(), media_id))  # noqa: S608


def _warning(existing: str | None, values: tuple[str, ...]) -> str | None:
    codes = set(filter(None, (existing or "").split(","))) | set(values)
    return ",".join(sorted(codes)) or None


def run_m2(
    input_root: str | Path,
    db_path: str | Path,
    report_dir: str | Path,
    *,
    rehash: bool = False,
    retry_failed: bool = False,
    max_frames: int = 10000,
    max_pixels: int = 500000000,
) -> dict[str, object]:
    """Run the deterministic M2 sequence without threads or subprocesses."""
    root = Path(input_root).resolve()
    safety.configure(root, db_path, report_dir=report_dir)
    opened = db.open_database(db_path, root)
    safety.configure(root, db_path, report_dir=report_dir)
    run_id = uuid.uuid4().hex
    configuration = json.dumps(
        {"rehash": rehash, "retry_failed": retry_failed, "max_frames": max_frames, "max_pixels": max_pixels},
        sort_keys=True,
    )
    connection = opened.connection()
    db.reset_stuck(connection)
    db.start_run(connection, run_id, configuration)
    counts: Counter[str] = Counter()
    failures: list[dict[str, str | None]] = []
    try:
        scanned = scan(root)
        for entry in scanned.skipped:
            with db.transaction(connection):
                connection.execute(
                    "INSERT INTO skipped_entries VALUES (?,?,?,?)",
                    (run_id, entry.path.relative_to(root).as_posix(), entry.kind, entry.reason or "ERROR"),
                )
        regular = [entry.path for entry in scanned.files]
        classified = classify_files(root, regular)
        hashes: dict[Path, str | None] = {}
        stats = {path: path.stat(follow_symlinks=False) for path in regular}
        db.mark_scan_complete(
            connection, {item.relative_path for item in classified if item.category in {"IMAGE", "VIDEO"}}
        )
        for item in classified:
            counts[item.category] += 1
            stat = stats[item.path]
            media_id = db.upsert_media(connection, item.relative_path, stat.st_size, stat.st_mtime_ns)
            hash_result = hashing.sha256_file(item.path)
            if hash_result.status != "OK":
                db.mark_hash_changed(connection, media_id)
                hashes[item.path] = None
                continue
            hashes[item.path] = hash_result.sha256
            _update(
                connection,
                media_id,
                sha256=hash_result.sha256,
                extension=item.extension,
                warnings=_warning(None, item.warnings),
            )
            if item.category == "JSON":
                continue
            if item.category in {"HASH_ONLY", "UNSUPPORTED"}:
                with db.transaction(connection):
                    connection.execute(
                        "INSERT OR REPLACE INTO unsupported_files("
                        "relative_path,extension,category,size,sha256,reason) VALUES (?,?,?,?,?,?)",
                        (item.relative_path, item.extension, item.category, stat.st_size, hash_result.sha256, None),
                    )
        json_records = discover_json(
            root,
            [
                (item.path, stats[item.path].st_size, hashes.get(item.path))
                for item in classified
                if item.category == "JSON"
            ],
        )
        for record in json_records:
            with db.transaction(connection):
                connection.execute(
                    "INSERT OR REPLACE INTO json_files("
                    "relative_path,size,sha256,kind,source_present) VALUES (?,?,?,?,1)",
                    (record.relative_path, record.size, record.sha256, record.kind),
                )
        media_records = [item for item in classified if item.category in {"IMAGE", "VIDEO"}]
        claims: defaultdict[str, list[int]] = defaultdict(list)
        media_paths: dict[int, Path] = {}
        for item in media_records:
            stat = stats[item.path]
            media_id = db.upsert_media(connection, item.relative_path, stat.st_size, stat.st_mtime_ns)
            media_paths[media_id] = item.path
            match = match_metadata(item.path, json_records)
            if match.record is not None:
                claims[match.record.relative_path].append(media_id)
                fields = parsed_fields(match.record)
                _update(
                    connection,
                    media_id,
                    metadata_status=match.status,
                    metadata_path=match.record.relative_path,
                    raw_metadata_json=match.record.raw,
                    match_method=match.method,
                    match_confidence=match.confidence,
                    match_reason=match.reason,
                    **fields,
                )
            else:
                _update(
                    connection,
                    media_id,
                    metadata_status=match.status,
                    match_method=match.method,
                    match_confidence=match.confidence,
                    match_reason=match.reason,
                )
            if item.category == "VIDEO":
                _update(
                    connection, media_id, verification_status="UNVERIFIED", reason="VIDEO_VALIDATION_NOT_IMPLEMENTED"
                )
            elif stat.st_size == 0:
                _update(connection, media_id, verification_status="FAILED_VERIFICATION", reason="ZERO_BYTE")
            else:
                existing = connection.execute(
                    "SELECT size, mtime_ns, verification_status FROM media WHERE media_id=?", (media_id,)
                ).fetchone()
                if (
                    not rehash
                    and existing is not None
                    and existing["size"] == stat.st_size
                    and existing["mtime_ns"] == stat.st_mtime_ns
                    and existing["verification_status"] == "VERIFIED"
                ):
                    continue
                if (
                    not retry_failed
                    and existing is not None
                    and existing["verification_status"] == "FAILED_VERIFICATION"
                ):
                    continue
                image = verify_image(item.path, max_frames=max_frames, max_pixels=max_pixels)
                _update(
                    connection,
                    media_id,
                    detected_format=image.detected_format,
                    width=image.width,
                    height=image.height,
                    frame_count=image.frame_count,
                    verification_status=image.status,
                    reason=image.reason,
                    last_error=image.last_error,
                )
                if image.detected_format and image.detected_format.lower() != item.extension.lower():
                    _update(
                        connection,
                        media_id,
                        warnings=_warning(
                            _status_field(connection, item.relative_path, "warnings"),
                            ("EXTENSION_FORMAT_MISMATCH",),
                        ),
                    )
                if image.status != "VERIFIED":
                    failures.append(
                        {
                            "relative_path": item.relative_path,
                            "media_type": item.category,
                            "status": image.status,
                            "reason": image.reason,
                            "last_error": image.last_error,
                        }
                    )
        for path, media_ids in claims.items():
            with db.transaction(connection):
                connection.execute(
                    "UPDATE json_files SET claimed_by_count=? WHERE relative_path=?", (len(media_ids), path)
                )
            paths_for_claim = [media_paths[media_id] for media_id in media_ids]
            expected_edit = len(paths_for_claim) == 2 and any(
                edited.stem.endswith("-edited") and edited.stem.removesuffix("-edited") == other.stem
                for edited in paths_for_claim
                for other in paths_for_claim
                if edited != other
            )
            if len(media_ids) > 1 and not expected_edit:
                for media_id in media_ids:
                    _update(connection, media_id, metadata_status="AMBIGUOUS", match_reason="JSON_CLAIMED_BY_MULTIPLE")
        groups: defaultdict[str, list[str]] = defaultdict(list)
        for item in classified:
            digest = hashes.get(item.path)
            if digest:
                groups[digest].append(item.relative_path)
        with db.transaction(connection):
            connection.execute("DELETE FROM duplicate_groups")
            connection.execute("DELETE FROM duplicate_members")
            for digest, paths in sorted(groups.items()):
                if len(paths) > 1:
                    connection.execute("INSERT INTO duplicate_groups VALUES (?,?,?)", (digest, len(paths), "LOCAL"))
                    for path in sorted(paths):
                        connection.execute("INSERT INTO duplicate_members VALUES (?,?)", (digest, path))
        counts["skipped_entries"] = len(scanned.skipped)
        counts["duplicate_groups"] = sum(len(paths) > 1 for paths in groups.values())
        counts["verified_images"] = sum(
            1
            for item in media_records
            if item.category == "IMAGE" and _status(connection, item.relative_path) == "VERIFIED"
        )
        payload = {"run_id": run_id, "counts": dict(counts), "failures": failures}
        report_paths = reporting.write_reports(report_dir, run_id, payload)
        db.finish_run(connection, run_id, "COMPLETED", json.dumps(dict(counts), sort_keys=True))
        return {
            "run_id": run_id,
            "counts": dict(counts),
            "reports": {key: str(value) for key, value in report_paths.items()},
        }
    except BaseException as error:
        db.finish_run(connection, run_id, "FAILED", json.dumps(dict(counts), sort_keys=True), str(error))
        raise
    finally:
        connection.close()
        opened.close()


def _status(connection: sqlite3.Connection, path: str) -> str | None:
    row = connection.execute("SELECT verification_status FROM media WHERE path=?", (path,)).fetchone()
    return row[0] if row else None


def _status_field(connection: sqlite3.Connection, path: str, field: str) -> str | None:
    if field not in {"warnings"}:
        raise ValueError("unsupported media field")
    row = connection.execute(f"SELECT {field} FROM media WHERE path=?", (path,)).fetchone()  # noqa: S608
    return row[0] if row else None
