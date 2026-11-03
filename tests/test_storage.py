"""Unit tests for app/storage.py: on-disk names and the migration from files/<uuid>."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import storage  # noqa: E402


def _db(tmp_path: Path, rows: list[tuple[str, str, str | None]]) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "index.db")
    conn.execute(
        "CREATE TABLE files (id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,"
        " ext TEXT NOT NULL DEFAULT '', mtime REAL NOT NULL DEFAULT 0, disk_name TEXT UNIQUE)"
    )
    conn.executemany("INSERT INTO files (id, name, disk_name) VALUES (?, ?, ?)", rows)
    conn.commit()
    return conn


def _disk_names(conn: sqlite3.Connection) -> dict[str, str | None]:
    return dict(conn.execute("SELECT id, disk_name FROM files").fetchall())


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Budget 2026.ods", "Budget 2026.ods"),
        ("Café notes.odt", "Café notes.odt"),
        ("../../etc/passwd", "etcpasswd"),
        ("..", "document"),
        (".hidden.odt", "hidden.odt"),
        ("a\\b\0c\nd.odt", "abcd.odt"),
        ("  spaced  .odt ", "spaced.odt"),
    ],
)
def test_safe_disk_name(name: str, expected: str) -> None:
    assert storage.safe_disk_name(name) == expected


def test_safe_disk_name_caps_length_and_keeps_extension() -> None:
    capped = storage.safe_disk_name("é" * 300 + ".docx")
    assert capped.endswith(".docx") and len(capped.encode()) <= storage.MAX_NAME_BYTES
    capped.encode().decode()  # no split characters


def test_migrate_moves_legacy_files_to_their_names(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    for file_id, body in (("a" * 32, b"one"), ("b" * 32, b"two"), ("c" * 32, b"three")):
        (files / file_id).write_bytes(body)
    conn = _db(
        tmp_path,
        [("a" * 32, "Report.odt", None), ("b" * 32, "Report.odt", None), ("c" * 32, "Plan.ods", None)],
    )
    # A file that isn't ours stays put, and its name isn't taken.
    (files / "Plan.ods").write_bytes(b"not a document")

    assert storage.migrate(conn, files) == 3
    names = _disk_names(conn)
    assert sorted(names.values()) == ["Plan (2).ods", "Report (2).odt", "Report.odt"]
    assert {(files / names[i]).read_bytes() for i in names} == {b"one", b"two", b"three"}
    assert sorted(p.name for p in files.iterdir()) == ["Plan (2).ods", "Plan.ods", "Report (2).odt", "Report.odt"]

    # Idempotent.
    assert storage.migrate(conn, files) == 0
    assert _disk_names(conn) == names


def test_migrate_finishes_an_interrupted_run(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    linked, updated, missing = "a" * 32, "b" * 32, "c" * 32
    # Crashed after linking the new name, before updating the index.
    (files / linked).write_bytes(b"linked")
    (files / "Linked.odt").hardlink_to(files / linked)
    # Crashed after updating the index, before removing the old name.
    (files / updated).write_bytes(b"updated")
    (files / "Updated.odt").hardlink_to(files / updated)
    # Crashed mid-write of a new document.
    (files / storage.STAGING_DIR).mkdir()
    (files / storage.STAGING_DIR / "tmpabc").write_bytes(b"partial")
    conn = _db(
        tmp_path,
        [(linked, "Linked.odt", None), (updated, "Updated.odt", "Updated.odt"), (missing, "Gone.odt", None)],
    )

    storage.migrate(conn, files)
    assert _disk_names(conn) == {linked: "Linked.odt", updated: "Updated.odt", missing: None}
    assert sorted(p.name for p in files.iterdir()) == [storage.STAGING_DIR, "Linked.odt", "Updated.odt"]
    assert list((files / storage.STAGING_DIR).iterdir()) == []
    assert (files / "Linked.odt").read_bytes() == b"linked"


def test_store_rename_and_paths(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    conn = _db(tmp_path, [])
    for file_id in ("a" * 32, "b" * 32):
        with storage.new_staging_file(files) as out:
            out.write(file_id.encode())
        disk_name = storage.store_new(conn, files, Path(out.name), "Notes.odt", file_id)
        conn.execute("INSERT INTO files (id, name, disk_name) VALUES (?, ?, ?)", (file_id, "Notes.odt", disk_name))
    assert _disk_names(conn) == {"a" * 32: "Notes.odt", "b" * 32: "Notes (2).odt"}
    assert (files / "Notes.odt").stat().st_mode & 0o777 == 0o644

    row = {"id": "b" * 32, "disk_name": "Notes (2).odt"}
    assert storage.rename(conn, files, row, "Draft.odt", 0.0) == "Draft.odt"
    assert (files / "Draft.odt").read_bytes() == b"b" * 32
    assert not (files / "Notes (2).odt").exists()

    storage.replace_contents(files / "Draft.odt", b"new")
    assert (files / "Draft.odt").read_bytes() == b"new"
    assert list((files / storage.STAGING_DIR).iterdir()) == []

    assert storage.path_of(files, {"id": "c" * 32, "disk_name": None}) == files / ("c" * 32)
    with pytest.raises(ValueError):
        storage.path_of(files, {"id": "d" * 32, "disk_name": ".."})
