from __future__ import annotations

import ast
import hashlib
import multiprocessing
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from textwrap import dedent

import pytest

from archive_verifier import db, hashing, safety, scanner


def make_roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "input"
    source.mkdir()
    return source, tmp_path / "data" / "archive.db"


def lock_worker(db_path: str, root: str, output: multiprocessing.Queue[str], ready: multiprocessing.Event) -> None:
    try:
        opened = db.open_database(db_path, root)
    except db.DatabaseError:
        output.put("lost")
        ready.wait(10)
    else:
        output.put("won")
        ready.wait(10)
        opened.close()


def test_two_processes_contend_for_permanent_advisory_lock(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = context.Queue()
    ready = context.Event()
    processes = [context.Process(target=lock_worker, args=(str(db_path), str(source), queue, ready)) for _ in range(2)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    ready.set()
    for process in processes:
        process.join(20)
    assert sorted(results) == ["lost", "won"]
    assert db_path.with_name(db_path.name + ".lock").exists()
    assert all(process.exitcode == 0 for process in processes)


def test_lock_is_released_without_deleting_lock_file(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    opened = db.open_database(db_path, source)
    lock_path = db_path.with_name(db_path.name + ".lock")
    opened.close()
    assert lock_path.exists()
    reopened = db.open_database(db_path, source)
    reopened.close()


def test_scanner_accounts_each_entry_once_and_is_deterministic(tmp_path: Path) -> None:
    source, _ = make_roots(tmp_path)
    (source / "z").mkdir()
    (source / "a").mkdir()
    (source / "z" / "file.txt").write_text("x", encoding="utf-8")
    (source / "a" / "other.bin").write_bytes(b"x")
    result = scanner.scan(source)
    all_disk_entries = {path for path in source.rglob("*")}
    accounted = {entry.path for entry in result.entries}
    assert accounted == all_disk_entries
    assert len(result.entries) == len(accounted)
    assert len(result.directories) == 3
    assert [entry.path.name for entry in result.files] == ["other.bin", "file.txt"]


def test_scanner_skips_links_and_rejects_link_root(tmp_path: Path) -> None:
    source, _ = make_roots(tmp_path)
    target = source / "file"
    target.write_bytes(b"x")
    try:
        link = source / "link"
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted")
    result = scanner.scan(source)
    assert [(entry.kind, entry.reason) for entry in result.skipped] == [("LINK", "REPARSE_POINT")]
    with pytest.raises(safety.SafetyError):
        scanner.scan(link)


def test_scanner_permission_error_is_always_accounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, _ = make_roots(tmp_path)
    blocked = source / "blocked"
    blocked.mkdir()
    original = os.scandir

    def denied(path: str | os.PathLike[str]) -> object:
        if Path(path) == blocked:
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    result = scanner.scan(source)
    assert any(entry.path == blocked and entry.reason == "PERMISSION_ERROR" for entry in result.skipped)
    assert sum(entry.path == blocked for entry in result.entries) == 1


def test_hashing_detects_source_swap(tmp_path: Path) -> None:
    target = tmp_path / "large.bin"
    target.write_bytes(b"a" * 8192)
    changed = False

    def hook(bytes_read: int) -> None:
        nonlocal changed
        if bytes_read >= 1024 and not changed:
            target.write_bytes(b"b" * 8192)
            changed = True

    result = hashing.sha256_file(target, chunk_size=1024, chunk_hook=hook)
    assert result.status == "FILE_CHANGED_DURING_HASH"
    assert result.sha256 is None
    assert result.bytes_read >= 1024


def test_hashing_returns_verified_result(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"photo")
    result = hashing.sha256_file(target)
    assert result.status == "OK"
    assert result.sha256 == hashlib.sha256(b"photo").hexdigest()
    assert result.bytes_read == 5


def test_database_schema_and_cas_with_eight_threads(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    opened = db.open_database(db_path, source)
    setup = opened.connection()
    media_id = db.upsert_media(setup, "file", 1, 1)
    setup.close()
    results: list[bool] = []
    lock = threading.Lock()

    def race() -> None:
        connection = opened.connection()
        try:
            result = db.transition(connection, media_id, "DISCOVERED", "HASHING")
            with lock:
                results.append(result)
        finally:
            connection.close()

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert setup is not None
    opened.close()


def test_database_checks_and_nonreentrant_transaction(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    opened = db.open_database(db_path, source)
    connection = opened.connection()
    with pytest.raises(ValueError):
        db.transition(connection, 1, "UNKNOWN", "HASHING")
    with db.transaction(connection), pytest.raises(db.DatabaseError, match="nested"), db.transaction(connection):
        pass
    assert connection.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA
    stored_root = connection.execute("SELECT input_root, created_at FROM meta").fetchone()[0]
    assert stored_root == os.path.normcase(str(source.resolve()))
    connection.close()
    opened.close()


def test_changed_upsert_and_vanished_file_lifecycle(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    opened = db.open_database(db_path, source)
    connection = opened.connection()
    media_id = db.upsert_media(connection, "file", 1, 1)
    db.transition(connection, media_id, "DISCOVERED", "HASHING")
    db.transition(connection, media_id, "HASHING", "VERIFYING")
    db.transition(
        connection,
        media_id,
        "VERIFYING",
        "DONE",
        verification_status="VERIFIED",
        verified_at="2026-01-01T00:00:00Z",
    )
    db.mark_scan_complete(connection, set())
    row = connection.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
    assert row["source_present"] == 0
    db.upsert_media(connection, "file", 2, 2)
    row = connection.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
    assert row["source_present"] == 1
    assert row["stage"] == "DISCOVERED"
    assert row["verification_status"] == "PENDING"
    assert row["sha256"] is None
    connection.close()
    opened.close()


def test_new_db_does_not_modify_existing_db(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    original = db.open_database(db_path, source)
    original.close()
    before = db_path.read_bytes()
    replacement = db.open_database(db_path, source, new_db=True)
    assert replacement.path != db_path
    replacement.close()
    assert db_path.read_bytes() == before


def test_v1_database_refuses_missing_root_migration(tmp_path: Path) -> None:
    source, db_path = make_roots(tmp_path)
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (1)")
    connection.commit()
    connection.close()
    with pytest.raises(db.MigrationError, match="new_db"):
        db.open_database(db_path, source)


@pytest.mark.parametrize("bad", ["db.sqlite", "archive", "reports", "logs"])
def test_output_roots_inside_input_are_rejected(tmp_path: Path, bad: str) -> None:
    source, db_path = make_roots(tmp_path)
    value = source / bad
    with pytest.raises(safety.SafetyError):
        if bad == "db.sqlite":
            safety.configure(source, value)
        elif bad == "archive":
            safety.configure(source, db_path, archive_root=value)
        elif bad == "reports":
            safety.configure(source, db_path, report_dir=value)
        else:
            safety.configure(source, db_path, log_dir=value)


def _audit_source(
    source: str,
    allowed_destructive: dict[tuple[str, str], int],
    allowed_create: dict[tuple[str, str], int],
    module: str = "snippet.py",
) -> None:
    tree = ast.parse(dedent(source))
    aliases: dict[str, str] = {}
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(item, ast.ImportFrom) and item.module:
            for alias in item.names:
                aliases[alias.asname or alias.name] = f"{item.module}.{alias.name}"
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def function(node: ast.AST) -> str:
        current = parents.get(node)
        name = ""
        while current is not None:
            if isinstance(current, ast.ClassDef):
                name = current.name
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return f"{name}.{current.name}" if name else current.name
            current = parents.get(current)
        return "<module>"

    counts: dict[tuple[str, str], int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            base = (
                node.func.value.id
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                else ""
            )
            qualified = f"{aliases.get(base, base)}.{attr}" if base else aliases.get(getattr(node.func, "id", ""), "")
            key = (module, function(node))
            if attr == "mkdir":
                counts[key] = counts.get(key, 0) + 1
                assert key in allowed_create and counts[key] <= allowed_create[key]
            elif (
                qualified in {
                    "os.remove", "os.unlink", "os.rename", "os.replace", "os.rmdir", "os.truncate",
                    "os.link", "os.symlink", "os.chmod", "os.utime",
                }
                or base == "os"
                and attr in {
                    "remove",
                    "unlink",
                    "rename",
                    "replace",
                    "rmdir",
                    "truncate",
                    "link",
                    "symlink",
                    "chmod",
                    "utime",
                }
            ) or attr in {
                "unlink",
                "write_text",
                "write_bytes",
                "touch",
                "symlink_to",
                "hardlink_to",
                "rmtree",
                "move",
            }:
                counts[key] = counts.get(key, 0) + 1
                assert key in allowed_destructive and counts[key] <= allowed_destructive[key]
            if (isinstance(node.func, ast.Name) and node.func.id == "open") or (
                isinstance(node.func, ast.Attribute) and attr == "open"
            ):
                mode = (
                    node.args[1]
                    if isinstance(node.func, ast.Name) or base == "io"
                    else node.args[0]
                    if node.args
                    else next((item.value for item in node.keywords if item.arg == "mode"), None)
                )
                if not isinstance(mode, ast.Constant) or mode.value not in {"r", "rb"}:
                    counts[key] = counts.get(key, 0) + 1
                    assert key in allowed_create and counts[key] <= allowed_create[key]
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "__import__"}
            ) or qualified.startswith(("subprocess.", "os.system", "os.popen", "os.exec", "os.spawn")):
                raise AssertionError("dynamic execution/import")
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"os", "sys", "subprocess", "shutil", "importlib"}
            ):
                raise AssertionError("dynamic attribute access")
            if isinstance(node.func, ast.Attribute) and attr == "replace":
                receiver = node.func.value
                if (
                    isinstance(receiver, ast.Name)
                    and receiver.id == "os"
                    or isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "Path"
                ):
                    raise AssertionError("filesystem replace")


def test_static_safety_audit_and_self_tests() -> None:
    root = Path(__file__).parents[1] / "src"
    destructive = {("safety.py", "cleanup_partial"): 1}
    create = {("db.py", "acquire"): 1, ("db.py", "open_database"): 1}
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        _audit_source(
            source,
            {(path.name, key[1]): value for key, value in destructive.items() if key[0] == path.name},
            {(path.name, key[1]): value for key, value in create.items() if key[0] == path.name},
            path.name,
        )
    _audit_source('name.replace("a", "b"); data.copy()', {}, {})
    with pytest.raises(AssertionError):
        _audit_source('Path("a").replace("b")', {}, {})
    with pytest.raises(AssertionError):
        _audit_source('os.replace("a", "b")', {}, {})
    with pytest.raises(AssertionError):
        _audit_source('getattr(os, "remove")("a")', {}, {})
    with pytest.raises(AssertionError):
        _audit_source("from os import remove as rm\nrm('a')", {}, {})
    with pytest.raises(AssertionError):
        _audit_source("open('a', 'w')", {}, {})


def test_no_network_imports_under_src() -> None:
    forbidden = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "smtplib", "websockets"}
    for path in (Path(__file__).parents[1] / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden


def test_repository_hygiene() -> None:
    result = subprocess.run(  # noqa: S607
        ["git", "ls-files"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    forbidden_extensions = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".lock", ".zip"}
    for name in result.stdout.splitlines():
        path = Path(name)
        assert path.suffix.lower() not in forbidden_extensions
        assert path.stat().st_size <= 5 * 1024 * 1024
        assert path.suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".mp4",
            ".mov",
        } or "tests/fixtures/" in name.replace("\\", "/")
