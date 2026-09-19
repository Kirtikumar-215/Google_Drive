from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image

from archive_verifier import db
from archive_verifier.classification import classify_files
from archive_verifier.m2 import run_m2
from archive_verifier.metadata import discover_json, match_metadata
from archive_verifier.reporting import NOTICE


def image(path: Path, color: str = "red") -> None:
    Image.new("RGB", (8, 6), color).save(path, format="JPEG")


def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "input"
    root.mkdir()
    return root, tmp_path / "data" / "archive.db", tmp_path / "reports"


def test_m2_migration_preserves_v1_and_adds_schema(tmp_path: Path) -> None:
    root, db_path, _ = fixture(tmp_path)
    db_path.parent.mkdir()
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE meta (input_root TEXT NOT NULL, created_at TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES (?, ?)", (str(root.resolve()).lower(), "2026-01-01T00:00:00Z"))
    connection.execute(
        "CREATE TABLE media (media_id INTEGER PRIMARY KEY, path TEXT UNIQUE, size INTEGER, mtime_ns INTEGER, "
        "sha256 TEXT, stage TEXT, verification_status TEXT, copy_status TEXT, metadata_status TEXT, reason TEXT, "
        "last_error TEXT)"
    )
    connection.execute(
        "INSERT INTO media VALUES (1,'old.jpg',3,4,'abc','DISCOVERED','PENDING','NOT_REQUESTED',NULL,NULL,NULL)"
    )
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()
    opened = db.open_database(db_path, root)
    connection = opened.connection()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(media)")}
    assert {"extension", "metadata_path", "raw_metadata_json", "verified_at"} <= columns
    preserved = connection.execute("SELECT path, sha256 FROM media WHERE media_id=1").fetchone()
    assert (preserved["path"], preserved["sha256"]) == ("old.jpg", "abc")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    for table in ("json_files", "skipped_entries", "duplicate_groups", "duplicate_members", "runs"):
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
    connection.execute("INSERT INTO media(path,size,mtime_ns,metadata_status) VALUES ('x',1,1,'MATCHED')")
    try:
        connection.execute("INSERT INTO media(path,size,mtime_ns,metadata_status) VALUES ('y',1,1,'BAD')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("invalid metadata status accepted")
    assert connection.execute("SELECT 1 FROM orphan_json").fetchone() is None
    connection.close()
    opened.close()


def test_m2_migration_failure_rolls_back(tmp_path: Path) -> None:
    root, db_path, _ = fixture(tmp_path)
    db_path.parent.mkdir()
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE meta (input_root TEXT NOT NULL, created_at TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES (?, ?)", (str(root.resolve()).lower(), "2026-01-01T00:00:00Z"))
    connection.execute("CREATE TABLE media (media_id INTEGER PRIMARY KEY, path TEXT, size INTEGER, mtime_ns INTEGER)")
    connection.execute("CREATE TABLE unsupported_files (path TEXT)")
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()
    try:
        db.open_database(db_path, root)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("invalid migration unexpectedly succeeded")
    connection = sqlite3.connect(db_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='unsupported_files_v1'").fetchone() is None
    connection.close()


def test_classification_covers_extensions_and_motion_photo(tmp_path: Path) -> None:
    root, _, _ = fixture(tmp_path)
    names = [
        "a.JPG",
        "b.png",
        "c.gif",
        "d.webp",
        "e.heic",
        "f.heif",
        "g.bmp",
        "h.tiff",
        "i.avif",
        "j.mp4",
        "k.MOV",
        "l.mts",
        "m.dng",
        "n.JSON",
        "o.xyz",
        "motion.jpg",
        "motion.mp",
    ]
    for name in names:
        (root / name).write_bytes(b"x")
    result = classify_files(root, [path for path in root.iterdir()])
    by_name = {item.path.name: item for item in result}
    assert by_name["a.JPG"].category == "IMAGE"
    assert by_name["k.MOV"].category == "VIDEO"
    assert by_name["m.dng"].category == "HASH_ONLY"
    assert by_name["n.JSON"].category == "JSON"
    assert by_name["o.xyz"].category == "UNSUPPORTED"
    assert by_name["motion.mp"].warnings == ("MOTION_PHOTO_COMPANION",)


def test_metadata_tiers_and_title_cross_check(tmp_path: Path) -> None:
    root, _, _ = fixture(tmp_path)
    media = root / "photo.jpg"
    media.write_bytes(b"x")
    supplemental = root / "photo.jpg.supplemental-metadata.json"
    normal = root / "photo.jpg.json"
    supplemental.write_text('{"title":"photo.jpg"}', encoding="utf-8")
    normal.write_text('{"title":"wrong.jpg"}', encoding="utf-8")
    records = discover_json(
        root, [(supplemental, supplemental.stat().st_size, "a"), (normal, normal.stat().st_size, "b")]
    )
    result = match_metadata(media, records)
    assert result.status == "MATCHED" and result.method == "SUPPLEMENTAL"
    supplemental.unlink()
    result = match_metadata(media, discover_json(root, [(normal, normal.stat().st_size, "b")]))
    assert result.status == "AMBIGUOUS" and result.reason == "TITLE_MISMATCH"


def test_metadata_edit_numbered_orphan_and_invalid_json(tmp_path: Path) -> None:
    root, _, _ = fixture(tmp_path)
    original = root / "IMG_1234.jpg"
    edited = root / "IMG_1234-edited.jpg"
    numbered = root / "IMG_1234(1).jpg"
    for path in (original, edited, numbered):
        path.write_bytes(b"x")
    original_json = root / "IMG_1234.jpg.json"
    numbered_json = root / "IMG_1234.jpg(1).json"
    invalid = root / "orphan.json"
    original_json.write_text('{"title":"IMG_1234.jpg"}', encoding="utf-8")
    numbered_json.write_text('{"title":"IMG_1234.jpg"}', encoding="utf-8")
    invalid.write_text("{", encoding="utf-8")
    records = discover_json(
        root, [(path, path.stat().st_size, None) for path in (original_json, numbered_json, invalid)]
    )
    assert match_metadata(edited, records).status == "MATCHED"
    assert match_metadata(numbered, records).status == "MATCHED"
    assert next(item for item in records if item.path == invalid).kind == "INVALID_JSON"


def test_metadata_same_directory_and_deterministic_permutations(tmp_path: Path) -> None:
    root, _, _ = fixture(tmp_path)
    other = root / "other"
    other.mkdir()
    media = root / "photo.jpg"
    foreign = other / "photo.jpg.json"
    media.write_bytes(b"x")
    foreign.write_text('{"title":"photo.jpg"}', encoding="utf-8")
    records = discover_json(root, [(foreign, foreign.stat().st_size, "x")])
    assert match_metadata(media, records).status == "UNMATCHED"
    local = root / "photo.jpg.json"
    local.write_text('{"title":"photo.jpg"}', encoding="utf-8")
    all_records = discover_json(root, [(local, local.stat().st_size, "x"), (foreign, foreign.stat().st_size, "x")])
    assert match_metadata(media, list(reversed(all_records))) == match_metadata(media, all_records)


def test_m2_run_reports_duplicates_video_zero_byte_and_source_unchanged(tmp_path: Path) -> None:
    root, db_path, reports = fixture(tmp_path)
    image(root / "photo.jpg")
    (root / "copy.jpg").write_bytes((root / "photo.jpg").read_bytes())
    (root / "clip.mp4").write_bytes(b"not validated")
    (root / "empty.png").write_bytes(b"")
    (root / "photo.jpg.json").write_text(
        '{"title":"photo.jpg","creationTime":{"timestamp":"1700000000"}}', encoding="utf-8"
    )
    before = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    result = run_m2(root, db_path, reports)
    after = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert set(result["reports"]) == {"json", "csv", "html"}
    report_text = Path(result["reports"]["html"]).read_text(encoding="utf-8")
    assert NOTICE in report_text
    connection = sqlite3.connect(db_path)
    video = connection.execute("SELECT verification_status, reason FROM media WHERE path='clip.mp4'").fetchone()
    zero = connection.execute("SELECT verification_status, reason FROM media WHERE path='empty.png'").fetchone()
    assert video == ("UNVERIFIED", "VIDEO_VALIDATION_NOT_IMPLEMENTED")
    assert zero == ("FAILED_VERIFICATION", "ZERO_BYTE")
    assert connection.execute("SELECT COUNT(*) FROM duplicate_groups").fetchone()[0] == 1
    connection.close()


def test_csv_formula_injection_and_collision_safe_reports(tmp_path: Path) -> None:
    root, db_path, reports = fixture(tmp_path)
    image(root / "x.jpg")
    run_m2(root, db_path, reports)
    run_m2(root, db_path, reports)
    assert len(list(reports.glob("*.csv"))) == 2
    csv_bytes = next(reports.glob("*.csv")).read_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
