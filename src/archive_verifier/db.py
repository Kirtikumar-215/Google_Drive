"""SQLite state, migrations, and the single-instance advisory lock."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from . import safety

CURRENT_SCHEMA = 2
STAGES = ("DISCOVERED", "HASHING", "COPYING", "VERIFYING", "DONE")
VERIFICATION_STATUSES = ("PENDING", "VERIFIED", "RETRY_PENDING", "FAILED_VERIFICATION", "UNVERIFIED", "ERROR")
COPY_STATUSES = ("NOT_REQUESTED", "PENDING", "COPIED", "ALREADY_PRESENT", "DESTINATION_COLLISION", "ERROR")


class DatabaseError(RuntimeError):
    """Base database application error."""


class MigrationError(DatabaseError):
    """Raised when an existing database cannot be migrated safely."""


class InstanceLock:
    """A permanent lock file whose advisory OS lock is released on close."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def acquire(self) -> None:
        handle = self.path.open("a+b")
        self.handle = handle
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except (BlockingIOError, OSError) as error:
            handle.close()
            self.handle = None
            raise DatabaseError(f"database is already locked: {self.path}") from error

    def close(self) -> None:
        handle = self.handle
        if handle is not None:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            handle.close()
            self.handle = None


class Database:
    def __init__(self, path: Path, input_root: Path, lock: InstanceLock) -> None:
        self.path = path
        self.input_root = input_root
        self._lock = lock
        self._closed = False

    def connection(self) -> sqlite3.Connection:
        if self._closed:
            raise DatabaseError("database is closed")
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=True)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._lock.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _root_key(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.normcase(os.path.abspath(os.fspath(path))))


def _new_path(path: Path) -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.stem}-{stamp}-{os.getpid()}{path.suffix}")


def _create_schema(connection: sqlite3.Connection, input_root: Path) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("CREATE TABLE meta (input_root TEXT NOT NULL, created_at TEXT NOT NULL)")
    connection.execute(
        """CREATE TABLE media (
            media_id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT,
            stage TEXT NOT NULL DEFAULT 'DISCOVERED' CHECK(stage IN (
                'DISCOVERED','HASHING','COPYING','VERIFYING','DONE')),
            verification_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(verification_status IN (
                'PENDING','VERIFIED','RETRY_PENDING','FAILED_VERIFICATION','UNVERIFIED','ERROR')),
            copy_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED' CHECK(copy_status IN (
                'NOT_REQUESTED','PENDING','COPIED','ALREADY_PRESENT','DESTINATION_COLLISION','ERROR')),
            metadata_status TEXT,
            reason TEXT,
            last_error TEXT,
            verification_attempts INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT,
            source_present INTEGER NOT NULL DEFAULT 1 CHECK(source_present IN (0,1))
        )"""
    )
    connection.execute("INSERT INTO meta(input_root, created_at) VALUES (?, ?)", (str(input_root), _utc_now()))
    connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA}")
    connection.commit()


def _migrate(connection: sqlite3.Connection, input_root: Path) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if (
        version == 0
        and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    ):
        raise MigrationError("v1 database has no stored input_root; use new_db=True")
    if version not in (0, CURRENT_SCHEMA):
        raise MigrationError(f"unsupported schema version {version}; use new_db=True")
    if version == 0:
        _create_schema(connection, input_root)


def open_database(
    db_path: str | os.PathLike[str], input_root: str | os.PathLike[str], new_db: bool = False
) -> Database:
    """Lock, validate, migrate, and open a database with per-thread connections."""
    root = _root_key(input_root)
    if not root.is_dir() or safety._is_reparse(Path(input_root)):
        raise safety.SafetyError("input root must be a real directory")
    requested = safety._normalized(db_path)
    if new_db and requested.exists():
        requested = _new_path(requested)
    safety.configure(root, requested)
    safety.assert_write_allowed(requested.parent, "database directory")
    requested.parent.mkdir(parents=True, exist_ok=True)
    lock_path = requested.with_name(requested.name + ".lock")
    safety.assert_write_allowed(lock_path, "permanent lock file")
    lock = InstanceLock(lock_path)
    lock.acquire()
    try:
        connection = sqlite3.connect(requested, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if row is None:
            _migrate(connection, root)
        else:
            stored = connection.execute("SELECT input_root FROM meta LIMIT 1").fetchone()
            if stored is None or _root_key(stored[0]) != root:
                raise DatabaseError("input root differs from database; use new_db=True")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != CURRENT_SCHEMA:
                _migrate(connection, root)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DatabaseError("SQLite quick_check failed")
        connection.close()
        return Database(requested, root, lock)
    except BaseException:
        connection.close()
        lock.close()
        raise


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run one non-reentrant immediate transaction."""
    if connection.in_transaction:
        raise DatabaseError("nested transactions are not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def upsert_media(connection: sqlite3.Connection, path: str, size: int, mtime_ns: int) -> int:
    with transaction(connection):
        row = connection.execute("SELECT * FROM media WHERE path=?", (path,)).fetchone()
        if row is None:
            cursor = connection.execute("INSERT INTO media(path,size,mtime_ns) VALUES (?,?,?)", (path, size, mtime_ns))
            if cursor.lastrowid is None:
                raise DatabaseError("SQLite did not return a media id")
            return int(cursor.lastrowid)
        if row["size"] != size or row["mtime_ns"] != mtime_ns:
            connection.execute(
                """UPDATE media SET size=?, mtime_ns=?, sha256=NULL, stage='DISCOVERED',
                   verification_status='PENDING', metadata_status=NULL, reason=NULL,
                   last_error=NULL, verification_attempts=0, copy_status='NOT_REQUESTED',
                   verified_at=NULL, source_present=1 WHERE media_id=?""",
                (size, mtime_ns, row["media_id"]),
            )
        else:
            connection.execute("UPDATE media SET source_present=1 WHERE media_id=?", (row["media_id"],))
        return int(row["media_id"])


def mark_scan_complete(connection: sqlite3.Connection, seen_paths: set[str]) -> None:
    with transaction(connection):
        connection.execute("UPDATE media SET source_present=0")
        for path in seen_paths:
            connection.execute("UPDATE media SET source_present=1 WHERE path=?", (path,))


def transition(
    connection: sqlite3.Connection, media_id: int, old_stage: str, new_stage: str, **fields: str | int | None
) -> bool:
    if old_stage not in STAGES or new_stage not in STAGES:
        raise ValueError("unknown stage")
    allowed = {"verification_status": VERIFICATION_STATUSES, "copy_status": COPY_STATUSES}
    assignments = ["stage=?"]
    values: list[str | int | None] = [new_stage]
    for name, value in fields.items():
        if name in allowed and value not in allowed[name]:
            raise ValueError(f"unknown {name}")
        if name not in {
            "verification_status",
            "copy_status",
            "metadata_status",
            "reason",
            "last_error",
            "sha256",
            "verification_attempts",
            "verified_at",
        }:
            raise ValueError(f"invalid media field: {name}")
        assignments.append(f"{name}=?")
        values.append(value)
    values.extend((media_id, old_stage))
    with transaction(connection):
        cursor = connection.execute(f"UPDATE media SET {', '.join(assignments)} WHERE media_id=? AND stage=?", values)  # noqa: S608
        return cursor.rowcount == 1


def mark_hash_changed(connection: sqlite3.Connection, media_id: int) -> None:
    with transaction(connection):
        connection.execute(
            """UPDATE media SET stage='DISCOVERED', reason='FILE_CHANGED_DURING_HASH',
               verification_attempts=verification_attempts+1 WHERE media_id=?""",
            (media_id,),
        )
