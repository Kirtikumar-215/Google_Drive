"""SQLite state store. Each caller owns its connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import safety


SCHEMA_VERSION = 1


def connect(path: str | Path) -> sqlite3.Connection:
    target = safety.assert_write_allowed(path, "SQLite database")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.row_factory = sqlite3.Row
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS media (
            media_id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT,
            stage TEXT NOT NULL DEFAULT 'DISCOVERED',
            verification_status TEXT NOT NULL DEFAULT 'PENDING',
            copy_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
            metadata_status TEXT,
            reason TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS unsupported_files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL
        );
        """
    )
    row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported schema version: {row[0]}")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite quick_check failed")
    connection.commit()


def upsert_media(connection: sqlite3.Connection, path: str, size: int, mtime_ns: int) -> int:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """INSERT INTO media(path, size, mtime_ns) VALUES (?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns
           WHERE media.size != excluded.size OR media.mtime_ns != excluded.mtime_ns""",
        (path, size, mtime_ns),
    )
    media_id = connection.execute("SELECT media_id FROM media WHERE path=?", (path,)).fetchone()[0]
    connection.commit()
    return media_id


def transition(connection: sqlite3.Connection, media_id: int, old_stage: str,
               new_stage: str, **fields: str | None) -> bool:
    assignments = ["stage=?"]
    values: list[str | None] = [new_stage]
    for name, value in fields.items():
        if name not in {"verification_status", "copy_status", "metadata_status", "reason", "last_error", "sha256"}:
            raise ValueError(f"invalid media field: {name}")
        assignments.append(f"{name}=?")
        values.append(value)
    values.extend((media_id, old_stage))
    connection.execute("BEGIN IMMEDIATE")
    cursor = connection.execute(
        f"UPDATE media SET {', '.join(assignments)} WHERE media_id=? AND stage=?", values
    )
    connection.commit()
    return cursor.rowcount == 1
