"""Where document bytes live on disk.

Each document is stored as ``files/<disk name>``, where the disk name is its
real filename made safe for a filesystem (``Budget 2026.ods``), with a
`` (2)`` suffix when two documents would otherwise share one.  Everything
else (URLs, WOPI file ids, share tokens, the index) keeps using the
document's uuid; only the index's ``disk_name`` column maps an id to a path.
Paths are only ever built from that column, never from request input.

Documents created before this layout are stored as ``files/<uuid>`` with
``disk_name`` NULL; ``migrate`` moves them to their names at startup.

Every move is crash-safe: the new name is hard-linked first, then the index
is updated, then the old name is removed.  A crash at any point leaves the
index pointing at a file that exists, and at worst an extra link to it,
which ``migrate`` removes at the next startup.  New bytes are written to ``files/.staging/``
and linked into place, so a half-written file never has a document's name.

Kept free of Quart so it can be unit-tested on its own.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from typing import IO
from typing import Any

# Most filesystems cap a name at 255 bytes; leave room for " (NNN)".
MAX_NAME_BYTES = 200
STAGING_DIR = ".staging"


def safe_disk_name(name: str) -> str:
    """``name`` made safe to use as a single path component.

    Drops ``/``, ``\\`` and control characters (NUL included), strips
    surrounding whitespace and leading dots (so no hidden files, ``.`` or
    ``..``), and caps the length in bytes, keeping the extension.  Unicode
    and inner spaces are kept.
    """
    cleaned = "".join(
        ch for ch in name if ch not in "/\\" and unicodedata.category(ch) != "Cc"
    )
    cleaned = cleaned.strip().lstrip(".").strip() or "document"
    stem, dot, ext = cleaned.rpartition(".")
    if not dot or len(ext.encode()) > 16:
        stem, dot, ext = cleaned, "", ""
    budget = MAX_NAME_BYTES - len((dot + ext).encode())
    stem = stem.encode()[:budget].decode(errors="ignore").rstrip() or "document"
    return stem + dot + ext


def _numbered(name: str, n: int) -> str:
    if n == 1:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name} ({n})"
    return f"{stem} ({n}).{ext}"


def _claimed(conn: sqlite3.Connection, disk_name: str, file_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM files WHERE disk_name = ? AND id != ? LIMIT 1",
        (disk_name, file_id),
    ).fetchone()
    return row is not None


def path_of(files_dir: Path, row: dict[str, Any]) -> Path:
    """The file holding a document's bytes, from its index row."""
    disk_name = row.get("disk_name") or row["id"]
    if disk_name in ("", ".", "..") or "/" in disk_name or "\0" in disk_name:
        raise ValueError(f"bad disk name for document {row['id']}")
    return files_dir / disk_name


def new_staging_file(files_dir: Path) -> IO[bytes]:
    """An open temp file in ``files/.staging/``, on the same filesystem as the documents."""
    staging = files_dir / STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    out = tempfile.NamedTemporaryFile(dir=staging, delete=False)
    # tempfile creates 0600; documents are readable like any other file
    # written here (and by whatever backs up the data dir).
    os.fchmod(out.fileno(), 0o644)
    return out


def link_into_place(
    conn: sqlite3.Connection, files_dir: Path, source: Path, name: str, file_id: str
) -> str:
    """Hard-link ``source`` into ``files/`` under a free name for ``name``; return that name.

    A name is free if no other document claims it and nothing is on disk
    there.  ``source`` itself is left in place for the caller to remove once
    the index points at the new name.
    """
    base = safe_disk_name(name)
    n = 1
    while True:
        candidate = _numbered(base, n)
        n += 1
        if _claimed(conn, candidate, file_id):
            continue
        dest = files_dir / candidate
        try:
            os.link(source, dest)
        except FileExistsError:
            # Already linked by an earlier, interrupted attempt for this same
            # file: reuse it.  Anything else on disk there isn't ours.
            if dest.samefile(source):
                return candidate
            continue
        return candidate


def store_new(
    conn: sqlite3.Connection, files_dir: Path, staged: Path, name: str, file_id: str
) -> str:
    """Move fully-written staged bytes into ``files/`` for a new document; return its disk name.

    The caller inserts the index row afterwards; a crash in between leaves
    an unindexed file, never an index row without one.
    """
    disk_name = link_into_place(conn, files_dir, staged, name, file_id)
    staged.unlink()
    return disk_name


def replace_contents(path: Path, body: bytes) -> None:
    """Atomically replace a document's bytes."""
    with new_staging_file(path.parent) as out:
        out.write(body)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


def rename(
    conn: sqlite3.Connection, files_dir: Path, row: dict[str, Any], new_name: str, mtime: float
) -> str:
    """Rename a document: its display name, and its file on disk to match.  Returns the new disk name."""
    old_path = path_of(files_dir, row)
    if safe_disk_name(new_name) == old_path.name:
        disk_name = old_path.name
    else:
        disk_name = link_into_place(conn, files_dir, old_path, new_name, row["id"])
    with conn:
        conn.execute(
            "UPDATE files SET name = ?, disk_name = ?, mtime = ? WHERE id = ?",
            (new_name, disk_name, mtime, row["id"]),
        )
    if disk_name != old_path.name:
        old_path.unlink(missing_ok=True)
    return disk_name


def migrate(conn: sqlite3.Connection, files_dir: Path) -> int:
    """Move documents still stored as ``files/<uuid>`` to their names, and tidy up.  Returns how many moved.

    Also removes what an interrupted write or move leaves behind: staged
    files, and extra links to a document's file under a name no document
    claims.  Idempotent, and safe to interrupt at any point: a rerun
    finishes the job.  Call it before serving requests.
    """
    moved = 0
    rows = conn.execute("SELECT id, name, disk_name FROM files").fetchall()
    for file_id, name, disk_name in rows:
        legacy = files_dir / file_id
        if disk_name is None and legacy.is_file():
            new_name = link_into_place(conn, files_dir, legacy, name, file_id)
            with conn:
                conn.execute("UPDATE files SET disk_name = ? WHERE id = ?", (new_name, file_id))
            moved += 1

    staging = files_dir / STAGING_DIR
    if staging.is_dir():
        for leftover in staging.iterdir():
            leftover.unlink()

    claimed = {
        disk_name or file_id
        for file_id, disk_name in conn.execute("SELECT id, disk_name FROM files").fetchall()
    }
    claimed_inodes = set()
    for disk_name in claimed:
        try:
            st = (files_dir / disk_name).stat()
        except FileNotFoundError:
            continue
        claimed_inodes.add((st.st_dev, st.st_ino))
    for entry in files_dir.iterdir():
        if entry.name in claimed or entry.name == STAGING_DIR or not entry.is_file():
            continue
        st = entry.stat()
        # Only a second name for a document's own file; anything else is left alone.
        if (st.st_dev, st.st_ino) in claimed_inodes:
            entry.unlink()
    return moved
