from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from archive_verifier import db, hashing, safety, scanner


def setup_policy(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    safety.configure(source, tmp_path / "archive", tmp_path / "data" / "x.db", tmp_path / "reports", tmp_path / "logs")


def test_scan_skips_symlink_and_hashes_file(tmp_path: Path) -> None:
    setup_policy(tmp_path)
    source = tmp_path / "input"
    target = source / "photo.jpg"
    target.write_bytes(b"photo")
    try:
        (source / "link").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted")
    files, links = scanner.scan(source)
    assert [item.path for item in files] == [target]
    assert [item.path for item in links] == [source / "link"]
    assert hashing.sha256_file(target) == hashlib.sha256(b"photo").hexdigest()


def test_database_cas_transition(tmp_path: Path) -> None:
    setup_policy(tmp_path)
    connection = db.connect(tmp_path / "data" / "x.db")
    media_id = db.upsert_media(connection, "photo.jpg", 5, 1)
    assert db.transition(connection, media_id, "DISCOVERED", "HASHING")
    assert not db.transition(connection, media_id, "DISCOVERED", "HASHING")
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_safety_rejects_input_and_overlapping_roots(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    safety.configure(source, tmp_path / "archive", tmp_path / "data" / "x.db", tmp_path / "reports", tmp_path / "logs")
    with pytest.raises(safety.SafetyError):
        safety.assert_write_allowed(source / "new.txt", "test")
    with pytest.raises(safety.SafetyError):
        safety.configure(source, source / "archive", tmp_path / "data" / "x.db", tmp_path / "reports", tmp_path / "logs")


def test_static_filesystem_safety_audit() -> None:
    root = Path(__file__).parents[1] / "archive_verifier"
    allowed = {("safety.py", "cleanup_partial"), ("db.py", "connect"), ("db.py", "initialize"),
               ("db.py", "upsert_media"), ("db.py", "transition")}
    forbidden = {"remove", "unlink", "rmdir", "removedirs", "rename", "replace", "truncate",
                 "rmtree", "move", "copy", "copy2", "copyfile", "copytree", "system"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases: dict[str, str] = {}
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def function_name(node: ast.AST) -> str:
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return parent.name
                parent = parents.get(parent)
            return ""

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for item in node.names:
                    aliases[item.asname or item.name] = f"os.{item.name}"
        def assert_allowed(node: ast.AST, reason: str) -> None:
            assert (path.name, function_name(node)) in allowed, f"{reason} in {path.name}:{function_name(node)}"

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else aliases.get(getattr(node.func, "id", ""), "")
            if name.startswith("os.") and name.split(".", 1)[1] in forbidden:
                assert_allowed(node, f"forbidden {name}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden:
                assert_allowed(node, "forbidden method")
            if getattr(node.func, "id", None) == "open":
                mode = next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and any(flag in mode.value for flag in "wax+"):
                    assert_allowed(node, "forbidden open mode")
            if any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                   for keyword in node.keywords):
                assert_allowed(node, "shell=True")